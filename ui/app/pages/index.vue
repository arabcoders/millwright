<template>
  <main class="@container w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="dashboard">
      <template #actions>
        <UTooltip :text="scanDisabledReason" :disabled="!scanDisabledReason">
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-radar"
            :loading="scanningAll"
            :disabled="!!scanDisabledReason"
            @click="scanAll"
          >
            Scan all
          </UButton>
        </UTooltip>
        <UButton
          color="neutral"
          :variant="compact ? 'soft' : 'outline'"
          size="sm"
          icon="i-lucide-panels-top-left"
          :aria-pressed="compact"
          @click="compact = !compact"
        >
          Compact
        </UButton>
      </template>
    </PageHeader>
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <template v-if="showBacklog">
        <StatCard
          label="Files awaiting completion"
          :value="stats?.backlog.remaining_files ?? 'Loading'"
          icon="i-lucide-files"
          tooltip="Counts each indexed file once while probing, healthchecks, flow decisions, or processing remain unfinished. Files not yet discovered aren't included."
          color="primary"
        />
        <StatCard
          v-if="stats?.backlog.estimated_finish_at"
          label="Estimated work finish"
          :value="finishValue"
          :hint="stats?.backlog.reason ?? 'Current unfinished work'"
          :tooltip="finishTooltip"
          :value-wrap="true"
          icon="i-lucide-clock"
          color="primary"
        />
      </template>
      <StatCard
        label="Scanner"
        :value="progressSummary.discoveryStatus"
        :hint="progressLoading || progressError || !scanner ? '' : scannerHint"
        icon="i-lucide-radar"
        :color="
          progressError ? 'error' : statusInfo(progressSummary.discoveryStatus, 'discovery').color
        "
        tooltip="Discovers files and updates the catalog. Files found isn't a completion percentage."
        to="/libraries"
      >
        <template #value>
          <span v-if="progressError">Unavailable</span>
          <span v-else-if="progressLoading || !scanner">Loading</span>
          <span v-else class="flex items-center gap-1.5">
            <StatusBadge
              :status="progressSummary.discoveryStatus"
              group="discovery"
              class="shrink-0"
            />
            <span v-if="scannerLibrary" class="truncate">{{ scannerLibrary }}</span>
          </span>
        </template>
      </StatCard>
      <StatCard
        label="Probing"
        :value="progressSummary.probingStatus"
        :hint="progressLoading || progressError || !scanner ? '' : probingHint"
        icon="i-lucide-search"
        :color="
          progressError ? 'error' : statusInfo(progressSummary.probingStatus, 'probing').color
        "
        tooltip="Reads media metadata for discovered files. Includes optional MKV statistics work. Processed counts reset on server restart."
        to="/libraries"
      >
        <template #value>
          <span v-if="progressError">Unavailable</span>
          <span v-else-if="progressLoading || !scanner">Loading</span>
          <StatusBadge v-else :status="progressSummary.probingStatus" group="probing" />
        </template>
      </StatCard>
      <StatCard
        label="Jobs succeeded (24h)"
        :value="jobsSucceeded24h"
        icon="i-lucide-circle-check-big"
        :hint="avgDurationHint"
        color="success"
        to="/jobs?status=succeeded"
      />
      <StatCard
        label="Space saved"
        :value="spaceSavedLabel"
        icon="i-lucide-hard-drive-download"
        :hint="spaceSavedHint"
        color="info"
      />
      <StatCard
        v-for="node in nodes"
        :key="node.id"
        :label="node.display_name"
        :value="nodeStatus(node)"
        :hint="nodeUsageSummary(node)"
        hint-wrap
        :tooltip="`${nodeUsageSummary(node)}. Usage reflects the whole system`"
        icon="i-lucide-server"
      >
        <template #label>
          <button
            type="button"
            class="max-w-full truncate text-start hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
            @click="selectNode(node)"
          >
            <bdi>{{ node.display_name }}</bdi>
          </button>
        </template>
        <template #value>
          <StatusBadge :status="nodeStatus(node)" group="node" />
        </template>
        <template #actions>
          <UTooltip
            :text="
              pausingId !== null && pausingId !== node.id
                ? 'Wait for the current node request to finish.'
                : node.paused
                  ? 'Resume node'
                  : 'Pause node'
            "
          >
            <UButton
              color="neutral"
              variant="ghost"
              size="xs"
              class="size-8 justify-center"
              :icon="node.paused ? 'i-lucide-play' : 'i-lucide-pause'"
              :aria-label="`${node.paused ? 'Resume' : 'Pause'} ${node.display_name}`"
              :loading="pausingId === node.id"
              :disabled="pausingId !== null"
              @click="togglePause(node)"
            />
          </UTooltip>
        </template>
      </StatCard>
    </div>

    <UAlert
      v-if="progressError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load library activity"
      :description="progressError.message"
    />

    <section class="space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h2 class="text-base font-semibold text-highlighted">
          <NuxtLink
            to="/files?sort_by=created_at&sort_dir=desc"
            class="flex items-center gap-2 hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
          >
            <UIcon name="i-lucide-file-plus-2" class="size-5" /> New media files
          </NuxtLink>
        </h2>
        <div class="ms-auto flex flex-wrap items-center justify-end gap-2">
          <UBadge color="neutral" variant="subtle" size="sm">{{ recentFiles.length }} files</UBadge>
          <UButton
            color="neutral"
            variant="ghost"
            size="sm"
            :trailing-icon="
              collapsedSections.newFiles ? 'i-lucide-chevron-down' : 'i-lucide-chevron-up'
            "
            :aria-label="
              collapsedSections.newFiles ? 'Show new media files' : 'Hide new media files'
            "
            :aria-expanded="!collapsedSections.newFiles"
            @click="collapsedSections.newFiles = !collapsedSections.newFiles"
          />
        </div>
      </div>
      <template v-if="!collapsedSections.newFiles">
        <div
          v-if="recentFilesLoading && !recentFiles.length"
          class="grid grid-cols-1 gap-3 @min-[40rem]:grid-cols-2 @min-[64rem]:grid-cols-3"
        >
          <USkeleton v-for="item in 6" :key="item" class="h-64 rounded-xl" />
        </div>
        <UAlert
          v-if="recentFilesError"
          color="error"
          variant="soft"
          icon="i-lucide-triangle-alert"
          title="Could not load recent files"
          :description="recentFilesError.message"
        />
        <div
          v-if="recentFiles.length"
          class="grid grid-cols-1 gap-3 @min-[40rem]:grid-cols-2 @min-[64rem]:grid-cols-3"
        >
          <FileCard
            v-for="file in recentFiles"
            :key="file.id"
            :file="file"
            :libraries="libraries"
            :compact="compact"
            :busy="recentFilesLoading || fileActionBusy"
            :refreshing-metadata="refreshingMetadata === file.id"
            :scanner-paused="scanner?.paused"
            @open="openRecentFile(file)"
            @play="openFilePlayer(file)"
            @refresh-metadata="refreshMetadata(file)"
            @skip-cooldown="skipCooldown(file)"
            @health-check="queueHealthCheck(file)"
            @transcode="queueTranscode(file)"
            @dry-run="openFileDryRun(file)"
          />
        </div>
        <UEmpty
          v-else-if="!recentFilesLoading && !recentFilesError"
          title="No files indexed yet."
          variant="naked"
          :ui="{
            root: 'gap-1 p-0 sm:p-0 lg:p-0',
            header: 'max-w-none gap-1',
            title: 'text-sm font-normal text-toned',
          }"
          class="mw-card py-10 sm:py-10 lg:py-10"
        >
          <template #leading>
            <UIcon name="i-lucide-files" class="size-6 text-toned" />
          </template>
        </UEmpty>
      </template>
    </section>

    <section class="space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h2 class="text-base font-semibold text-highlighted">
          <NuxtLink
            :to="`/jobs?status=${processingJobStatuses.join(',')}`"
            class="flex items-center gap-2 hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
          >
            <UIcon name="i-lucide-loader-circle" class="size-5" />Active jobs
          </NuxtLink>
        </h2>
        <div class="ms-auto flex flex-wrap items-center justify-end gap-2">
          <UBadge color="neutral" variant="subtle" size="sm"> {{ processingTotal }} jobs </UBadge>
          <UButton
            color="neutral"
            variant="ghost"
            size="sm"
            :trailing-icon="
              collapsedSections.processing ? 'i-lucide-chevron-down' : 'i-lucide-chevron-up'
            "
            :aria-label="
              collapsedSections.processing ? 'Show processing jobs' : 'Hide processing jobs'
            "
            :aria-expanded="!collapsedSections.processing"
            @click="collapsedSections.processing = !collapsedSections.processing"
          />
        </div>
      </div>
      <template v-if="!collapsedSections.processing">
        <UAlert
          v-if="processingError"
          color="error"
          variant="soft"
          icon="i-lucide-triangle-alert"
          title="Could not load processing jobs"
          :description="processingErrorMessage"
        />
        <div
          v-if="processingLoading && !processingJobs.length"
          class="grid grid-cols-1 gap-3 @min-[40rem]:grid-cols-2 @min-[64rem]:grid-cols-3"
        >
          <USkeleton v-for="item in 6" :key="item" class="h-64 rounded-xl" />
        </div>
        <div
          v-else-if="processingJobs.length"
          class="grid grid-cols-1 gap-3 @min-[40rem]:grid-cols-2 @min-[64rem]:grid-cols-3"
        >
          <DeferredContent
            v-for="job in processingJobs"
            :key="job.id"
            :min-height="compact ? 208 : 384"
            class="min-w-0"
          >
            <JobCard
              :job="job"
              :libraries="libraries"
              :compact="compact"
              :busy="processingLoading || actionLoading.has(job.id)"
              @open="openDetail(job.id)"
              @play="job.file && openFilePlayer(job.file)"
              @open-file="openRecentFile({ id: $event })"
              @cancel="cancelProcessingJob(job)"
            />
          </DeferredContent>
        </div>
        <UEmpty
          v-else-if="!processingLoading && !processingError"
          title="No active jobs"
          variant="naked"
          :ui="{
            root: 'gap-1 p-0 sm:p-0 lg:p-0',
            header: 'max-w-none gap-1',
            title: 'text-sm font-normal text-toned',
          }"
          class="mw-card py-10 sm:py-10 lg:py-10"
        >
          <template #leading>
            <UIcon name="i-lucide-inbox" class="size-6 text-toned" />
          </template>
        </UEmpty>
        <p v-if="processingTotal > processingJobs.length" class="text-sm text-toned">
          Showing {{ processingJobs.length }} of {{ processingTotal }} processing jobs.
          <NuxtLink :to="`/jobs?status=${processingJobStatuses.join(',')}`" class="text-primary"
            >View all</NuxtLink
          >
        </p>
      </template>
    </section>
    <section class="space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h2 class="text-base font-semibold text-highlighted">
          <NuxtLink
            :to="jobsLink"
            class="flex items-center gap-2 hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
          >
            <UIcon name="i-lucide-list-todo" class="size-5" />Jobs
          </NuxtLink>
        </h2>
        <div class="ms-auto flex flex-wrap items-center justify-end gap-2">
          <UBadge color="neutral" variant="subtle" size="sm"> {{ jobsTotal }} jobs </UBadge>
          <UButton
            color="neutral"
            variant="ghost"
            size="sm"
            :trailing-icon="
              collapsedSections.jobs ? 'i-lucide-chevron-down' : 'i-lucide-chevron-up'
            "
            :aria-label="collapsedSections.jobs ? 'Show jobs' : 'Hide jobs'"
            :aria-expanded="!collapsedSections.jobs"
            @click="collapsedSections.jobs = !collapsedSections.jobs"
          />
        </div>
      </div>
      <JobList v-show="!collapsedSections.jobs" dashboard :compact="compact" />
    </section>
    <JobDetailModal
      v-model:open="detailOpen"
      :job-id="detailJobId"
      @open-file="openRecentFile({ id: $event })"
    />
    <NodeDetailsModal
      v-if="selectedNode"
      :selected-node="selectedNode"
      :ffmpeg-version="ffmpegVersion"
      :format-bytes="formatBytes"
      :format-relative-time="formatRelativeTime"
      @close="selectedNode = null"
    />
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useStorage } from '@vueuse/core';
import JobDetailModal from '~/components/JobDetailModal.vue';
import JobCard from '~/components/JobCard.vue';
import FileCard from '~/components/FileCard.vue';
import FileDetailModal from '~/components/FileDetailModal.vue';
import MediaPlayerModal from '~/components/MediaPlayerModal.vue';
import StatCard from '~/components/StatCard.vue';
import StatusBadge from '~/components/StatusBadge.vue';
import NodeDetailsModal from '~/components/NodeDetailsModal.vue';
import { formatBytes, formatTime, jobNumber } from '~/utils';
import { formatRelativeTime } from '~/utils/relativeTime';
import { formatDate } from '~/utils/date';
import { statusInfo } from '~/utils/status';
import { useEvents } from '~/composables/useEvents';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import { useLibraries } from '~/composables/useLibraries';
import { useLibraryProgress } from '~/composables/useLibraryProgress';
import { useNodes } from '~/composables/useNodes';
import { useJobs } from '~/composables/useJobs';
import { useStats } from '~/composables/useStats';
import {
  manualTranscodePrerequisite,
  refreshMetadataPrerequisite,
  useFiles,
} from '~/composables/useFiles';
import { useFileDryRun } from '~/composables/useFileDryRun';
import {
  applyFileProgress,
  applyQueuedFileJob,
  newerJobTime,
  retainFileJobStatus,
  type FileJobProgress,
} from '~/composables/stateHelpers';
import type { JobRow, MediaFileDetail, MediaFileRow, NodeRow } from '~/types/api';
import { dashboardJobStatusOptions, jobRouteFilters, processingJobStatuses } from '~/utils/jobList';
import { nodeUsageSummary } from '~/utils/nodeUsage';

