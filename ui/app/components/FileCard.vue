<template>
  <article
    class="mw-card @container/file-card flex h-full min-w-0 flex-col gap-3 p-4"
    :class="selected ? 'ring-2 ring-primary' : ''"
  >
    <div class="flex items-start gap-3">
      <UCheckbox
        v-if="selectable"
        class="shrink-0 self-center"
        :model-value="selected"
        :aria-label="`Select file ${fileName(file.path)}`"
        :disabled="busy"
        @update:model-value="$emit('select', $event)"
      />
      <h2 class="min-w-0 flex-1">
        <UTooltip :text="file.path">
          <button
            type="button"
            class="w-full min-w-0 text-left text-sm font-semibold text-highlighted hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
            :aria-label="`Open details for ${fileName(file.path)}`"
            :disabled="busy"
            @click="$emit('open')"
          >
            <span v-if="directory" class="block truncate text-xs font-normal text-dimmed"
              >./{{ directory }}</span
            >
            <span class="line-clamp-2 min-h-10 break-all">{{ fileName(file.path) }}</span>
          </button>
        </UTooltip>
      </h2>
      <JobProgressRing
        v-if="ringProgress"
        v-bind="ringProgress"
        :label="file.active_job_status ? `${file.active_job_kind} job progress` : 'File progress'"
        :color="file.active_job_kind === 'healthcheck' ? 'success' : 'primary'"
      />
      <span
        v-else
        class="flex size-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary"
      >
        <UIcon name="i-lucide-file-video" class="size-5" aria-hidden="true" />
      </span>
    </div>
    <div class="flex flex-wrap items-center gap-1.5">
      <UTooltip :text="libraryName">
        <UBadge
          color="neutral"
          variant="subtle"
          size="sm"
          icon="i-lucide-folder-tree"
          class="max-w-full"
        >
          <span class="truncate">{{ libraryName }}</span>
        </UBadge>
      </UTooltip>
      <StatusBadge
        v-if="file.active_job_kind === 'transcode'"
        :status="file.active_job_kind"
        group="kind"
      />
      <StatusBadge
        :status="statusFor('transcode', file.transcode_status)"
        :group="file.active_job_kind === 'transcode' ? 'job' : 'media'"
      />
      <StatusBadge
        v-if="file.active_job_kind === 'healthcheck'"
        :status="file.active_job_kind"
        group="kind"
      />
      <StatusBadge
        :status="statusFor('healthcheck', file.health_status)"
        :group="file.active_job_kind === 'healthcheck' ? 'job' : 'health'"
      />
      <template v-if="file.active_job_kind === 'probe' || file.active_job_kind === 'mkv_stats'">
        <StatusBadge :status="file.active_job_kind" group="kind" />
        <StatusBadge v-if="file.active_job_status" :status="file.active_job_status" />
      </template>
    </div>
    <dl
      v-if="!compact"
      class="grid grid-cols-2 gap-x-3 gap-y-2 text-xs @min-[22rem]/file-card:grid-cols-3"
    >
      <div v-for="row in fileMetadata(file)" :key="row.key" class="min-w-0">
        <dt class="text-dimmed">{{ row.label }}</dt>
        <dd class="mt-0.5 flex flex-wrap items-center gap-1" :class="row.color">
          <span v-if="row.after !== null" class="sr-only">Before:</span>
          <span class="break-all">{{ row.value }}</span>
          <template v-if="row.after !== null">
            <span class="sr-only">After:</span>
            <UIcon name="i-lucide-arrow-right" class="size-3 shrink-0" aria-hidden="true" />
            <span class="break-all">{{ row.after }}</span>
          </template>
        </dd>
      </div>
      <div>
        <dt class="text-dimmed">Added</dt>
        <dd class="mt-0.5 text-toned">
          <UTooltip :text="formatDate(file.created_at, 'detail')">
            <time :datetime="toIsoString(file.created_at)">{{
              formatRelativeTime(file.created_at, 'en')
            }}</time>
          </UTooltip>
        </dd>
      </div>
      <div>
        <dt class="text-dimmed">Updated</dt>
        <dd class="mt-0.5 text-toned">
          <UTooltip :text="formatDate(file.updated_at, 'detail')">
            <time :datetime="toIsoString(file.updated_at)">{{
              formatRelativeTime(file.updated_at, 'en')
            }}</time>
          </UTooltip>
        </dd>
      </div>
    </dl>
    <div class="mt-auto flex flex-wrap items-center justify-end gap-2" @click.stop>
      <UButton
        color="neutral"
        variant="outline"
        size="sm"
        icon="i-lucide-monitor-play"
        :disabled="busy"
        @click="$emit('play')"
      >
        Play
      </UButton>
      <UButton
        v-if="canSkipCooldown(file)"
        color="neutral"
        variant="outline"
        size="sm"
        icon="i-lucide-timer-off"
        :loading="busy"
        :disabled="actionsBlocked"
        @click="$emit('skip-cooldown')"
      >
        Skip cooldown
      </UButton>
      <UTooltip :text="actionsTooltip">
        <span class="inline-flex">
          <UDropdownMenu
            :items="actionItems"
            :content="{ align: 'end' }"
            :ui="{ content: 'w-72', itemDescription: 'whitespace-normal' }"
            :modal="false"
          >
            <UButton
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-settings-2"
              trailing-icon="i-lucide-chevron-down"
              :loading="busy || refreshingMetadata"
              :disabled="actionsBlocked"
            >
              Actions
            </UButton>
          </UDropdownMenu>
        </span>
      </UTooltip>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue';
