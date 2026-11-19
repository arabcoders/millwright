<template>
  <article
    class="@container/file-row grid min-w-0 gap-3 p-4 sm:p-5 xl:grid-cols-[minmax(0,1fr)_auto]"
    :class="{ 'bg-primary/5': selected }"
  >
    <div class="flex min-w-0 items-start gap-3">
      <StatusIndicator :info="indicator">
        <JobProgressRing v-if="progress" v-bind="progress" layout="icon" :label="indicator.label" />
      </StatusIndicator>
      <h2 class="min-w-0 flex-1">
        <UTooltip :text="file.path" :content="{ align: 'start' }" :ui="{ text: 'mask-it' }">
          <button
            type="button"
            class="w-full min-w-0 text-left text-base font-semibold text-highlighted hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
            :aria-label="`Open details for ${fileName(file.path)}`"
            :disabled="busy"
            @click="$emit('open')"
          >
            <span class="mask-it line-clamp-2 wrap-anywhere">{{ fileName(file.path) }}</span>
            <span
              v-if="directory"
              class="mask-it mt-1 block truncate text-xs font-normal text-dimmed"
              >./{{ directory }}</span
            >
          </button>
        </UTooltip>
      </h2>
    </div>
    <div
      class="flex flex-wrap items-center justify-end gap-1.5 xl:col-start-2 xl:row-start-1 xl:self-start"
      @click.stop
    >
      <UCheckbox
        v-if="selectable"
        class="shrink-0"
        :model-value="selected"
        :aria-label="`Select file ${fileName(file.path)}`"
        :disabled="busy"
        @update:model-value="$emit('select', $event)"
      />
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
              variant="ghost"
              size="sm"
              icon="i-lucide-ellipsis"
              square
              :aria-label="`Actions for ${fileName(file.path)}`"
              :loading="busy || refreshingMetadata"
              :disabled="actionsBlocked"
            />
          </UDropdownMenu>
        </span>
      </UTooltip>
    </div>
    <div
      class="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 text-xs text-toned xl:col-span-2"
    >
      <UTooltip :text="libraryName" :ui="{ text: 'mask-it' }">
        <span class="inline-flex min-w-0 max-w-full items-center gap-1.5">
          <UIcon name="i-lucide-folder-tree" class="size-3.5 shrink-0" aria-hidden="true" />
          <span class="mask-it truncate">{{ libraryName }}</span>
        </span>
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
      <UTooltip
        v-if="cooldown !== null"
        text="Time remaining before processing can start. The file must stay unchanged during the cooldown."
        :content="{ align: 'start' }"
      >
        <UBadge color="info" variant="soft" size="sm" class="gap-1" tabindex="0">
          <UIcon name="i-lucide-timer" class="size-3.5 shrink-0" aria-hidden="true" />
          <span class="sr-only">Cooldown remaining:</span>
          <span class="font-mono font-semibold tabular-nums">
            {{ formatCountdown(cooldown, 'full') }}
          </span>
        </UBadge>
      </UTooltip>
    </div>
    <dl
      v-if="!compact"
      class="grid min-w-0 grid-cols-2 gap-x-8 gap-y-3 text-sm @min-[32rem]/file-row:grid-cols-3 @min-[48rem]/file-row:grid-cols-4 @min-[64rem]/file-row:grid-cols-6 xl:col-span-2"
    >
      <div v-for="row in fileMetadata(file)" :key="row.key" class="min-w-0">
        <dt class="text-xs text-muted">{{ row.label }}</dt>
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
        <dt class="text-xs text-muted">Added</dt>
        <dd class="mt-0.5 text-toned">
          <UTooltip :text="formatDate(file.created_at, 'detail')">
            <time :datetime="toIsoString(file.created_at)">{{
              formatRelativeTime(file.created_at, 'en')
            }}</time>
          </UTooltip>
        </dd>
      </div>
      <div>
        <dt class="text-xs text-muted">Updated</dt>
        <dd class="mt-0.5 text-toned">
          <UTooltip :text="formatDate(file.updated_at, 'detail')">
            <time :datetime="toIsoString(file.updated_at)">{{
              formatRelativeTime(file.updated_at, 'en')
            }}</time>
          </UTooltip>
        </dd>
      </div>
    </dl>
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
import { cooldownRemaining, fileRingProgress, formatCountdown } from '~/utils/progress';
import { fileIndicatorInfo } from '~/utils/status';
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
const progress = computed(() => fileRingProgress(props.file, now.value));
const cooldown = computed(() => {
  const value = progress.value;
  return value
    ? cooldownRemaining(value.status, value.cooldownStartedAt, value.cooldownUntil, now.value)
    : null;
});
const indicator = computed(() => fileIndicatorInfo(props.file));
watch(
  () => cooldown.value !== null && cooldown.value > 0,
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