const collapsedSections = useStorage<Record<string, boolean>>('dashboard_sections_collapsed', {});
const compact = useStorage('dashboard_compact', false);
const route = useRoute();
const jobsLink = computed(() => {
  const filters = jobRouteFilters(route.query, dashboardJobStatusOptions);
  return {
    path: '/jobs',
    query: {
      ...filters,
      status: filters.status ?? dashboardJobStatusOptions.map((option) => option.value).join(','),
    },
  };
});
const { summary: stats, loadSummary: loadStats, setSummary } = useStats();
const {
  recentItems: recentFiles,
  recentLoading: recentFilesLoading,
  recentError: recentFilesError,
  loadRecent: loadRecentFiles,
  actionBusy: fileActionBusy,
  queueHealthCheck,
  refreshMetadata,
  skipCooldown,
  refreshingMetadata,
  queueTranscode,
  loadDetail: loadFileDetail,
} = useFiles();
const {
  total: jobsTotal,
  processingJobs,
  processingTotal,
  processingLoading,
  processingError,
  loadProcessing,
  runAction,
} = useJobs();
const { items: nodes, ensure: ensureNodes, update, setStatus, setUsage } = useNodes();
const selectedNode = ref<NodeRow | null>(null);
const { items: libraries, ensure: ensureLibraries, scanMany } = useLibraries();
const {
  summary: progressSummary,
  load: loadProgress,
  isLoading: progressLoading,
  lastError: progressError,
  scanner,
  scannerBusy,
} = useLibraryProgress();
const scannerLibrary = computed(() => {
  const id = progressSummary.value.discoveryLibraryId;
  return id === null
    ? ''
    : (libraries.value.find((library) => library.id === id)?.name ?? `Library #${id}`);
});
const scannerHint = computed(() => {
  const { discoveryStatus, found, queued } = progressSummary.value;
  const hints: string[] = [];
  if (['scanning', 'cancelling'].includes(discoveryStatus))
    hints.push(`${found.toLocaleString()} files found`);
  if (queued)
    hints.push(`${queued.toLocaleString()} ${queued === 1 ? 'library' : 'libraries'} queued`);
  if (scanner.value?.paused && discoveryStatus !== 'paused') hints.push('New scans paused');
  return hints.join(' · ');
});
const probingHint = computed(() => {
  const { processed, remaining, probingStatus, activeProbes } = progressSummary.value;
  return (
    (activeProbes ? `${activeProbes} active · ` : '') +
    `${processed.toLocaleString()} processed · ${remaining.toLocaleString()} remaining` +
    (scanner.value?.paused && probingStatus === 'scanning' ? ' · New probes paused' : '')
  );
});
const scanningAll = ref(false);
const scanDisabledReason = computed(() => {
  if (scanningAll.value) return 'Wait for the scan request to finish.';
  if (!libraries.value.length) return 'No libraries to scan.';
  if (scannerBusy.value) return 'Wait for the activity control to finish.';
  if (progressLoading.value) return 'Loading activity state.';
  if (!scanner.value) return 'Refresh to check the current activity state.';
  if (scanner.value?.failed) return 'Scanner startup failed. Check the server logs.';
  if (scanner.value?.paused) return 'Resume activity to scan now.';
  return '';
});
const scanAll = async (): Promise<void> => {
  if (scanDisabledReason.value) return;
  scanningAll.value = true;
  try {
    await scanMany(libraries.value.map((library) => library.id));
  } finally {
    scanningAll.value = false;
  }
};
const detailOpen = ref(false);
const detailJobId = ref<number | null>(null);
const actionLoading = ref(new Set<number>());
const pausingId = ref<number | null>(null);
const notify = useNotification();
const { messageFor } = useApiErrorMessage();
const processingErrorMessage = computed(() =>
  messageFor(processingError.value?.detail, 'Could not load processing jobs. Try again.'),
);
const openDetail = (id: number): void => {
  detailJobId.value = id;
  detailOpen.value = true;
};
const selectNode = (node: NodeRow): void => {
  selectedNode.value = node;
};
const nodeStatus = (node: NodeRow): string =>
  node.state === 'online' && node.active_jobs > 0
    ? 'busy'
    : node.state === 'online' && node.paused
      ? 'paused'
      : node.state;
