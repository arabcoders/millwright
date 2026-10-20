<template>
  <UModal
    v-model:open="open"
    :title="detail ? `File ${jobNumber(detail.id)}` : 'File details'"
    :ui="{ content: 'w-[calc(100vw-1rem)] max-w-4xl', body: 'max-h-[80vh] overflow-y-auto' }"
    @after:leave="emit('after:leave')"
  >
    <template #body>
      <div v-if="detail" class="space-y-4">
        <section class="space-y-3">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="flex min-w-0 items-start gap-3">
              <JobProgressRing
                v-if="ringProgress"
                v-bind="ringProgress"
                :label="
                  detail.active_job_status
                    ? `${detail.active_job_kind} job progress`
                    : 'File progress'
                "
                :color="detail.active_job_kind === 'healthcheck' ? 'success' : 'primary'"
              />
              <span v-else class="mw-detail-icon size-10 shrink-0"
                ><UIcon name="i-lucide-file-video" class="size-5"
              /></span>
              <div class="min-w-0">
                <UTooltip :text="fileName(detail.path)">
                  <h2 class="cursor-help truncate text-lg font-semibold text-highlighted">
                    {{ fileName(detail.path) }}
                  </h2>
                </UTooltip>
                <UTooltip :text="detail.path"
                  ><p class="cursor-help truncate text-sm text-toned">
                    {{ parentPath(detail.path) }}
                  </p></UTooltip
                >
                <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
                  <StatusBadge
                    :status="statusFor(detail, 'transcode', detail.transcode_status)"
                    :group="detail.active_job_kind === 'transcode' ? 'job' : 'media'"
                  />
                  <StatusBadge
                    :status="statusFor(detail, 'healthcheck', detail.health_status)"
                    :group="detail.active_job_kind === 'healthcheck' ? 'job' : 'health'"
                  />
                  <template
                    v-if="
                      detail.active_job_kind === 'probe' || detail.active_job_kind === 'mkv_stats'
                    "
                  >
                    <StatusBadge :status="detail.active_job_kind" group="kind" />
                    <StatusBadge
                      v-if="detail.active_job_status"
                      :status="detail.active_job_status"
                    />
                  </template>
                </div>
              </div>
            </div>
            <div v-if="!readOnly" class="ml-auto flex flex-wrap items-center justify-end gap-2">
              <UButton
                v-if="onPlay"
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-monitor-play"
                :disabled="actionsBusy"
                @click="emit('play')"
              >
                Play
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
                      :loading="actionsBusy"
                      :disabled="actionsBusy"
                    >
                      Actions
                    </UButton>
                  </UDropdownMenu>
                </span>
              </UTooltip>
            </div>
          </div>
          <USelectMenu
            v-if="snapshots.length"
            v-model="selectedSnapshotId"
            :items="snapshotOptions"
            value-key="value"
            icon="i-lucide-layers"
            aria-label="File data version"
            class="w-full"
          />
          <p v-if="selectedSnapshot" class="text-sm text-toned">
            Showing the selected version. Actions and status apply to the current file.
          </p>
          <dl
            class="grid grid-cols-1 overflow-hidden rounded-md border border-default text-sm [&>div]:border-t [&>div]:border-default [&>div:first-child]:border-t-0 sm:grid-cols-2 sm:[&>div]:border-l sm:[&>div:nth-child(-n+2)]:border-t-0 sm:[&>div:nth-child(2n+1)]:border-l-0 lg:grid-cols-4 lg:[&>div:nth-child(-n+4)]:border-t-0 lg:[&>div:nth-child(2n+1)]:border-l lg:[&>div:nth-child(4n+1)]:border-l-0"
          >
            <div
              v-for="row in metadata"
              :key="row.key"
              class="flex min-w-0 flex-col gap-1 border-default p-3"
            >
              <dt class="flex items-center gap-1 text-xs text-toned">
                <UIcon :name="row.icon" class="size-3.5" />{{ row.label }}
              </dt>
              <dd class="flex flex-wrap items-center gap-1.5" :class="row.color">
                <span class="break-all">{{ row.value }}</span>
              </dd>
            </div>
            <div class="flex min-w-0 flex-col gap-1 border-default p-3">
              <dt class="flex items-center gap-1 text-xs text-toned">
                <UIcon name="i-lucide-tag" class="size-3.5" />Medium
              </dt>
              <dd class="truncate">
                {{ selectedSnapshot ? 'Unknown' : (detail.medium ?? 'Unknown') }}
              </dd>
            </div>
            <div class="flex min-w-0 flex-col gap-1 border-default p-3">
              <dt class="flex items-center gap-1 text-xs text-toned">
                <UIcon name="i-lucide-clock" class="size-3.5" />Modified
              </dt>
              <dd>
                <span v-if="selectedSnapshot">Not recorded</span>
                <UTooltip v-else :text="formatDate(detail.modified_at, 'detail')">
                  <time :datetime="toIsoString(detail.modified_at)">{{
                    formatRelativeTime(detail.modified_at, 'en')
                  }}</time>
                </UTooltip>
              </dd>
            </div>
          </dl>
        </section>
        <UAlert
          v-if="healthFindings.length"
          color="warning"
          variant="soft"
          title="Health findings"
          :description="`${healthFindings.length} finding${healthFindings.length === 1 ? '' : 's'}: ${healthFindings.slice(0, 3).join('; ')}`"
        />
        <section class="space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
              <UIcon name="i-lucide-list-video" class="size-5" />
              Streams
            </h3>
            <div class="flex items-center gap-2">
              <UBadge v-if="activeProbe" color="neutral" variant="subtle" size="sm">
                {{ activeProbe.streams.length }} streams
              </UBadge>
              <UButton
                variant="ghost"
                color="neutral"
                size="sm"
                :trailing-icon="streamsVisible ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
                :aria-label="streamsVisible ? 'Hide streams' : 'Show streams'"
                :aria-expanded="streamsVisible"
                @click="streamsVisible = !streamsVisible"
              />
            </div>
          </div>
          <div v-if="streamsVisible">
            <div v-if="activeProbe" class="max-h-80 overflow-y-auto">
              <UTable
                :data="activeProbe.streams"
                :columns="streamColumns"
                :ui="{ tbody: '[&>tr]:transition-colors [&>tr:hover]:bg-elevated/60' }"
              />
            </div>
            <p v-else class="text-sm text-toned">No stream info available.</p>
          </div>
        </section>
        <section class="space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
              <UIcon name="i-lucide-list-checks" class="size-5" />
              Jobs
            </h3>
            <div class="flex items-center gap-2">
              <UBadge color="neutral" variant="subtle" size="sm">
                {{ detail.jobs.length }} jobs
              </UBadge>
              <UButton
                variant="ghost"
                color="neutral"
                size="sm"
                :trailing-icon="jobsVisible ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
                :aria-label="jobsVisible ? 'Hide jobs' : 'Show jobs'"
                :aria-expanded="jobsVisible"
                @click="jobsVisible = !jobsVisible"
              />
            </div>
          </div>
          <div v-if="jobsVisible">
            <ul v-if="detail.jobs.length" class="mw-frame divide-y divide-default">
              <li v-for="job in detail.jobs" :key="job.id" class="p-1">
                <button
                  type="button"
                  class="flex w-full flex-wrap items-center gap-2 rounded-md p-1.5 text-start transition-colors hover:bg-elevated focus-visible:outline-2 focus-visible:outline-primary"
                  @click="openJobId = job.id"
                >
                  <span class="text-xs text-toned">{{ jobNumber(job.id) }}</span>
                  <StatusBadge :status="job.status" />
                  <UBadge color="neutral" variant="subtle" size="sm">{{ job.kind }}</UBadge>
                  <UTooltip v-if="job.error" :text="job.error"
                    ><span class="min-w-0 flex-1 truncate text-xs text-error">{{
                      job.error
                    }}</span></UTooltip
                  >
                  <UTooltip :text="formatDate(job.created_at, 'detail')"
                    ><time
                      class="ml-auto shrink-0 text-xs text-toned"
                      :datetime="toIsoString(job.created_at)"
                      >{{ formatRelativeTime(job.created_at, 'en') }}</time
                    ></UTooltip
                  >
                  <UIcon name="i-lucide-chevron-right" class="size-4 shrink-0 text-dimmed" />
                </button>
              </li>
            </ul>
            <p v-else class="text-sm text-toned">No jobs for this file.</p>
          </div>
        </section>
        <section v-if="activeProbe?.raw" class="space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
              <UIcon name="i-lucide-braces" class="size-5" />
              Raw ffprobe
            </h3>
            <UButton
              color="neutral"
              variant="ghost"
              size="sm"
              :trailing-icon="rawOpen ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
              :aria-label="rawOpen ? 'Hide raw ffprobe' : 'Show raw ffprobe'"
              :aria-expanded="rawOpen"
              @click="rawOpen = !rawOpen"
            />
          </div>
          <pre
            v-if="rawOpen"
            class="mw-frame max-h-72 overflow-auto p-3 font-mono text-xs text-toned"
            >{{ JSON.stringify(activeProbe.raw, null, 2) }}</pre>
        </section>
      </div>
    </template>
  </UModal>
  <JobDetailModal v-model:open="jobOpen" :job-id="openJobId" @open-file="jobOpen = false" />
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useNow, useStorage } from '@vueuse/core';
import type { DropdownMenuItem, TableColumn } from '@nuxt/ui';
import type { MediaFileDetail, ProbeStream } from '~/types/api';
import { jobNumber } from '~/utils';
import { fileSnapshotMetadata } from '~/utils/mediaMetadata';
import { useJobs } from '~/composables/useJobs';
import { formatDate, toIsoString } from '~/utils/date';
import { healthSamples } from '~/utils/health';
import { formatRelativeTime } from '~/utils/relativeTime';
import { fileRingProgress } from '~/utils/progress';
import { statusInfo } from '~/utils/status';
import { canSkipCooldown } from '~/composables/useFiles';

