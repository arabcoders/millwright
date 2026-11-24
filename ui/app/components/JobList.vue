<template>
  <div class="min-w-0 space-y-6">
    <DefinePagination v-slot="{ position }">
      <ListPagination
        v-if="!lastError && (jobs.length || offset > 0)"
        label="Jobs"
        :position="position"
        :limit="limit"
        :offset="offset"
        :total="total"
        :shown="jobs.length"
        :disabled="loading || bulkBusy"
        @update:limit="changeLimit($event, position)"
        @update:offset="changeOffset($event, position)"
      >
        <UButton
          v-if="jobs.length"
          size="sm"
          color="neutral"
          variant="outline"
          :icon="allVisibleSelected ? 'i-lucide-square-check' : 'i-lucide-square'"
          :disabled="actionsBlocked"
          @click="allVisibleSelected ? clearSelection() : selectVisible()"
          >{{ allVisibleSelected ? 'Clear selection' : 'Select page' }}</UButton
        >
        <UBadge v-if="selectedIds.size" color="primary" variant="subtle"
          >{{ selectedIds.size }} selected</UBadge
        >
        <UDropdownMenu
          v-if="availableBulkActions.length"
          :items="bulkActionItems"
          :modal="false"
          :ui="{ content: 'w-80', itemDescription: 'whitespace-normal' }"
        >
          <UButton
            size="sm"
            color="neutral"
            variant="outline"
            :loading="bulkBusy"
            :disabled="actionsBlocked || actionLoading.size > 0"
            icon="i-lucide-list"
            trailing-icon="i-lucide-chevron-down"
            >Actions</UButton
          >
        </UDropdownMenu>
      </ListPagination>
    </DefinePagination>
    <PageHeader v-if="!dashboard" page="jobs">
      <template #actions>
        <div class="flex w-full flex-wrap items-center justify-end gap-2 xl:w-auto">
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-upload"
            :disabled="bulkBusy"
            @click="importInput?.click()"
            >View job export</UButton
          >
          <input
            ref="importInput"
            type="file"
            accept=".json,application/json"
            class="hidden"
            @change="importJob"
          />
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-refresh-cw"
            aria-label="Refresh jobs"
            :loading="loading"
            :disabled="bulkBusy"
            @click="refreshJobs"
            >Refresh</UButton
          >
        </div>
      </template>
    </PageHeader>

    <UAlert
      v-if="lastError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load jobs"
      :description="loadError"
    />
    <UAlert
      v-if="libraryError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load libraries"
      :description="libraryError.message"
    />

    <ListFilters
      v-model:query="query"
      search-placeholder="Search path, job ID, or file_id:123..."
      :sort-by="sortBy"
      :sort-dir="sortDir"
      :sort-options="jobSortOptions"
      sort-label="Sort jobs"
      :filters-active="filtersActive"
      :disabled="bulkBusy"
      @update:sort-by="updateQuery({ sort_by: $event })"
      @update:sort-dir="updateQuery({ sort_dir: $event })"
      @clear-filters="updateQuery({ query: '', kind: JOB_FILTER_ALL, status: '' })"
    >
      <USelectMenu
        value-key="value"
        v-model="kind"
        :items="jobKindOptions"
        :disabled="bulkBusy"
        size="sm"
        class="w-full sm:w-44"
        aria-label="Filter by job type"
        @update:model-value="updateQuery({ kind })"
      />
      <USelectMenu
        value-key="value"
        v-model="statuses"
        :items="statusOptions"
        multiple
        placeholder="All statuses"
        :clear="{ 'aria-label': 'Clear status filter' }"
        :disabled="bulkBusy"
        size="sm"
        class="w-full sm:w-56"
        aria-label="Filter by job status"
        @update:model-value="updateQuery({ status: statuses.join(',') })"
      />
      <template #trailing>
        <UButton
          v-if="dashboard"
          color="neutral"
          variant="outline"
          size="sm"
          class="ms-auto"
          icon="i-lucide-refresh-cw"
          aria-label="Refresh jobs"
          :loading="loading"
          :disabled="bulkBusy"
          @click="refreshJobs"
          >Refresh</UButton
        >
      </template>
    </ListFilters>
    <div ref="results" class="min-w-0 scroll-mt-4 space-y-4">
      <PaginationControls position="top" />
      <div
        v-if="loading && !jobs.length"
        class="mw-card divide-y divide-default"
        aria-label="Loading jobs"
      >
        <div v-for="n in 6" :key="n" class="space-y-3 p-4 sm:p-5">
          <USkeleton class="h-5 w-2/3" />
          <USkeleton class="h-4 w-1/3" />
          <USkeleton v-if="!compact" class="h-20 w-full" />
        </div>
      </div>
      <div
        v-else-if="jobs.length"
        class="mw-card divide-y divide-default"
        :aria-busy="loading || bulkBusy || actionLoading.size > 0"
      >
        <DeferredContent
          v-for="job in jobs"
          :key="job.id"
          :min-height="compact ? 144 : 280"
          class="w-full min-w-0 max-w-full"
        >
          <JobCard
            :job="job"
            :libraries="libraries"
            :compact="compact"
            selectable
            :selected="selectedIds.has(job.id)"
            :busy="loading || bulkBusy || actionLoading.has(job.id)"
            :file-loading="fileLoadingId === job.file?.id"
            @open="openDetail(job)"
            @play="job.file && openPlayer(job.file)"
            @open-file="openFile"
            @select="toggleSelection(job.id, $event)"
            @cancel="cancelJob(job)"
            @retry="retryJob(job)"
            @bump="bumpJob(job)"
            @delete="deleteJob(job)"
          />
        </DeferredContent>
      </div>
      <UEmpty
        v-else-if="!loading && !lastError"
        title="No jobs match the current filters."
        icon="i-lucide-inbox"
        :size="dashboard ? 'sm' : undefined"
      />

      <PaginationControls position="bottom" />
    </div>

    <JobDetailModal
      v-model:open="detailOpen"
      :job-id="detailJobId"
      :imported="importedJob"
      :file-loading="fileLoadingId !== null"
      @open-file="openFile"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { createReusableTemplate } from '@vueuse/core';