const togglePause = async (node: NodeRow): Promise<void> => {
  if (pausingId.value !== null) return;
  pausingId.value = node.id;
  try {
    await update(node.id, { paused: !node.paused });
  } catch {
  } finally {
    pausingId.value = null;
  }
};
const ffmpegVersion = (node: NodeRow): string =>
  (node.capabilities?.ffmpeg_version ?? '').split(' ').slice(0, 3).join(' ');
const refreshAll = (): void => {
  void loadStats();
  void loadProcessing();
  void ensureNodes();
  void ensureLibraries();
  void loadProgress();
  void loadRecentFiles().catch(() => {});
};
const recentFileName = (path: string): string => path.split(/[\\/]/).at(-1) || path;
const overlay = useOverlay();
let disposed = false;
const fileDetail = ref<MediaFileDetail | null>(null);
const fileDetailOpen = ref(false);
const fileDetailId = ref<number | null>(null);
const fileRawOpen = ref(false);
const healthChecking = ref(false);
const transcoding = ref(false);
let fileDetailLoad: {
  id: number;
  row?: MediaFileRow;
  progress: Map<number, FileJobProgress>;
} | null = null;
const terminalFileRefreshes = new Set<string>();
let fileDetailModal: ReturnType<typeof overlay.create<typeof FileDetailModal>> | null = null;
let playerModal: ReturnType<typeof overlay.create<typeof MediaPlayerModal>> | null = null;
const playerModalFileId = ref<number | null>(null);
const closeFileDetail = (open: boolean): void => {
  if (open) return;
  fileDetailOpen.value = false;
  fileDetailId.value = null;
  fileDetailLoad = null;
  fileDetail.value = null;
  fileRawOpen.value = false;
  const modal = fileDetailModal;
  fileDetailModal = null;
  modal?.close();
};
const loadRecentDetail = async (id: number): Promise<void> => {
  const journal: NonNullable<typeof fileDetailLoad> = {
    id,
    progress: new Map(),
  };
  fileDetailLoad = journal;
  try {
    const file = await loadFileDetail(id);
    if (
      !disposed &&
      fileDetailLoad === journal &&
      fileDetailOpen.value &&
      fileDetailId.value === id
    ) {
      const merged = { ...file, ...journal.row };
      if (fileDetail.value?.id === id) retainFileJobStatus(merged, fileDetail.value);
      if (merged.active_job_id !== null) {
        const update = journal.progress.get(merged.active_job_id);
        if (update) applyFileProgress(merged, update);
      }
      fileDetail.value = merged;
    }
  } catch (error) {
    if (!disposed && fileDetailLoad === journal && fileDetailId.value === id) {
      closeFileDetail(false);
      notify.error(error instanceof Error ? error.message : 'Could not load file.');
    }
  } finally {
    if (fileDetailLoad === journal) fileDetailLoad = null;
  }
};
const openRecentFile = (file: Pick<MediaFileRow, 'id'>): void => {
  if (disposed || fileActionBusy.value || healthChecking.value || transcoding.value) return;
  closeFileDetail(false);
  terminalFileRefreshes.clear();
  fileDetailId.value = file.id;
  fileDetailOpen.value = true;
  fileDetailModal = overlay.create(FileDetailModal, {
    props: fileDetailProps.value,
    destroyOnClose: true,
  });
  const modal = fileDetailModal;
  void modal.open().result.then(() => {
    if (fileDetailModal === modal) closeFileDetail(false);
  });
  void loadRecentDetail(file.id);
};
const {
  isOpen: fileDryRunOpen,
  fileId: dryFileId,
  open: openFilePreview,
} = useFileDryRun((id) => openRecentFile({ id }));
const openFileDryRun = async (file: MediaFileRow): Promise<void> => {
  if (disposed || fileActionBusy.value || healthChecking.value || transcoding.value) return;
  try {
    await openFilePreview(file);
  } catch (error) {
    if (!disposed) notify.error(error instanceof Error ? error.message : 'Could not open dry run.');
  }
};
const openDetailDryRun = (): void => {
  if (!fileDetail.value || fileActionBusy.value || fileDetailLoad) return;
  if (fileDryRunOpen.value && dryFileId.value === fileDetail.value.id) {
    closeFileDetail(false);
    return;
  }
  void openFileDryRun(fileDetail.value);
};
const openFilePlayer = (file: Pick<MediaFileRow, 'id' | 'path'>): void => {
  if (disposed || fileActionBusy.value || healthChecking.value || transcoding.value) return;
  playerModal?.close();
  playerModalFileId.value = file.id;
  playerModal = overlay.create(MediaPlayerModal, {
    props: { open: true, fileId: file.id, title: recentFileName(file.path) },
    destroyOnClose: true,
  });
  const modal = playerModal;
  void modal.open().result.then(() => {
    if (playerModal === modal) {
      playerModal = null;
      playerModalFileId.value = null;
    }
  });
};
const runFileHealthCheck = async (): Promise<void> => {
  const file = fileDetail.value;
  if (!file || fileDetailLoad || fileActionBusy.value || healthChecking.value || transcoding.value)
    return;
  healthChecking.value = true;
  try {
    if (await queueHealthCheck(file)) {
      if (!disposed && fileDetailId.value === file.id) closeFileDetail(false);
    }
  } finally {
    healthChecking.value = false;
  }
};
const runFileTranscode = async (): Promise<void> => {
  const file = fileDetail.value;
  if (!file || fileDetailLoad || fileActionBusy.value || healthChecking.value || transcoding.value)
    return;
  transcoding.value = true;
  try {
    if (await queueTranscode(file)) {
      if (!disposed && fileDetailId.value === file.id) closeFileDetail(false);
    }
  } finally {
    transcoding.value = false;
  }
};
const fileDetailProps = computed(() => ({
  open: fileDetailOpen.value,
  rawOpen: fileRawOpen.value,
  detail: fileDetail.value,
  transcodeDisabledReason: fileDetail.value
    ? manualTranscodePrerequisite(fileDetail.value, libraries.value)
    : null,
  healthChecking: healthChecking.value || fileActionBusy.value,
  transcoding: transcoding.value,
  refreshingMetadata: refreshingMetadata.value === fileDetail.value?.id,
  refreshDisabledReason: fileDetail.value
    ? refreshMetadataPrerequisite(fileDetail.value, libraries.value, !!scanner.value?.paused)
    : null,
  'onUpdate:open': closeFileDetail,
  'onUpdate:rawOpen': (open: boolean) => (fileRawOpen.value = open),
  onHealthCheck: runFileHealthCheck,
  onTranscode: runFileTranscode,
  onRefreshMetadata: () => {
    if (fileDetail.value && !fileDetailLoad) void refreshMetadata(fileDetail.value);
  },
  onSkipCooldown: () => {
    if (fileDetail.value && !fileDetailLoad) void skipCooldown(fileDetail.value);
  },
  onDryRun: openDetailDryRun,
  onPlay: () => {
    if (fileDetail.value && !fileDetailLoad) openFilePlayer(fileDetail.value);
  },
}));
watch(fileDetailProps, (props) => fileDetailModal?.patch(props));