const props = defineProps<{
  detail: MediaFileDetail | null;
  healthChecking: boolean;
  transcoding: boolean;
  refreshingMetadata?: boolean;
  refreshDisabledReason?: string | null;
  transcodeDisabledReason?: string | null;
  readOnly?: boolean;
  onDryRun?: () => void;
  onPlay?: () => void;
}>();
const open = defineModel<boolean>('open', { required: true });
const rawOpen = defineModel<boolean>('rawOpen', { required: true });
const streamsVisible = useStorage('file_streams_visible', true);
const jobsVisible = useStorage('file_jobs_visible', true);
const emit = defineEmits<{
  transcode: [];
  'health-check': [];
  'dry-run': [];
  play: [];
  'refresh-metadata': [];
  'skip-cooldown': [];
  'after:leave': [];
  // Nuxt UI's overlay binds close so the overlay instance can dismiss itself.
  close: [];
}>();
const streamColumns: TableColumn<ProbeStream>[] = [
  { accessorKey: 'index', header: '#' },
  {
    accessorKey: 'codec_type',
    header: 'Type',
    cell: ({ row }) =>
      row.original.disposition_attached_pic ? 'Artwork' : row.original.codec_type,
  },
  { accessorKey: 'codec', header: 'Codec' },
  { accessorKey: 'language', header: 'Language' },
  {
    id: 'resolution',
    header: 'Resolution',
    cell: ({ row }) =>
      row.original.width && row.original.height
        ? `${row.original.width}×${row.original.height}`
        : '',
  },
  {
    accessorKey: 'fps',
    header: 'FPS',
    cell: ({ row }) => (row.original.fps == null ? '' : row.original.fps.toFixed(2)),
  },
  { accessorKey: 'pix_fmt', header: 'Pix fmt', cell: ({ row }) => row.original.pix_fmt ?? '' },
  { accessorKey: 'channels', header: 'Channels', cell: ({ row }) => row.original.channels ?? '' },
  { accessorKey: 'title', header: 'Title', cell: ({ row }) => row.original.title ?? '' },
  { accessorKey: 'filename', header: 'Filename', cell: ({ row }) => row.original.filename ?? '' },
  { accessorKey: 'mimetype', header: 'MIME type', cell: ({ row }) => row.original.mimetype ?? '' },
];