import type { DropdownMenuItem } from '@nuxt/ui';
import { useDialog } from '~/composables/useDialog';
import { useJobRequeue } from '~/composables/useJobRequeue';
import { useLibraries } from '~/composables/useLibraries';
import { useJobs, type BulkJobAction } from '~/composables/useJobs';
import { useFiles } from '~/composables/useFiles';
import { useListPagination } from '~/composables/useListPagination';
import {
  applyFileProgress,
  applyQueuedFileJob,
  newerJobTime,
  retainFileJobStatus,
  type FileJobProgress,
} from '~/composables/stateHelpers';
import FileDetailModal from '~/components/FileDetailModal.vue';
import MediaPlayerModal from '~/components/MediaPlayerModal.vue';
import { validJobExport } from '~/utils/jobExport';
import {
  JOB_FILTER_ALL,
  canJobAction,
  jobActionIds,
  jobKindOptions,
  jobQueryKeys,
  jobRouteFilters,
  jobSortOptions,
  jobStatusOptions,
  dashboardJobStatusOptions,
  remainingJobSelection,
  updateJobRoute,
} from '~/utils/jobList';
import type { JobDetail, JobFile, JobRow, MediaFileDetail, MediaFileRow } from '~/types/api';

const props = defineProps<{ dashboard?: boolean; compact?: boolean }>();
const statusOptions = props.dashboard ? dashboardJobStatusOptions : jobStatusOptions;
const defaultStatuses = props.dashboard ? statusOptions.map((option) => option.value) : [];
const route = useRoute();
const router = useRouter();
const [DefinePagination, PaginationControls] = createReusableTemplate<{
  position: 'top' | 'bottom';
}>();
const query = ref('');
const kind = ref(JOB_FILTER_ALL);
const statuses = ref<string[]>([]);
const filtersActive = computed(
  () => !!query.value.trim() || kind.value !== JOB_FILTER_ALL || statuses.value.length > 0,
);
const { limit, results, applyRouteLimit, changeLimit, changeOffset } = useListPagination(
  props.dashboard ? 'dashboard_jobs_page_size' : 'jobs_page_size',
  updateQuery,
);
const offset = ref(0);
const {
  items: jobs,
  total,
  isLoading: loading,
  lastError,
  load,
  runAction: runJobAction,
  remove,
  runBulk: runBulkAction,
} = useJobs();
const notify = useNotification();
const { messageFor } = useApiErrorMessage();
const loadError = computed(() =>
  messageFor(
    lastError.value?.detail,
    lastError.value instanceof Error
      ? lastError.value.message
      : `Request failed${lastError.value?.status ? ` (HTTP ${lastError.value.status})` : ''}. Try again.`,
  ),
);
const { confirmDialog } = useDialog();
const { requeue, requeueBulk } = useJobRequeue();
const { loadDetail: loadFileDetail } = useFiles();
const overlay = useOverlay();
const fileLoadingId = ref<number | null>(null);
let fileModal: ReturnType<typeof overlay.create<typeof FileDetailModal>> | null = null;
let playerModal: ReturnType<typeof overlay.create<typeof MediaPlayerModal>> | null = null;
const playerModalFileId = ref<number | null>(null);
const fileDetail = ref<MediaFileDetail | null>(null);
const fileDetailId = ref<number | null>(null);
const terminalFileRefreshes = new Set<string>();
let fileDetailLoad: {
  id: number;
  row?: MediaFileRow;
  progress: Map<number, FileJobProgress>;
} | null = null;
let disposed = false;