const cancelProcessingJob = async (job: JobRow): Promise<void> => {
  if (processingLoading.value || actionLoading.value.has(job.id)) return;
  actionLoading.value = new Set(actionLoading.value).add(job.id);
  try {
    const result = await runAction(job.id, 'cancel');
    if (!result.ok) {
      notify.error(messageFor(result.error.detail, 'Request failed.'));
      return;
    }
    notify.success(`Cancellation requested for job ${jobNumber(job.id)}.`);
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
  } finally {
    const next = new Set(actionLoading.value);
    next.delete(job.id);
    actionLoading.value = next;
  }
};
onMounted(() => {
  refreshAll();
});
const { on } = useEvents();
const stopEvents = on((event) => {
  if (
    event.type === 'file.status' &&
    event.payload.media_file_id !== undefined &&
    (event.payload.action === 'deleted' || event.payload.action === 'pruned')
  ) {
    const id = event.payload.media_file_id;
    if (fileDetailId.value === id) closeFileDetail(false);
    if (playerModalFileId.value === id) {
      playerModal?.close();
      playerModalFileId.value = null;
    }
    return;
  }
  if (event.type === 'stats') setSummary(event.payload);
  else if (event.type === 'node.status') {
    setStatus(
      event.payload.name,
      event.payload.state,
      event.payload.active_jobs,
      event.payload.active_job_ids,
      event.payload.active_transcodes,
      event.payload.active_healthchecks,
      event.payload.active_mkv_stats,
    );
  } else if (event.type === 'node.usage') {
    setUsage(event.payload.name, event.payload.usage, event.payload.last_seen_at);
  } else if (event.type === 'job.progress') {
    if (fileDetailLoad) {
      const update = event.payload;
      const previous = fileDetailLoad.progress.get(update.job_id);
      if (!previous || newerJobTime(update.updated_at, previous.updated_at))
        fileDetailLoad.progress.set(update.job_id, update);
    }
    if (fileDetail.value) applyFileProgress(fileDetail.value, event.payload);
  } else if (event.type === 'file.status' && event.payload.file) {
    const row = event.payload.file;
    if (fileDetail.value?.id === row.id) retainFileJobStatus(row, fileDetail.value);
    if (fileDetailLoad?.id === row.id) fileDetailLoad.row = row;
    if (fileDetailOpen.value && fileDetailId.value === row.id) {
      const file = { ...row };
      if (fileDetail.value) {
        if (
          fileDetail.value.active_job_id !== null &&
          fileDetail.value.active_job_updated_at !== null
        )
          applyFileProgress(file, {
            job_id: fileDetail.value.active_job_id,
            progress: fileDetail.value.active_job_progress,
            updated_at: fileDetail.value.active_job_updated_at,
          });
        Object.assign(fileDetail.value, file);
      }
      if (['probed', 'probe_error'].includes(event.payload.action)) void loadRecentDetail(file.id);
    }
  } else if (event.type === 'job.status' && fileDetail.value) {
    if (!('job' in event.payload)) {
      applyQueuedFileJob(fileDetail.value, event.payload);
      return;
    }
    const job = event.payload.job;
    if (job.file?.id !== fileDetail.value.id) return;
    retainFileJobStatus(fileDetail.value, job);
    const summary = {
      id: job.id,
      kind: job.kind,
      status: job.status,
      error: job.error,
      created_at: job.created_at,
    };
    fileDetail.value.jobs = [
      summary,
      ...fileDetail.value.jobs.filter((item) => item.id !== job.id),
    ].sort((a, b) => b.id - a.id);
    if (['succeeded', 'failed', 'cancelled', 'skipped'].includes(job.status)) {
      const key = `${job.id}:${job.status}`;
      if (!terminalFileRefreshes.has(key)) {
        terminalFileRefreshes.add(key);
        void loadRecentDetail(fileDetail.value.id);
      }
    }
  } else if (event.type === 'job.deleted' && fileDetail.value) {
    fileDetail.value.jobs = fileDetail.value.jobs.filter((job) => job.id !== event.payload.job_id);
  }
});
onBeforeUnmount(() => {
  disposed = true;
  stopEvents();
  closeFileDetail(false);
  playerModal?.close();
});

