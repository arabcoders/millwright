<template>
  <div class="space-y-6">
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
        @update:limit="updateQuery({ limit: String($event) })"
        @update:offset="updateQuery({ offset: String($event) })"
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
        <UTooltip
          v-for="action in availableBulkActions"
          :key="action.value"
          :text="action.hint"
          :disabled="!action.hint"
        >
          <UButton
            size="xs"
            color="neutral"
            variant="outline"
            :loading="bulkBusy"
            :disabled="actionsBlocked"
            :icon="action.icon"
            @click="runBulk(action.value)"
            >{{ action.label }}{{ action.partial ? ` (${action.count})` : '' }}</UButton
          >
        </UTooltip>
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
            @click="loadJobs"
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

    <ListFilters
      v-model:query="query"
      search-placeholder="Search path, job ID, or file_id:123..."
      :sort-by="sortBy"
      :sort-dir="sortDir"
      :sort-options="jobSortOptions"
      sort-label="Sort jobs"
      :disabled="bulkBusy"
      @update:sort-by="updateQuery({ sort_by: $event })"
      @update:sort-dir="updateQuery({ sort_dir: $event })"
    >
      <USelectMenu
        value-key="value"
        v-model="kind"
        :items="jobKindOptions"
        :disabled="bulkBusy"
        size="sm"
        class="w-full sm:w-auto sm:min-w-40"
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
        class="w-full sm:w-64"
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
          @click="loadJobs"
          >Refresh</UButton
        >
      </template>
    </ListFilters>
    <div class="space-y-3">
      <PaginationControls position="top" />
      <div
        v-if="loading && !jobs.length"
        class="grid grid-cols-1 gap-3 @min-[40rem]:grid-cols-2 @min-[64rem]:grid-cols-3"
        aria-label="Loading jobs"
      >
        <USkeleton v-for="n in 6" :key="n" class="h-64 rounded-xl" />
      </div>
      <div
        v-else-if="jobs.length"
        class="grid grid-cols-1 gap-3 @min-[40rem]:grid-cols-2 @min-[64rem]:grid-cols-3"
      >
        <DeferredContent
          v-for="job in jobs"
          :key="job.id"
          :min-height="compact ? 208 : 384"
          class="min-w-0"
        >
          <JobCard
            :job="job"
            :libraries="libraries"
            :compact="compact"
            selectable
            :selected="selectedIds.has(job.id)"
            :busy="loading || bulkBusy"
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
import { useDialog } from '~/composables/useDialog';
import { useJobRequeue } from '~/composables/useJobRequeue';
import { useLibraries } from '~/composables/useLibraries';
import { useJobs, type BulkJobAction } from '~/composables/useJobs';
import { useFiles } from '~/composables/useFiles';
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
import type { JobDetail, JobFile, JobRow } from '~/types/api';

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
const limit = ref(50);
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

const openFile = async (id: number): Promise<void> => {
  if (disposed || fileLoadingId.value !== null) return;
  fileLoadingId.value = id;
  try {
    const detail = await loadFileDetail(id);
    if (disposed) return;
    fileModal?.close();
    const modal = overlay.create(FileDetailModal, {
      destroyOnClose: true,
      props: {
        open: true,
        rawOpen: false,
        detail,
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
      if (fileModal === modal) fileModal = null;
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
    event.payload.media_file_id === playerModalFileId.value &&
    (event.payload.action === 'deleted' || event.payload.action === 'pruned')
  ) {
    playerModal?.close();
    playerModalFileId.value = null;
  }
});

onBeforeUnmount(() => {
  disposed = true;
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

const updateQuery = async (changes: Record<string, string>): Promise<void> => {
  if (bulkBusy.value || ('offset' in changes && loading.value)) return;
  await router.replace({
    query: updateJobRoute(route.query, changes, statusOptions),
  });
};

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
    limit.value = Number(values.limit ?? 50);
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

const { items: libraries, ensure: ensureLibraries } = useLibraries();

onMounted(() => {
  mounted = true;
  void loadJobs();
  void ensureLibraries();
});

const rowAction = async (job: JobRow, action: 'cancel' | 'retry' | 'bump'): Promise<void> => {
  if (actionsBlocked.value) return;
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
  if (actionsBlocked.value) return;
  const result = await confirmDialog({
    title: 'Delete job?',
    message: `This permanently removes job #${job.id} and its history. The media file is kept.`,
    confirmText: 'Delete',
    confirmColor: 'error',
  });
  if (result.status && !actionsBlocked.value) {
    const result = await remove(job.id);
    if (result.ok) {
      selectedIds.value = remainingJobSelection(selectedIds.value, [job.id]);
      notify.success(`Deletion requested for job #${job.id}.`);
    } else {
      notify.error(messageFor(result.error.detail, 'Request failed.'));
    }
  }
};

const runBulk = async (action: BulkJobAction): Promise<void> => {
  if (actionsBlocked.value) return;
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