const openPlayer = (file: JobFile): void => {
  if (disposed) return;
  playerModal?.close();
  playerModalFileId.value = file.id;
  playerModal = overlay.create(MediaPlayerModal, {
    props: { open: true, fileId: file.id, title: file.path.split(/[\\/]/).at(-1) || file.path },
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

const loadOpenFile = async (id: number): Promise<void> => {
  const journal: NonNullable<typeof fileDetailLoad> = { id, progress: new Map() };
  fileDetailLoad = journal;
  try {
    const detail = await loadFileDetail(id);
    if (disposed || fileDetailLoad !== journal || fileDetailId.value !== id) return;
    const merged = { ...detail, ...journal.row };
    if (fileDetail.value?.id === id) retainFileJobStatus(merged, fileDetail.value);
    if (merged.active_job_id !== null) {
      const update = journal.progress.get(merged.active_job_id);
      if (update) applyFileProgress(merged, update);
    }
    fileDetail.value = merged;
  } catch (error) {
    if (!disposed && fileDetailLoad === journal)
      notify.error(error instanceof Error ? error.message : 'Could not load file.');
  } finally {
    if (fileDetailLoad === journal) fileDetailLoad = null;
  }
};
watch(
  fileDetail,
  (detail) => {
    if (detail) fileModal?.patch({ detail });
  },
  { deep: true },
);

const openFile = async (id: number): Promise<void> => {
  if (disposed || fileLoadingId.value !== null) return;
  fileLoadingId.value = id;
  fileModal?.close();
  fileModal = null;
  fileDetailId.value = id;
  fileDetail.value = null;
  terminalFileRefreshes.clear();
  try {
    await loadOpenFile(id);
    if (disposed || fileDetailId.value !== id) return;
    if (!fileDetail.value) {
      fileDetailId.value = null;
      return;
    }
    const modal = overlay.create(FileDetailModal, {
      destroyOnClose: true,
      props: {
        open: true,
        rawOpen: false,
        detail: fileDetail.value,
        readOnly: true,
        healthChecking: false,
        transcoding: false,
        'onUpdate:open': (open: boolean) => {
          if (!open) modal.close();
        },
        'onUpdate:rawOpen': (open: boolean) => modal.patch({ rawOpen: open }),
      },
    });
    fileModal = modal;
    void modal.open().result.then(() => {
      if (fileModal === modal) {
        fileModal = null;
        fileDetailId.value = null;
        fileDetail.value = null;
        fileDetailLoad = null;
      }
    });
  } catch (error) {
    if (!disposed) notify.error(error instanceof Error ? error.message : 'Could not load file.');
  } finally {
    fileLoadingId.value = null;
  }
};

const { on } = useEvents();
const stopEvents = on((event) => {
  if (
    event.type === 'file.status' &&
    (event.payload.action === 'deleted' || event.payload.action === 'pruned')
  ) {
    const id = event.payload.media_file_id;
    if (id === playerModalFileId.value) {
      playerModal?.close();
      playerModalFileId.value = null;
    }
    if (id === fileDetailId.value) {
      fileModal?.close();
      fileDetailId.value = null;
      fileDetail.value = null;
      fileDetailLoad = null;
    }
    return;
  }
  if (event.type === 'job.progress') {
    if (fileDetailLoad) {
      const update = event.payload;
      const previous = fileDetailLoad.progress.get(update.job_id);
      if (!previous || newerJobTime(update.updated_at, previous.updated_at))
        fileDetailLoad.progress.set(update.job_id, update);
    }
    if (fileDetail.value) applyFileProgress(fileDetail.value, event.payload);
    return;
  }
  if (event.type === 'file.status' && event.payload.file) {
    const file = { ...event.payload.file };
    if (fileDetailLoad?.id === file.id) fileDetailLoad.row = file;
    if (file.id !== fileDetailId.value) return;
    if (fileDetail.value) {
      retainFileJobStatus(file, fileDetail.value);
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
    if (['probed', 'probe_error'].includes(event.payload.action)) void loadOpenFile(file.id);
    return;
  }
  if (event.type === 'job.status' && fileDetail.value) {
    if (!('job' in event.payload)) {
      applyQueuedFileJob(fileDetail.value, event.payload);
      return;
    }
    const job = event.payload.job;
    if (job.file?.id !== fileDetail.value.id) return;
    retainFileJobStatus(fileDetail.value, job);
    fileDetail.value.jobs = [
      {
        id: job.id,
        kind: job.kind,
        status: job.status,
        error: job.error,
        created_at: job.created_at,
      },
      ...fileDetail.value.jobs.filter((item) => item.id !== job.id),
    ].sort((a, b) => b.id - a.id);
    if (['succeeded', 'failed', 'cancelled', 'skipped'].includes(job.status)) {
      const key = `${job.id}:${job.status}`;
      if (!terminalFileRefreshes.has(key)) {
        terminalFileRefreshes.add(key);
        void loadOpenFile(fileDetail.value.id);
      }
    }
  }
  if (event.type === 'job.deleted' && fileDetail.value) {
    fileDetail.value.jobs = fileDetail.value.jobs.filter((job) => job.id !== event.payload.job_id);
  }
});

onBeforeUnmount(() => {
  disposed = true;
  fileDetailLoad = null;
  stopEvents();
  fileModal?.close();
  playerModal?.close();
});

const selectedIds = ref<Set<number>>(new Set());
watch(
  () => jobs.value.map((job) => job.id),
  (ids) => {
    selectedIds.value = new Set([...selectedIds.value].filter((id) => ids.includes(id)));
  },
);
const bulkBusy = ref(false);
const actionLoading = ref(new Set<number>());
const actionsBlocked = computed(() => bulkBusy.value || loading.value);
const sortBy = ref('created_at');
const sortDir = ref<'asc' | 'desc'>('desc');
const selectedJobs = computed(() => jobs.value.filter((job) => selectedIds.value.has(job.id)));
const bulkActions: Array<{
  value: BulkJobAction;
  label: string;
  icon: string;
  eligibility: string;
}> = [
  {
    value: 'delete',
    label: 'Delete',
    icon: 'i-lucide-trash-2',
    eligibility: 'Pending, assigned, running, verifying, and finalizing jobs cannot be deleted.',
  },
  {
    value: 'bump',
    label: 'Move to top',
    icon: 'i-lucide-arrow-up-narrow-wide',
    eligibility: 'Only preparing and queued jobs can be moved to the top.',
  },
  {
    value: 'cancel',
    label: 'Cancel',
    icon: 'i-lucide-x',
    eligibility: 'Only active jobs, including jobs on hold, can be cancelled.',
  },
  {
    value: 'retry',
    label: 'Requeue',
    icon: 'i-lucide-rotate-cw',
    eligibility: 'Only succeeded, failed, and cancelled jobs can be requeued.',
  },
];
const availableBulkActions = computed(() =>
  bulkActions.flatMap((action) => {
    const count = selectedJobs.value.filter((job) => canJobAction(job.status, action.value)).length;
    if (!count) return [];
    const partial = count < selectedJobs.value.length;
    return [
      {
        ...action,
        count,
        partial,
        hint: partial
          ? `${count} of ${selectedJobs.value.length} selected jobs. ${action.eligibility}`
          : '',
      },
    ];
  }),
);
const bulkActionItems = computed<DropdownMenuItem[]>(() =>
  availableBulkActions.value.map((action): DropdownMenuItem => ({
    label: `${action.label}${action.partial ? ` (${action.count})` : ''}`,
    icon: action.icon,
    description: action.hint || undefined,
    color: action.value === 'delete' ? 'error' : undefined,
    disabled: actionsBlocked.value || actionLoading.value.size > 0,
    onSelect: () => void runBulk(action.value),
  })),
);
const allVisibleSelected = computed(
  () => jobs.value.length > 0 && jobs.value.every((job) => selectedIds.value.has(job.id)),
);
const clearSelection = (): void => {
  selectedIds.value = new Set();
};
const selectVisible = (): void => {
  if (actionsBlocked.value) return;
  selectedIds.value = new Set(jobs.value.map((job) => job.id));
};
const toggleSelection = (id: number, value: boolean | 'indeterminate'): void => {
  if (actionsBlocked.value) return;
  const next = new Set(selectedIds.value);
  if (value === true) next.add(id);
  else next.delete(id);
  selectedIds.value = next;
};
const queryValue = (value: unknown): string | undefined =>
  typeof value === 'string' ? value : undefined;

const sameManagedQuery = (
  source: Record<string, unknown>,
  values: Record<string, string>,
): boolean => jobQueryKeys.every((key) => source[key] === values[key]);

async function updateQuery(changes: Record<string, string>): Promise<boolean> {
  if (bulkBusy.value || ('offset' in changes && loading.value)) return false;
  return !(await router.replace({ query: updateJobRoute(route.query, changes, statusOptions) }));
}

const loadJobs = async (): Promise<void> => {
  if (bulkBusy.value) return;
  const updated = await load({
    status: (statuses.value.length ? statuses.value : defaultStatuses).join(','),
    limit: limit.value,
    offset: offset.value,
    query: query.value.trim() || undefined,
    kind: kind.value === JOB_FILTER_ALL ? undefined : kind.value,
    sort_by: sortBy.value as 'id' | 'created_at' | 'updated_at' | 'priority' | 'status' | 'path',
    sort_dir: sortDir.value,
  });
  if (updated)
    selectedIds.value = new Set(
      [...selectedIds.value].filter((id) => jobs.value.some((job) => job.id === id)),
    );
};
let mounted = false;
const refreshJobs = async (): Promise<void> => {
  await Promise.all([loadJobs(), reloadLibraries()]);
};
watch([total, offset, loading, bulkBusy], async () => {
  if (!mounted || actionsBlocked.value || lastError.value) return;
  if (offset.value > 0 && offset.value >= total.value) {
    const lastOffset = Math.max(0, Math.ceil(total.value / limit.value) - 1) * limit.value;
    await updateQuery({ offset: String(lastOffset) });
  }
});

watch(
  () => route.query,
  async (current, previous) => {
    const values = jobRouteFilters(current, statusOptions);
    if (previous && !sameManagedQuery(jobRouteFilters(previous, statusOptions), values))
      clearSelection();
    statuses.value = values.status?.split(',') ?? [];
    query.value = values.query ?? '';
    kind.value = values.kind ?? JOB_FILTER_ALL;
    sortBy.value = values.sort_by ?? 'created_at';
    sortDir.value = (values.sort_dir as 'asc' | 'desc' | undefined) ?? 'desc';
    offset.value = Number(values.offset ?? 0);
    applyRouteLimit(values.limit);
    if (!sameManagedQuery(current, values)) {
      await router.replace({
        query: updateJobRoute(current, {}, statusOptions),
      });
      return;
    }
    if (mounted) await loadJobs();
  },
  { deep: true, immediate: true },
);

watch(query, async (value) => {
  const normalized = value.trim();
  if (normalized !== (queryValue(route.query.query) ?? '').trim())
    await updateQuery({ query: normalized });
});

const {
  items: libraries,
  ensure: ensureLibraries,
  reload: reloadLibraries,
  lastError: libraryError,
} = useLibraries();

onMounted(() => {
  mounted = true;
  void loadJobs();
  void ensureLibraries();
});

const rowAction = async (job: JobRow, action: 'cancel' | 'retry' | 'bump'): Promise<void> => {
  if (actionsBlocked.value || actionLoading.value.has(job.id)) return;
  actionLoading.value = new Set(actionLoading.value).add(job.id);
  try {
    if (action === 'retry') {
      await requeue(job.id);
      return;
    }
    const result = await runJobAction(job.id, action);
    if (!result.ok) {
      notify.error(messageFor(result.error.detail, 'Request failed.'));
      return;
    }
    notify.success(
      action === 'bump'
        ? `Job #${job.id} moved to the top of the queue.`
        : `Cancellation requested for job #${job.id}.`,
    );
  } finally {
    const next = new Set(actionLoading.value);
    next.delete(job.id);
    actionLoading.value = next;
  }
};

const cancelJob = (job: JobRow): void => {
  void rowAction(job, 'cancel');
};

const retryJob = (job: JobRow): void => {
  void rowAction(job, 'retry');
};

const bumpJob = async (job: JobRow): Promise<void> => {
  await rowAction(job, 'bump');
};

const deleteJob = async (job: JobRow): Promise<void> => {
  if (actionsBlocked.value || actionLoading.value.has(job.id)) return;
  const result = await confirmDialog({
    title: 'Delete job?',
    message: `This permanently removes job #${job.id} and its history. The media file is kept.`,
    confirmText: 'Delete',
    confirmColor: 'error',
  });
  if (result.status && !actionsBlocked.value && !actionLoading.value.has(job.id)) {
    actionLoading.value = new Set(actionLoading.value).add(job.id);
    try {
      const result = await remove(job.id);
      if (result.ok) {
        selectedIds.value = remainingJobSelection(selectedIds.value, [job.id]);
        notify.success(`Deletion requested for job #${job.id}.`);
      } else {
        notify.error(messageFor(result.error.detail, 'Request failed.'));
      }
    } finally {
      const next = new Set(actionLoading.value);
      next.delete(job.id);
      actionLoading.value = next;
    }
  }
};

const runBulk = async (action: BulkJobAction): Promise<void> => {
  if (actionsBlocked.value || actionLoading.value.size > 0) return;
  const ids = jobActionIds(jobs.value, selectedIds.value, action);
  if (!ids.length) return;
  if (action !== 'bump' && action !== 'retry') {
    const label = action === 'cancel' ? 'cancel' : 'delete';
    const result = await confirmDialog({
      title: `${label[0]!.toUpperCase()}${label.slice(1)} jobs?`,
      message:
        action === 'delete'
          ? `This permanently removes ${ids.length} selected job${ids.length === 1 ? '' : 's'} and ${ids.length === 1 ? 'its' : 'their'} history. Media files are kept.`
          : `Request cancellation for ${ids.length} selected job${ids.length === 1 ? '' : 's'}?`,
      confirmText: label[0]!.toUpperCase() + label.slice(1),
      confirmColor: action === 'cancel' || action === 'delete' ? 'error' : 'primary',
    });
    if (!result.status || actionsBlocked.value) return;
  }
  bulkBusy.value = true;
  try {
    if (action === 'retry') {
      const accepted = await requeueBulk(ids);
      if (accepted) selectedIds.value = remainingJobSelection(selectedIds.value, accepted);
      return;
    }
    const result = await runBulkAction(action, ids);
    if ('error' in result) {
      notify.error(result.error.detail ?? 'Request failed.');
      return;
    }
    if ('accepted' in result) {
      const count = result.accepted.length;
      const summary = `Deletion requested for ${count} job${count === 1 ? '' : 's'}. ${result.failed.length} rejected.`;
      if (result.failed.length) {
        const first = result.failed[0]!;
        notify.error(`${summary} First rejection: #${first.id}: ${first.detail}`);
      } else if (count) notify.success(summary);
      else notify.info(summary);
      selectedIds.value = remainingJobSelection(selectedIds.value, result.accepted);
      return;
    }
    const count = result.succeeded.length;
    const summary =
      action === 'cancel'
        ? `Cancellation requested for ${count} job${count === 1 ? '' : 's'}.`
        : `${count} job${count === 1 ? '' : 's'} moved to the top of the queue.`;
    if (result.failed.length) {
      const first = result.failed[0]!;
      notify.error(
        `${summary} ${result.failed.length} failed. First failure: #${first.id}: ${first.detail}`,
      );
    } else {
      notify.success(summary);
    }
    selectedIds.value = remainingJobSelection(selectedIds.value, result.succeeded);
  } finally {
    bulkBusy.value = false;
  }
};

const detailOpen = ref(false);
const detailJobId = ref<number | null>(null);
const importedJob = ref<JobDetail | null>(null);

watch(
  () => route.query.job,
  (value) => {
    const id = Number(queryValue(value));
    if (!Number.isInteger(id) || id <= 0) return;
    importedJob.value = null;
    detailJobId.value = id;
    detailOpen.value = true;
  },
  { immediate: true },
);
const importInput = ref<HTMLInputElement | null>(null);
const openDetail = (job: JobRow): void => {
  if (bulkBusy.value) return;
  importedJob.value = null;
  detailJobId.value = job.id;
  detailOpen.value = true;
};
const importJob = async (event: Event): Promise<void> => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;
  try {
    const job: unknown = JSON.parse(await file.text());
    if (!validJobExport(job)) throw new Error();
    importedJob.value = job;
    detailJobId.value = job.id;
    detailOpen.value = true;
  } catch {
    notify.error('Invalid job export file.');
  }
};
</script>
