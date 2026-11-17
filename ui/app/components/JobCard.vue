<template>
  <article
    class="@container/job-row grid min-w-0 gap-3 p-4 sm:p-5 xl:grid-cols-[minmax(0,1fr)_auto]"
    :class="{ 'bg-primary/5': selected }"
  >
    <div class="flex min-w-0 items-start gap-3">
      <StatusIndicator :info="indicator">
        <JobProgressRing
          v-if="isActive(job.status)"
          layout="icon"
          :status="job.status"
          :percent="progressPercent(job.progress)"
          :cooldown-started-at="job.cooldown_started_at"
          :cooldown-until="job.hold_until"
          :label="indicator.label"
        />
      </StatusIndicator>
      <h2 class="min-w-0 flex-1">
        <UTooltip v-if="job.file" :text="job.file.path" :content="{ align: 'start' }">
          <button
            type="button"
            class="w-full min-w-0 text-left text-base font-semibold text-highlighted hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
            :aria-label="`Open job #${job.id} for ${fileName(job.file.path)}`"
            @click="$emit('open')"
          >
            <span class="line-clamp-2 wrap-anywhere">{{ fileName(job.file.path) }}</span>
            <span v-if="directory" class="mt-1 block truncate text-xs font-normal text-dimmed"
              >./{{ directory }}</span
            >
          </button>
        </UTooltip>
        <button
          v-else
          type="button"
          class="w-full min-w-0 text-left text-base font-semibold text-highlighted hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
          :aria-label="`Open job #${job.id}`"
          @click="$emit('open')"
        >
          File unavailable
        </button>
      </h2>
    </div>
    <div
      class="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 text-xs text-toned xl:col-span-2"
    >
      <span class="tabular-nums">#{{ job.id }}</span>
      <UTooltip :text="libraryName">
        <span class="inline-flex min-w-0 max-w-full items-center gap-1.5">
          <UIcon name="i-lucide-folder-tree" class="size-3.5 shrink-0" aria-hidden="true" />
          <span class="truncate">{{ libraryName }}</span>
        </span>
      </UTooltip>
      <StatusBadge :status="job.kind" group="kind" />
      <StatusBadge :status="job.status" />
      <UTooltip
        v-if="job.node_name || job.node_id !== null"
        :text="`Node: ${job.node_name ?? job.node_id}`"
      >
        <span class="inline-flex min-w-0 max-w-full items-center gap-1.5">
          <UIcon name="i-lucide-server" class="size-3.5 shrink-0" aria-hidden="true" />
          <span class="truncate">{{ job.node_name ?? job.node_id }}</span>
        </span>
      </UTooltip>
      <UBadge
        v-if="healthResult"
        :color="healthResult === 'Healthy' ? 'success' : 'error'"
        :icon="healthResult === 'Healthy' ? 'i-lucide-heart-pulse' : 'i-lucide-triangle-alert'"
        variant="soft"
        size="sm"
        >{{ healthResult }}</UBadge
      >
    </div>
    <dl
      v-if="!compact"
      class="grid min-w-0 grid-cols-2 gap-x-8 gap-y-3 text-sm @min-[32rem]/job-row:grid-cols-3 @min-[48rem]/job-row:grid-cols-4 @min-[64rem]/job-row:grid-cols-6 xl:col-span-2"
    >
      <div v-for="row in metadata" :key="row.key" class="min-w-0">
        <dt class="text-xs text-muted">{{ row.label }}</dt>
        <dd class="mt-0.5 flex flex-wrap items-center gap-1" :class="row.color">
          <UTooltip
            :text="row.tooltip ?? undefined"
            :disabled="!row.tooltip"
            :ui="{
              content: 'h-auto max-h-80 max-w-sm items-start overflow-y-auto',
              text: 'min-w-0 overflow-visible text-clip whitespace-pre-line wrap-break-word',
            }"
          >
            <span
              class="flex min-w-0 flex-wrap items-center gap-1"
              :tabindex="row.tooltip ? 0 : undefined"
            >
              <span v-if="row.after !== null" class="sr-only">Before:</span>
              <span class="break-all">{{ row.value }}</span>
              <template v-if="row.after !== null">
                <span class="sr-only">{{ row.expected ? 'Planned:' : 'After:' }}</span>
                <UIcon name="i-lucide-arrow-right" class="size-3 shrink-0" aria-hidden="true" />
                <span class="break-all">{{ row.after }}</span>
              </template>
            </span>
          </UTooltip>
        </dd>
      </div>
      <div v-if="processingSeconds !== null">
        <dt class="text-xs text-muted">
          {{ job.finished_at ? 'Processing duration' : 'Elapsed' }}
        </dt>
        <dd class="mt-0.5 text-toned tabular-nums">
          {{ formatTime(processingSeconds)
          }}<span v-if="running && !job.finished_at"
            >, {{ remainingTime === null ? '??:??' : `~${remainingTime}` }} left</span
          >
        </dd>
      </div>
      <div v-if="isActive(job.status) && progressStage(job.progress)">
        <dt class="text-xs text-muted">Stage</dt>
        <dd class="mt-0.5 text-toned">{{ progressStage(job.progress) }}</dd>
      </div>
      <div v-if="healthErrors !== null">
        <dt class="text-xs text-muted">Detected errors</dt>
        <dd class="mt-0.5" :class="healthErrors > 0 ? 'text-error' : 'text-toned'">
          {{ healthErrors }}
        </dd>
      </div>
      <div>
        <dt class="text-xs text-muted">Created</dt>
        <dd class="mt-0.5 text-toned">
          <UTooltip :text="formatDate(job.created_at, 'detail')">
            <time :datetime="toIsoString(job.created_at)">{{
              formatRelativeTime(job.created_at, 'en')
            }}</time>
          </UTooltip>
        </dd>
      </div>
      <div>
        <dt class="text-xs text-muted">Updated</dt>
        <dd class="mt-0.5 text-toned">
          <UTooltip :text="formatDate(job.updated_at, 'detail')">
            <time :datetime="toIsoString(job.updated_at)">{{
              formatRelativeTime(job.updated_at, 'en')
            }}</time>
          </UTooltip>
        </dd>
      </div>
      <div v-if="job.kind === 'transcode' && estimatedEnd">
        <dt class="text-xs text-muted">Estimated finish</dt>
        <dd class="mt-0.5 text-toned">
          <UTooltip :text="formatDate(estimatedEnd, 'detail')">
            <time :datetime="toIsoString(estimatedEnd)">{{
              formatRelativeTime(estimatedEnd, 'en')
            }}</time>
          </UTooltip>
        </dd>
      </div>
    </dl>
    <UAlert
      v-if="reason"
      color="info"
      variant="soft"
      icon="i-lucide-info"
      class="xl:col-span-2"
      :ui="{
        root: 'p-2 gap-2',
        icon: 'size-4',
        description: 'text-xs',
      }"
    >
      <template #description><ReasonDisplay :reason="reason" /></template>
    </UAlert>
    <div v-if="job.error || job.warning" class="space-y-2 xl:col-span-2">
      <button
        v-if="job.error"
        type="button"
        class="flex w-full items-start gap-2 rounded-lg bg-error/10 p-2 text-left text-xs text-error hover:bg-error/15 focus-visible:outline-2 focus-visible:outline-error"
        :aria-label="`View job error: ${job.error}`"
        @click="$emit('open')"
      >
        <UIcon name="i-lucide-circle-alert" class="mt-0.5 size-4 shrink-0" />
        <span class="whitespace-pre-wrap break-all">{{ job.error }}</span>
      </button>
      <button
        v-if="job.warning"
        type="button"
        class="flex w-full items-start gap-2 rounded-lg bg-warning/10 p-2 text-left text-xs text-warning hover:bg-warning/15 focus-visible:outline-2 focus-visible:outline-warning"
        :aria-label="`View job warning: ${job.warning}`"
        @click="$emit('open')"
      >
        <UIcon name="i-lucide-triangle-alert" class="mt-0.5 size-4 shrink-0" />
        <span class="whitespace-pre-wrap break-all">{{ job.warning }}</span>
      </button>
    </div>
    <div
      class="flex flex-wrap items-center justify-end gap-1.5 xl:col-start-2 xl:row-start-1 xl:self-start"
      @click.stop
    >
      <UCheckbox
        v-if="selectable"
        class="shrink-0"
        :model-value="selected"
        :aria-label="`Select job #${job.id}`"
        :disabled="busy"
        @update:model-value="$emit('select', $event)"
      />
      <UButton
        v-if="job.file"
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
        v-if="job.file"
        color="neutral"
        variant="outline"
        size="sm"
        icon="i-lucide-file-video"
        :loading="fileLoading"
        :disabled="busy"
        @click="$emit('open-file', job.file.id)"
        >View file</UButton
      >
      <UButton
        v-if="canJobAction(job.status, 'cancel')"
        color="neutral"
        variant="outline"
        size="sm"
        icon="i-lucide-x"
        aria-label="Cancel job"
        :loading="busy"
        :disabled="busy"
        @click="$emit('cancel')"
        >Cancel</UButton
      >
      <UButton
        v-else-if="canJobAction(job.status, 'retry')"
        color="neutral"
        variant="outline"
        size="sm"
        icon="i-lucide-rotate-cw"
        aria-label="Requeue job"
        :loading="busy"
        :disabled="busy"
        @click="$emit('retry')"
        >Requeue</UButton
      >
      <UDropdownMenu
        v-if="actionItems.length"
        :items="actionItems"
        :modal="false"
        :content="{ align: 'end' }"
      >
        <UButton
          color="neutral"
          variant="ghost"
          size="sm"
          icon="i-lucide-ellipsis"
          square
          :aria-label="`Actions for job #${job.id}`"
          :loading="busy"
          :disabled="busy"
        />
      </UDropdownMenu>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue';