const showBacklog = computed(() => !stats.value || stats.value.backlog.remaining_files > 0);
const finishValue = computed(() => {
  const estimate = stats.value?.backlog;
  if (!estimate) return 'Loading';
  return estimate.estimated_finish_at
    ? formatRelativeTime(estimate.estimated_finish_at, 'en')
    : 'Unavailable';
});
const finishTooltip = computed(() => {
  const estimate = stats.value?.backlog;
  const scope = 'Estimate for currently indexed unfinished work, not scan completion.';
  return estimate?.estimated_finish_at
    ? `${scope} ${formatDate(estimate.estimated_finish_at, 'detail')}`
    : scope;
});
const jobsSucceeded24h = computed(() => stats.value?.jobs.count ?? 0);
const avgDurationHint = computed(() => {
  const avg = stats.value?.jobs.avg_duration_s ?? 0;
  return avg > 0 ? `avg ${formatTime(avg)} per job` : '';
});
const spaceSavedTotal = computed(() =>
  Object.values(stats.value?.space_saved_by_library ?? {}).reduce((sum, n) => sum + n, 0),
);
const spaceSavedLabel = computed(() => formatBytes(spaceSavedTotal.value));
const spaceSavedHint = computed(() => {
  const count = Object.keys(stats.value?.space_saved_by_library ?? {}).length;
  return count ? `across ${count} ${count === 1 ? 'library' : 'libraries'}` : '';
});
</script>
