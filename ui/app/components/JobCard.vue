<template>
  <article
    class="mw-card @container/job-card flex h-full min-w-0 flex-col gap-3 p-4"
    :class="selected ? 'ring-2 ring-primary' : ''"
  >
    <div class="flex items-start gap-3">
      <UCheckbox
        v-if="selectable"
        class="shrink-0 self-center"
        :model-value="selected"
        :aria-label="`Select job #${job.id}`"
        :disabled="busy"
        @update:model-value="$emit('select', $event)"
      />
      <h2 class="min-w-0 flex-1">
        <UTooltip v-if="job.file" :text="job.file.path">
          <button
            type="button"
            class="w-full min-w-0 text-left text-sm font-semibold text-highlighted hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
            @click="$emit('open')"
          >
            <span v-if="directory" class="block truncate text-xs font-normal text-dimmed"
              >./{{ directory }}</span
            >
            <span class="line-clamp-2 min-h-10 break-all">{{ fileName(job.file.path) }}</span>
          </button>
        </UTooltip>
        <button
          v-else
          type="button"
          class="min-h-10 w-full min-w-0 text-left text-sm font-semibold text-highlighted hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
          @click="$emit('open')"
        >
          File unavailable
        </button>
      </h2>
      <JobProgressRing
        v-if="isActive(job.status)"
        :status="job.status"
        :percent="progressPercent(job.progress)"
        :cooldown-started-at="job.cooldown_started_at"
        :cooldown-until="job.hold_until"
        :label="`${statusInfo(job.kind, 'kind').label} job progress`"
        :color="job.kind === 'healthcheck' ? 'success' : 'primary'"
      />
      <div
        v-else
        class="flex size-10 shrink-0 items-center justify-center rounded-lg"
        :class="
          job.status === 'failed' || healthResult === 'Corrupt'
            ? 'bg-error/10 text-error'
            : job.kind === 'healthcheck'
              ? 'bg-success/10 text-success'
              : 'bg-primary/10 text-primary'
        "
      >
        <UIcon :name="statusInfo(job.kind, 'kind').icon" class="size-5" />
      </div>
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
      <StatusBadge :status="job.kind" group="kind" />
      <StatusBadge :status="job.status" />
      <UTooltip
        v-if="job.node_name || job.node_id !== null"
        :text="`Node: ${job.node_name ?? job.node_id}`"
      >
        <UBadge
          color="neutral"
          variant="subtle"
          size="sm"
          icon="i-lucide-server"
          class="max-w-full"
        >
          <span class="truncate">{{ job.node_name ?? job.node_id }}</span>
        </UBadge>
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
      class="grid grid-cols-2 gap-x-3 gap-y-2 text-xs @min-[22rem]/job-card:grid-cols-3"
    >
      <div v-for="row in metadata" :key="row.key" class="min-w-0">
        <dt class="text-dimmed">{{ row.label }}</dt>
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
        <dt class="text-dimmed">
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
        <dt class="text-dimmed">Stage</dt>
        <dd class="mt-0.5 text-toned">{{ progressStage(job.progress) }}</dd>
      </div>
      <div v-if="healthErrors !== null">
        <dt class="text-dimmed">Detected errors</dt>
        <dd class="mt-0.5" :class="healthErrors > 0 ? 'text-error' : 'text-toned'">
          {{ healthErrors }}
        </dd>
      </div>
      <div>
        <dt class="text-dimmed">Created</dt>
        <dd class="mt-0.5 text-toned">
          <UTooltip :text="formatDate(job.created_at, 'detail')">
            <time :datetime="toIsoString(job.created_at)">{{
              formatRelativeTime(job.created_at, 'en')
            }}</time>
          </UTooltip>
        </dd>
      </div>
      <div>
        <dt class="text-dimmed">Updated</dt>
        <dd class="mt-0.5 text-toned">
          <UTooltip :text="formatDate(job.updated_at, 'detail')">
            <time :datetime="toIsoString(job.updated_at)">{{
              formatRelativeTime(job.updated_at, 'en')
            }}</time>
          </UTooltip>
        </dd>
      </div>
      <div v-if="job.kind === 'transcode' && estimatedEnd">
        <dt class="text-dimmed">Estimated finish</dt>
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
      :ui="{
        root: 'p-2 gap-2',
        icon: 'size-4',
        description: 'text-xs',
      }"
    >
      <template #description><ReasonDisplay :reason="reason" /></template>
    </UAlert>
    <div v-if="job.error || job.warning" class="space-y-2">
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
    <div class="mt-auto flex flex-wrap items-center justify-end gap-2" @click.stop>
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
      <UButton
        v-if="canJobAction(job.status, 'delete')"
        color="neutral"
        variant="outline"
        size="sm"
        icon="i-lucide-trash-2"
        aria-label="Delete job"
        :loading="busy"
        :disabled="busy"
        @click="$emit('delete')"
        >Delete</UButton
      >
      <UTooltip v-if="canJobAction(job.status, 'bump')" text="Move to top">
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-arrow-up-narrow-wide"
          aria-label="Move job to top"
          :loading="busy"
          :disabled="busy"
          @click="$emit('bump')"
          >Move to top</UButton
        >
      </UTooltip>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue';
import { useNow } from '@vueuse/core';
import StatusBadge from '~/components/StatusBadge.vue';
import ReasonDisplay from '~/components/ReasonDisplay.vue';
import { formatTime } from '~/utils';
import { jobMetadata } from '~/utils/mediaMetadata';
import { elapsedSeconds, formatDate, toIsoString } from '~/utils/date';
import { relativeDirectory } from '~/utils/location';
import { formatRelativeTime } from '~/utils/relativeTime';
import { estimatedJobCompletion, progressPercent, progressStage } from '~/utils/progress';
import { statusInfo } from '~/utils/status';
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
defineEmits<{
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