import { useNow } from '@vueuse/core';
import type { DropdownMenuItem } from '@nuxt/ui';
import StatusBadge from '~/components/StatusBadge.vue';
import ReasonDisplay from '~/components/ReasonDisplay.vue';
import { formatTime } from '~/utils';
import { jobMetadata } from '~/utils/mediaMetadata';
import { elapsedSeconds, formatDate, toIsoString } from '~/utils/date';
import { relativeDirectory } from '~/utils/location';
import { formatRelativeTime } from '~/utils/relativeTime';
import { estimatedJobCompletion, progressPercent, progressStage } from '~/utils/progress';
import { jobIndicatorInfo } from '~/utils/status';
import { canJobAction, jobReason } from '~/utils/jobList';
import type { JobRow, JobStatus, LibraryRow } from '~/types/api';
import { ACTIVE_JOB_STATUSES } from '~/types/api';

const props = withDefaults(
  defineProps<{
    job: JobRow;
    libraries: LibraryRow[];
    selectable?: boolean;
    selected?: boolean;
    busy?: boolean;
    fileLoading?: boolean;
    compact?: boolean;
  }>(),
  {
    selectable: false,
    selected: false,
    busy: false,
    fileLoading: false,
    compact: false,
  },
);
const emit = defineEmits<{
  open: [];
  play: [];
  'open-file': [id: number];
  select: [value: boolean | 'indeterminate'];
  cancel: [];
  retry: [];
  bump: [];
  delete: [];
}>();