const actionsBusy = computed(
  () => props.transcoding || props.healthChecking || props.refreshingMetadata,
);
const actionItems = computed<DropdownMenuItem[][]>(() => [
  [
    ...(props.detail && canSkipCooldown(props.detail)
      ? [
          {
            label: 'Skip cooldown',
            icon: 'i-lucide-timer-off',
            disabled: actionsBusy.value,
            onSelect: () => emit('skip-cooldown'),
          },
        ]
      : []),
    {
      label: 'Refresh metadata',
      icon: 'i-lucide-refresh-cw',
      description: props.refreshDisabledReason || undefined,
      disabled: actionsBusy.value || !!props.refreshDisabledReason,
      onSelect: () => emit('refresh-metadata'),
    },
    {
      label: 'Health check',
      icon: 'i-lucide-heart-pulse',
      disabled: actionsBusy.value,
      onSelect: () => emit('health-check'),
    },
    {
      label: 'Transcode',
      icon: 'i-lucide-film',
      description: props.transcodeDisabledReason || undefined,
      disabled: actionsBusy.value || !!props.transcodeDisabledReason,
      onSelect: () => emit('transcode'),
    },
  ],
  ...(props.onDryRun
    ? [
        [
          {
            label: 'Dry run',
            icon: 'i-lucide-play',
            disabled: actionsBusy.value,
            onSelect: () => emit('dry-run'),
          },
        ],
      ]
    : []),
]);