import { useNow } from '@vueuse/core';
import type { DropdownMenuItem } from '@nuxt/ui';
import { fileMetadata } from '~/utils/mediaMetadata';
import { formatDate, toIsoString } from '~/utils/date';
import { relativeDirectory } from '~/utils/location';
import { formatRelativeTime } from '~/utils/relativeTime';
import { fileRingProgress } from '~/utils/progress';
import {
  canSkipCooldown,
  manualTranscodePrerequisite,
  refreshMetadataPrerequisite,
} from '~/composables/useFiles';
import type { LibraryRow, MediaFileRow } from '~/types/api';

const props = withDefaults(
  defineProps<{
    file: MediaFileRow;
    libraries: LibraryRow[];
    selectable?: boolean;
    selected?: boolean;
    busy?: boolean;
    refreshingMetadata?: boolean;
    scannerPaused?: boolean;
    compact?: boolean;
  }>(),
  {
    selectable: false,
    selected: false,
    busy: false,
    refreshingMetadata: false,
    scannerPaused: false,
    compact: false,
  },
);
const emit = defineEmits<{
  open: [];
  select: [value: boolean | 'indeterminate'];
  play: [];
  'refresh-metadata': [];
  'skip-cooldown': [];
  'health-check': [];
  transcode: [];
  'dry-run': [];
}>();

const library = computed(() =>
  props.libraries.find((library) => library.id === props.file.library_id),
);
const libraryName = computed(() => library.value?.name ?? 'Library unavailable');
const { now, pause, resume } = useNow({ interval: 1000, controls: true, immediate: false });
const ringProgress = computed(() => fileRingProgress(props.file, now.value));
watch(
  () => !props.file.active_job_status && ringProgress.value !== null,
  (waiting) => (waiting ? resume() : pause()),
  { immediate: true },
);
const refreshDisabledReason = computed(() => {
  if (props.busy || props.refreshingMetadata) return 'An action is already in progress';
  return refreshMetadataPrerequisite(props.file, props.libraries, props.scannerPaused);
});
const actionsBlocked = computed(() => props.busy || props.refreshingMetadata);
const transcodeDisabledReason = computed(() =>
  manualTranscodePrerequisite(props.file, props.libraries),
);
const actionItems = computed<DropdownMenuItem[][]>(() => [
  [
    {
      label: 'Refresh metadata',
      icon: 'i-lucide-refresh-cw',
      description: refreshDisabledReason.value || undefined,
      disabled: !!refreshDisabledReason.value,
      onSelect: () => emit('refresh-metadata'),
    },
    {
      label: 'Health check',
      icon: 'i-lucide-heart-pulse',
      disabled: actionsBlocked.value,
      onSelect: () => emit('health-check'),
    },
    {
      label: 'Transcode',
      icon: 'i-lucide-film',
      description: transcodeDisabledReason.value || undefined,
      disabled: actionsBlocked.value || !!transcodeDisabledReason.value,
      onSelect: () => emit('transcode'),
    },
  ],
  [
    {
      label: 'Dry run',
      icon: 'i-lucide-play',
      disabled: actionsBlocked.value,
      onSelect: () => emit('dry-run'),
    },
  ],
]);
const actionsTooltip = computed(() => {
  if (props.busy || props.refreshingMetadata) return 'An action is already in progress';
  return 'File actions';
});
const directory = computed(() =>
  library.value ? relativeDirectory(props.file.path, library.value.roots) : '',
);
const fileName = (path: string): string => path.split(/[\\/]/).at(-1) || path;
const statusFor = (kind: string, fallback: string): string =>
  props.file.active_job_kind === kind && props.file.active_job_status
    ? props.file.active_job_status
    : fallback;
</script>