const library = computed(() =>
  props.libraries.find((library) => library.id === props.job.library_id),
);
const libraryName = computed(() => library.value?.name ?? 'Library unavailable');
const actionItems = computed<DropdownMenuItem[]>(() => {
  const items: DropdownMenuItem[] = [];
  if (canJobAction(props.job.status, 'bump'))
    items.push({
      label: 'Move to top',
      icon: 'i-lucide-arrow-up-narrow-wide',
      disabled: props.busy,
      onSelect: () => emit('bump'),
    });
  if (canJobAction(props.job.status, 'delete'))
    items.push({
      label: 'Delete',
      icon: 'i-lucide-trash-2',
      color: 'error',
      disabled: props.busy,
      onSelect: () => emit('delete'),
    });
  return items;
});
const directory = computed(() =>
  props.job.file && library.value
    ? relativeDirectory(props.job.file.path, library.value.roots)
    : '',
);
const isActive = (value: JobStatus): boolean => ACTIVE_JOB_STATUSES.includes(value);
const reason = computed(() => jobReason(props.job));
const running = computed(() => isActive(props.job.status) && props.job.started_at !== null);
const { now, pause, resume } = useNow({
  interval: 1000,
  controls: true,
  immediate: false,
});
watch(running, (value) => (value ? resume() : pause()), { immediate: true });
const processingSeconds = computed(() =>
  props.job.finished_at || running.value
    ? elapsedSeconds(props.job.started_at, props.job.finished_at ?? now.value)
    : null,
);
const healthResult = computed(() => {
  if (props.job.kind !== 'healthcheck' || props.job.status !== 'succeeded') return null;
  const healthy = props.job.latest_attempt?.health?.['healthy'];
  return healthy === true ? 'Healthy' : healthy === false ? 'Corrupt' : null;
});
const indicator = computed(() =>
  jobIndicatorInfo(props.job, props.job.latest_attempt?.health?.['healthy']),
);
const healthErrors = computed(() => {
  const count = props.job.latest_attempt?.health?.['error_count'];
  return healthResult.value && typeof count === 'number' && Number.isFinite(count) && count >= 0
    ? count
    : null;
});
const fileName = (path: string): string => path.split(/[\\/]/).at(-1) || path;
const estimatedEnd = computed(() => estimatedJobCompletion(props.job));
const remainingTime = computed(() => {
  const seconds = elapsedSeconds(now.value, estimatedEnd.value);
  if (seconds === null) return null;
  const remaining = Math.ceil(seconds);
  return remaining < 60 ? `00:${String(remaining).padStart(2, '0')}` : formatTime(remaining);
});
const metadata = computed(() =>
  jobMetadata(props.job.file, props.job.latest_attempt, props.job).filter(
    (row) =>
      ['size', 'video_codec', 'resolution', 'duration', 'bit_rate', 'audio', 'subtitle'].includes(
        row.key,
      ) &&
      (!['bit_rate', 'audio', 'subtitle'].includes(row.key) || row.value !== 'Unknown'),
  ),
);
</script>