const actionsTooltip = computed(() => {
  if (actionsBusy.value) return 'An action is already in progress';
  return 'File actions';
});

const fileName = (path: string): string => path.split(/[\\/]/).at(-1) || path;
const parentPath = (path: string): string =>
  path.slice(0, Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))) || 'Root';

const selectedSnapshotId = ref('current');
const snapshots = computed(() => props.detail?.media_snapshots ?? []);
const snapshotOptions = computed(() => [
  { value: 'current', label: 'Metadata source: Current file' },
  ...snapshots.value.map((snapshot) => {
    const version =
      snapshot.kind === 'original'
        ? 'Original file'
        : snapshot.kind === 'stage'
          ? snapshot.stage_number === null
            ? 'Stage'
            : `Stage ${snapshot.stage_number}`
          : 'Recorded result';
    const result =
      snapshot.result && snapshot.result !== 'succeeded'
        ? ` (${statusInfo(snapshot.result).label})`
        : '';
    return {
      value: snapshot.id,
      label: `Job ${jobNumber(snapshot.job_id)} · Attempt ${snapshot.attempt_number} · ${version}${result}`,
    };
  }),
]);
const selectedSnapshot = computed(
  () => snapshots.value.find((snapshot) => snapshot.id === selectedSnapshotId.value) ?? null,
);
const activeProbe = computed(() =>
  selectedSnapshot.value ? selectedSnapshot.value.probe : props.detail?.probe,
);
const metadata = computed(() =>
  props.detail ? fileSnapshotMetadata(props.detail, selectedSnapshot.value) : [],
);
watch([() => props.detail?.id, open], () => {
  selectedSnapshotId.value = 'current';
});
watch(snapshots, (items) => {
  if (
    selectedSnapshotId.value !== 'current' &&
    !items.some((item) => item.id === selectedSnapshotId.value)
  )
    selectedSnapshotId.value = 'current';
});
const { now, pause, resume } = useNow({ interval: 1000, controls: true, immediate: false });
const ringProgress = computed(() =>
  props.detail ? fileRingProgress(props.detail, now.value) : null,
);
watch(
  () => open.value && !props.detail?.active_job_status && ringProgress.value !== null,
  (waiting) => (waiting ? resume() : pause()),
  { immediate: true },
);

const statusFor = (file: MediaFileDetail, kind: string, fallback: string): string =>
  file.active_job_kind === kind && file.active_job_status ? file.active_job_status : fallback;

const openJobId = ref<number | null>(null);
const jobOpen = computed({
  get: () => openJobId.value !== null,
  set: (value: boolean) => {
    if (!value) openJobId.value = null;
  },
});

const healthFindings = ref<string[]>([]);
watch(
  () => props.detail?.id,
  async (id) => {
    openJobId.value = null;
    healthFindings.value = [];
    const file = props.detail;
    if (!id || !file || file.health_status === 'healthy') return;
    const job = file.jobs
      .filter((item) => item.kind === 'healthcheck')
      .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id)[0];
    if (!job) return;
    try {
      const detail = await useJobs().get(job.id);
      if (props.detail?.id !== id) return;
      healthFindings.value = healthSamples(detail.attempts.at(-1)?.health);
    } catch {
      // Findings stay hidden. The job row below still opens the healthcheck.
    }
  },
  { immediate: true },
);
</script>
