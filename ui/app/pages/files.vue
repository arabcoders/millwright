<template>
  <main class="@container w-full min-w-0 max-w-full space-y-6">
    <DefinePagination v-slot="{ position }">
      <ListPagination
        v-if="!lastError && (files.length || offset > 0)"
        label="Files"
        :position="position"
        :limit="limit"
        :offset="offset"
        :total="total"
        :shown="files.length"
        :disabled="loading || actionBusy"
        @update:limit="changeLimit($event, position)"
        @update:offset="changeOffset($event, position)"
      >
        <UButton
          v-if="files.length"
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
          v-if="selectedIds.size"
          :items="bulkActionItems"
          :modal="false"
          :ui="{ content: 'w-80', itemDescription: 'whitespace-normal' }"
        >
          <UButton
            size="sm"
            color="neutral"
            variant="outline"
            :loading="actionBusy"
            :disabled="actionsBlocked"
            icon="i-lucide-list"
            trailing-icon="i-lucide-chevron-down"
            >Actions</UButton
          >
        </UDropdownMenu>
      </ListPagination>
    </DefinePagination>
    <PageHeader page="files">
      <template #actions>
        <UTooltip v-if="rescanDisabled" text="Pick a library first">
          <span class="inline-flex">
            <UButton
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-radar"
              aria-label="Rescan library"
              :disabled="rescanDisabled"
              :loading="scanning"
              @click="rescanLibrary"
            >
              Rescan
            </UButton>
          </span>
        </UTooltip>
        <UButton
          v-else
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-radar"
          aria-label="Rescan library"
          :loading="scanning"
          :disabled="actionBusy"
          @click="rescanLibrary"
        >
          Rescan
        </UButton>

        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-refresh-cw"
          aria-label="Refresh files"
          :loading="loading"
          :disabled="actionBusy"
          @click="refreshFiles"
        >
          Refresh
        </UButton>
      </template>
    </PageHeader>

    <UAlert
      v-if="lastError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load files"
      :description="lastError.message"
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
      search-placeholder="Search files or file_id:123..."
      :sort-by="sortBy"
      :sort-dir="sortDir"
      :sort-options="sortOptions"
      sort-label="Sort files"
      :filters-active="filtersActive"
      :disabled="actionBusy"
      @update:sort-by="updateQuery({ sort_by: $event })"
      @update:sort-dir="updateQuery({ sort_dir: $event })"
      @clear-filters="
        updateQuery({ query: '', library_id: FILTER_ALL, transcode_status: '', health_status: '' })
      "
    >
      <USelectMenu
        value-key="value"
        v-model="libraryId"
        :disabled="actionBusy"
        :items="libraryOptions"
        size="sm"
        class="w-full sm:w-48"
        aria-label="Filter by library"
        @update:model-value="updateQuery({ library_id: libraryId })"
      />
      <USelectMenu
        value-key="value"
        v-model="transcodeStates"
        :disabled="actionBusy"
        :items="transcodeOptions"
        multiple
        placeholder="All transcode states"
        :clear="{ 'aria-label': 'Clear transcode status filter' }"
        size="sm"
        class="w-full sm:w-56"
        aria-label="Filter by transcode status"
        @update:model-value="updateQuery({ transcode_status: transcodeStates.join(',') })"
      />
      <USelectMenu
        value-key="value"
        v-model="healthStates"
        :disabled="actionBusy"
        :items="healthOptions"
        multiple
        placeholder="All health states"
        :clear="{ 'aria-label': 'Clear health status filter' }"
        size="sm"
        class="w-full sm:w-48"
        aria-label="Filter by health status"
        @update:model-value="updateQuery({ health_status: healthStates.join(',') })"
      />
    </ListFilters>

    <div ref="results" class="min-w-0 scroll-mt-4 space-y-4">
      <PaginationControls position="top" />
      <div
        v-if="loading && !files.length"
        class="mw-card divide-y divide-default"
        aria-label="Loading files"
      >
        <div v-for="item in 6" :key="item" class="space-y-3 p-4 sm:p-5">
          <USkeleton class="h-5 w-2/3" />
          <USkeleton class="h-4 w-1/3" />
          <USkeleton class="h-20 w-full" />
        </div>
      </div>
      <div
        v-else-if="files.length"
        class="mw-card divide-y divide-default"
        :aria-busy="loading || actionBusy"
      >
        <DeferredContent
          v-for="file in files"
          :key="file.id"
          :min-height="256"
          class="w-full min-w-0 max-w-full"
        >
          <FileCard
            :file="file"
            :libraries="libraries"
            selectable
            :selected="selectedIds.has(file.id)"
            :busy="loading || actionBusy"
            :refreshing-metadata="refreshingMetadata === file.id"
            :scanner-paused="scanner?.paused"
            @open="openDetail(file)"
            @select="toggleSelection(file.id, $event)"
            @play="openPlayer(file.id, file.path)"
            @refresh-metadata="refreshMetadata(file)"
            @skip-cooldown="skipCooldown(file)"
            @health-check="queueHealthCheck(file)"
            @transcode="queueTranscode(file)"
            @dry-run="openDryRun(file)"
          />
        </DeferredContent>
      </div>
      <UEmpty
        v-else-if="!lastError"
        title="No files match the current filters."
        icon="i-lucide-folder-open"
      />

      <PaginationControls position="bottom" />
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { createReusableTemplate, useNow } from '@vueuse/core';
import type { DropdownMenuItem } from '@nuxt/ui';
import FileCard from '~/components/FileCard.vue';
import FileDetailModal from '~/components/FileDetailModal.vue';
import MediaPlayerModal from '~/components/MediaPlayerModal.vue';
import BatchDryRunModal from '~/components/BatchDryRunModal.vue';
import ListFilters from '~/components/ListFilters.vue';
import ListPagination from '~/components/ListPagination.vue';
import { useEvents } from '~/composables/useEvents';
import {
  canSkipCooldown,
  refreshMetadataPrerequisite,
  manualTranscodePrerequisite,
  transcodePrerequisite,
  transcodeWarnings,
  useFiles,
} from '~/composables/useFiles';
import { useFileDryRun } from '~/composables/useFileDryRun';
import { useLibraries } from '~/composables/useLibraries';
import { useLibraryProgress } from '~/composables/useLibraryProgress';
import { useListPagination } from '~/composables/useListPagination';
import {
  applyFileProgress,
  applyQueuedFileJob,
  newerJobTime,
  retainFileJobStatus,
  type FileJobProgress,
} from '~/composables/stateHelpers';
import type { BulkFileAction, MediaFileDetail, MediaFileRow } from '~/types/api';
import { parseStatuses, statusInfo, statusOptions } from '~/utils/status';
import { listPageSize } from '~/utils/listPagination';

const {
  items: files,
  total,
  isLoading: loading,
  lastError,
  load: loadFilesRequest,
  queueHealthCheck,
  refreshMetadata,
  skipCooldown,
  refreshingMetadata,
  queueTranscode,
  queueBulk,
  actionBusy,
  loadDetail: loadFileDetail,
} = useFiles();
const {
  items: libraries,
  ensure: ensureLibraries,
  reload: reloadLibraries,
  lastError: libraryError,
  scan,
} = useLibraries();
const { scanner } = useLibraryProgress();
const notify = useNotification();
const [DefinePagination, PaginationControls] = createReusableTemplate<{
  position: 'top' | 'bottom';
}>();

const FILTER_ALL = '__all__';

const selectedIds = ref<Set<number>>(new Set());
const actionsBlocked = computed(() => actionBusy.value || loading.value);
const selectedFiles = computed(() => files.value.filter((file) => selectedIds.value.has(file.id)));
const now = useNow({ interval: 1000 });
const selectedCooldownFiles = computed(() =>
  selectedFiles.value.filter((file) => canSkipCooldown(file, now.value.getTime())),
);
const allVisibleSelected = computed(
  () => files.value.length > 0 && files.value.every((file) => selectedIds.value.has(file.id)),
);
const selectedTranscodable = computed(() =>
  selectedFiles.value.filter((file) => !transcodePrerequisite(file, libraries.value)),
);
const transcodeExclusions = computed(() => transcodeWarnings(selectedFiles.value, libraries.value));
const bulkActionItems = computed<DropdownMenuItem[]>(() => {
  const items: DropdownMenuItem[] = [];
  if (selectedCooldownFiles.value.length) {
    const partial = selectedCooldownFiles.value.length < selectedFiles.value.length;
    items.push({
      label: `Skip cooldown${partial ? ` (${selectedCooldownFiles.value.length})` : ''}`,
      icon: 'i-lucide-timer-off',
      description: partial
        ? 'Only selected files with a skippable cooldown are included.'
        : undefined,
      disabled: actionsBlocked.value,
      onSelect: () => void runBulk('skip_cooldown'),
    });
  }
  items.push({
    label: 'Health check',
    icon: 'i-lucide-heart-pulse',
    disabled: actionsBlocked.value,
    onSelect: () => void runBulk('healthcheck'),
  });
  if (selectedTranscodable.value.length)
    items.push({
      label: `Transcode${transcodeExclusions.value.length ? ` (${selectedTranscodable.value.length})` : ''}`,
      icon: 'i-lucide-film',
      description: transcodeExclusions.value.join(' ') || undefined,
      disabled: actionsBlocked.value,
      onSelect: () => void runBulk('transcode'),
    });
  items.push({
    label: 'Dry run',
    icon: 'i-lucide-play',
    disabled: actionsBlocked.value,
    onSelect: () => openSelectedDryRun(),
  });
  return items;
});
const clearSelection = (): void => {
  selectedIds.value = new Set();
};
const selectVisible = (): void => {
  if (!actionsBlocked.value) selectedIds.value = new Set(files.value.map((file) => file.id));
};
const toggleSelection = (id: number, value: boolean | 'indeterminate'): void => {
  if (actionsBlocked.value) return;
  const next = new Set(selectedIds.value);
  if (value === true) next.add(id);
  else next.delete(id);
  selectedIds.value = next;
};
watch(
  () => files.value.map((file) => file.id),
  (ids) => {
    selectedIds.value = new Set([...selectedIds.value].filter((id) => ids.includes(id)));
  },
);
const runBulk = async (action: BulkFileAction): Promise<void> => {
  if (actionsBlocked.value) return;
  const result = await queueBulk(
    action,
    action === 'skip_cooldown' ? selectedCooldownFiles.value : selectedFiles.value,
  );
  if (!result) return;
  const completed = new Set(result.accepted.map((item) => item.file_id));
  selectedIds.value = new Set([...selectedIds.value].filter((id) => !completed.has(id)));
};

const query = ref('');
const libraryId = ref(FILTER_ALL);
const transcodeStates = ref<string[]>([]);
const healthStates = ref<string[]>([]);
const filtersActive = computed(
  () =>
    !!query.value.trim() ||
    libraryId.value !== FILTER_ALL ||
    transcodeStates.value.length > 0 ||
    healthStates.value.length > 0,
);
const { limit, results, applyRouteLimit, changeLimit, changeOffset } = useListPagination(
  'files_page_size',
  updateQuery,
);
const offset = ref(0);
const sortBy = ref('id');
const sortDir = ref<'asc' | 'desc'>('desc');
const sortOptions = [
  { label: 'ID', value: 'id' },
  { label: 'Name / Path', value: 'path' },
  { label: 'File size', value: 'size' },
  { label: 'Duration', value: 'duration_s' },
  { label: 'Overall bitrate', value: 'bit_rate' },
  { label: 'Added', value: 'created_at' },
  { label: 'Updated', value: 'updated_at' },
  { label: 'Transcode status', value: 'transcode_status' },
  { label: 'Health status', value: 'health_status' },
];

const libraryOptions = computed(() => [
  { label: 'All libraries', value: FILTER_ALL },
  ...libraries.value.map((lib) => ({ label: lib.name, value: String(lib.id) })),
]);

const transcodeOptions = statusOptions('media').slice(1);

const healthOptions = ['pending', 'healthy', 'corrupt'].map((value) => ({
  label: statusInfo(value, 'health').label,
  value,
}));

const route = useRoute();
const router = useRouter();
const managedQueryKeys = [
  'query',
  'library_id',
  'transcode_status',
  'health_status',
  'offset',
  'limit',
  'sort_by',
  'sort_dir',
];

const queryValue = (value: unknown): string | undefined =>
  typeof value === 'string' ? value : undefined;

const positiveInteger = (value: string | undefined): string | undefined =>
  value && /^[1-9]\d*$/.test(value) ? value : undefined;

const nonnegativeInteger = (value: string | undefined): string | undefined => {
  if (!value || !/^\d+$/.test(value)) return undefined;
  return value.replace(/^0+(?=\d)/, '');
};

const routeFilters = (source: Record<string, unknown>): Record<string, string> => {
  const values: Record<string, string> = {};
  const search = queryValue(source.query)?.trim();
  const library = positiveInteger(queryValue(source.library_id));
  const transcode = parseStatuses(source.transcode_status, transcodeOptions).join(',');
  const health = parseStatuses(source.health_status, healthOptions).join(',');
  const pageOffset = nonnegativeInteger(queryValue(source.offset));
  const pageLimit = queryValue(source.limit);
  const sort = queryValue(source.sort_by);
  const direction = queryValue(source.sort_dir);
  if (search) values.query = search;
  if (library) values.library_id = library;
  if (transcode) values.transcode_status = transcode;
  if (health) values.health_status = health;
  if (pageOffset && pageOffset !== '0') values.offset = pageOffset;
  if (pageLimit && listPageSize(pageLimit)) values.limit = pageLimit;
  if (sort && sortOptions.some((option) => option.value === sort)) values.sort_by = sort;
  if (direction === 'asc' || direction === 'desc') values.sort_dir = direction;
  return values;
};

const sameManagedQuery = (
  source: Record<string, unknown>,
  values: Record<string, string>,
): boolean => managedQueryKeys.every((key) => source[key] === values[key]);

async function updateQuery(changes: Record<string, string>): Promise<boolean> {
  if (actionBusy.value || ('offset' in changes && loading.value)) return false;
  const next = { ...route.query } as Record<string, string | undefined>;
  for (const [key, value] of Object.entries(changes)) {
    if (value && value !== FILTER_ALL) next[key] = value;
    else next[key] = undefined;
  }
  if (!('offset' in changes) && managedQueryKeys.some((key) => key in changes)) {
    delete next.offset;
  }
  return !(await router.replace({ query: next }));
}

const loadFiles = async (): Promise<void> => {
  await loadFilesRequest({
    query: query.value.trim() || undefined,
    library_id: libraryId.value === FILTER_ALL ? undefined : libraryId.value,
    transcode_status: transcodeStates.value.join(',') || undefined,
    health_status: healthStates.value.join(',') || undefined,
    limit: limit.value,
    offset: offset.value,
    sort_by: sortBy.value as
      | 'id'
      | 'path'
      | 'size'
      | 'duration_s'
      | 'bit_rate'
      | 'transcode_status'
      | 'health_status'
      | 'created_at'
      | 'updated_at',
    sort_dir: sortDir.value,
  }).catch(() => {});
};
const refreshFiles = async (): Promise<void> => {
  await Promise.all([loadFiles(), reloadLibraries()]);
};

const rescanDisabled = computed(() => libraryId.value === FILTER_ALL);
const scanning = ref(false);

const rescanLibrary = async (): Promise<void> => {
  if (actionBusy.value) return;
  const id = libraryId.value;
  if (id === FILTER_ALL) return;
  const name = libraries.value.find((lib) => String(lib.id) === id)?.name ?? `#${id}`;

  scanning.value = true;
  try {
    await scan({ id: Number(id), name });
  } finally {
    scanning.value = false;
  }
};

const { on } = useEvents();
let mounted = false;

watch([total, offset, loading, actionBusy], async () => {
  if (!mounted || loading.value || actionBusy.value || lastError.value) return;
  if (offset.value > 0 && offset.value >= total.value) {
    const lastOffset = Math.max(0, Math.ceil(total.value / limit.value) - 1) * limit.value;
    await updateQuery({ offset: String(lastOffset) });
  }
});

onMounted(() => {
  mounted = true;
  void loadFiles();
  void ensureLibraries().catch(() => {});
});

watch(
  () => route.query,
  async (current) => {
    clearSelection();
    const values = routeFilters(current);
    query.value = values.query ?? '';
    libraryId.value = values.library_id ?? FILTER_ALL;
    transcodeStates.value = values.transcode_status?.split(',') ?? [];
    healthStates.value = values.health_status?.split(',') ?? [];
    offset.value = Number(values.offset ?? 0);
    applyRouteLimit(values.limit);
    sortBy.value = values.sort_by ?? 'id';
    sortDir.value = (values.sort_dir as 'asc' | 'desc' | undefined) ?? 'desc';
    if (!sameManagedQuery(current, values)) {
      const next = { ...route.query } as Record<string, string | undefined>;
      for (const key of managedQueryKeys) {
        if (values[key]) next[key] = values[key];
        else next[key] = undefined;
      }
      await router.replace({ query: next });
      return;
    }
    if (mounted) await loadFiles();
  },
  { deep: true, immediate: true },
);

watch(query, async (value) => {
  const normalized = value.trim();
  if (normalized !== (queryValue(route.query.query) ?? '').trim())
    await updateQuery({ query: normalized });
});

const terminalDetailRefreshes = new Set<string>();
let detailLoad: {
  id: number;
  row?: MediaFileRow;
  progress: Map<number, FileJobProgress>;
} | null = null;
const stopEvents = on((event) => {
  if (
    event.type === 'file.status' &&
    event.payload.media_file_id !== undefined &&
    (event.payload.action === 'deleted' || event.payload.action === 'pruned')
  ) {
    const id = event.payload.media_file_id;
    if (detailFileId.value === id) updateDetailOpen(false);
    if (playerModalFileId.value === id) {
      playerModal?.close();
      playerModalFileId.value = null;
    }
    return;
  }
  if (event.type === 'job.progress') {
    if (detailLoad) {
      const update = event.payload;
      const previous = detailLoad.progress.get(update.job_id);
      if (!previous || newerJobTime(update.updated_at, previous.updated_at))
        detailLoad.progress.set(update.job_id, update);
    }
    if (detailOpen.value && detail.value) applyFileProgress(detail.value, event.payload);
    return;
  }
  if (
    event.type === 'file.status' &&
    event.payload.file &&
    detailLoad?.id === event.payload.file.id
  )
    detailLoad.row = event.payload.file;
  if (event.type === 'library.scan' && event.payload.reason === 'manual') {
    const { payload } = event;
    if (payload.status === 'succeeded') {
      notify.success(
        `Scan of "${payload.library_name}" finished: ${payload.added} added, ${payload.updated} updated, ${payload.unchanged} unchanged.`,
      );
      if (payload.error_count) {
        notify.warning(
          `Scan reported ${payload.error_count} error${payload.error_count === 1 ? '' : 's'}: ${payload.error_samples[0] ?? 'unknown error'}.`,
        );
      }
    } else if (payload.status === 'cancelled') {
      notify.info(`Scan of "${payload.library_name}" cancelled. Files already found are kept.`);
    } else if (payload.status === 'failed') {
      notify.error(`Scan of "${payload.library_name}" failed: ${payload.error}`);
    }
    return;
  }
  if (
    event.type === 'file.status' &&
    event.payload.file &&
    detailOpen.value &&
    event.payload.file.id === detailFileId.value
  ) {
    const file = { ...event.payload.file };
    if (detail.value) {
      retainFileJobStatus(file, detail.value);
      if (detail.value.active_job_id !== null && detail.value.active_job_updated_at !== null) {
        applyFileProgress(file, {
          job_id: detail.value.active_job_id,
          progress: detail.value.active_job_progress,
          updated_at: detail.value.active_job_updated_at,
        });
      }
      Object.assign(detail.value, file);
    }
    if (['probed', 'probe_error'].includes(event.payload.action)) void loadDetail(file.id);
    return;
  }
  if (event.type === 'job.status' && detailOpen.value && detail.value) {
    if (!('job' in event.payload)) {
      applyQueuedFileJob(detail.value, event.payload);
      return;
    }
    const file = event.payload.job.file;
    if (!file || file.id !== detail.value.id) return;
    const job = event.payload.job;
    retainFileJobStatus(detail.value, job);
    const summary = {
      id: job.id,
      kind: job.kind,
      status: job.status,
      error: job.error,
      created_at: job.created_at,
    };
    detail.value.jobs = [summary, ...detail.value.jobs.filter((item) => item.id !== job.id)].sort(
      (a, b) => b.id - a.id,
    );
    if (['succeeded', 'failed', 'cancelled', 'skipped'].includes(event.payload.status)) {
      const key = `${event.payload.job_id}:${event.payload.status}`;
      if (!terminalDetailRefreshes.has(key)) {
        terminalDetailRefreshes.add(key);
        void loadDetail(detail.value.id);
      }
    }
  }
  if (event.type === 'job.deleted' && detail.value)
    detail.value.jobs = detail.value.jobs.filter((job) => job.id !== event.payload.job_id);
});

onBeforeUnmount(() => stopEvents());

const detailOpen = ref(false);
const detail = ref<MediaFileDetail | null>(null);
const detailFileId = ref<number | null>(null);
const healthChecking = ref(false);
const rawOpen = ref(false);

const fileName = (path: string): string => path.split(/[\\/]/).at(-1) || path;

const {
  isOpen: dryRunOpen,
  fileId: dryFileId,
  open: openDryRun,
} = useFileDryRun((id) => openDetail({ id }));

const openDryRunDetail = (): void => {
  if (!detail.value) return;
  if (dryRunOpen.value && dryFileId.value === detail.value.id) {
    updateDetailOpen(false);
    return;
  }
  void openDryRun(detail.value);
};

const openDetail = (file: Pick<MediaFileRow, 'id'>): void => {
  if (actionBusy.value) return;
  terminalDetailRefreshes.clear();
  detailFileId.value = file.id;
  detail.value = null;
  detailOpen.value = true;
  detailModal?.close();
  detailModal = overlay.create(FileDetailModal, {
    props: detailProps.value,
    destroyOnClose: true,
  });
  const modal = detailModal;
  void modal.open().result.then(() => {
    if (detailModal === modal) detailModal = null;
  });
  void loadDetail(file.id);
};

const loadDetail = async (id: number): Promise<void> => {
  const journal: NonNullable<typeof detailLoad> = { id, progress: new Map() };
  detailLoad = journal;
  try {
    const file = await loadFileDetail(id);
    if (detailLoad === journal && detailOpen.value && detailFileId.value === id) {
      const merged = { ...file, ...journal.row };
      if (detail.value?.id === id) retainFileJobStatus(merged, detail.value);
      if (merged.active_job_id !== null) {
        const update = journal.progress.get(merged.active_job_id);
        if (update) applyFileProgress(merged, update);
      }
      detail.value = merged;
    }
  } catch (error) {
    if (detailLoad === journal && detailOpen.value && detailFileId.value === id) {
      updateDetailOpen(false);
      notify.error(error instanceof Error ? error.message : 'Could not load file.');
    }
  } finally {
    if (detailLoad === journal) detailLoad = null;
  }
};

const updateDetailOpen = (open: boolean): void => {
  detailOpen.value = open;
  if (open) return;
  detailFileId.value = null;
  detailLoad = null;
  detail.value = null;
  rawOpen.value = false;
  healthChecking.value = false;
  transcoding.value = false;
};

const runHealthCheck = async (): Promise<void> => {
  if (!detail.value || healthChecking.value || transcoding.value) return;
  healthChecking.value = true;
  try {
    if (await queueHealthCheck(detail.value)) detailOpen.value = false;
  } finally {
    healthChecking.value = false;
  }
};

const transcoding = ref(false);

const runTranscode = async (): Promise<void> => {
  if (!detail.value || transcoding.value || healthChecking.value) return;
  transcoding.value = true;
  try {
    if (await queueTranscode(detail.value)) detailOpen.value = false;
  } finally {
    transcoding.value = false;
  }
};

const overlay = useOverlay();
let batchModal: ReturnType<typeof overlay.create<typeof BatchDryRunModal>> | null = null;
const openSelectedDryRun = (): void => {
  if (actionsBlocked.value || !selectedFiles.value.length) return;
  batchModal?.close();
  const modal = overlay.create(BatchDryRunModal, {
    destroyOnClose: true,
    props: {
      open: true,
      scope: { files: selectedFiles.value.map((file) => ({ ...file })) },
      'onUpdate:open': (open: boolean) => {
        if (!open) modal.close();
      },
    },
  });
  batchModal = modal;
  void modal.open().result.then(() => {
    if (batchModal === modal) batchModal = null;
  });
};
let detailModal: ReturnType<typeof overlay.create<typeof FileDetailModal>> | null = null;
let playerModal: ReturnType<typeof overlay.create<typeof MediaPlayerModal>> | null = null;
const playerModalFileId = ref<number | null>(null);

const openPlayer = (fileId: number, path: string): void => {
  playerModal?.close();
  playerModalFileId.value = fileId;
  playerModal = overlay.create(MediaPlayerModal, {
    props: { open: true, fileId, title: fileName(path) },
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

const openDetailPlayer = (): void => {
  if (detail.value) openPlayer(detail.value.id, detail.value.path);
};

const detailProps = computed(() => ({
  open: detailOpen.value,
  rawOpen: rawOpen.value,
  detail: detail.value,
  transcodeDisabledReason: detail.value
    ? manualTranscodePrerequisite(detail.value, libraries.value)
    : null,
  healthChecking: healthChecking.value || actionBusy.value,
  transcoding: transcoding.value,
  refreshingMetadata: refreshingMetadata.value === detail.value?.id,
  refreshDisabledReason: detail.value
    ? refreshMetadataPrerequisite(detail.value, libraries.value, !!scanner.value?.paused)
    : null,
  'onUpdate:open': updateDetailOpen,
  'onUpdate:rawOpen': (open: boolean) => (rawOpen.value = open),
  onTranscode: runTranscode,
  onHealthCheck: runHealthCheck,
  onRefreshMetadata: () => {
    if (detail.value) void refreshMetadata(detail.value);
  },
  onSkipCooldown: () => {
    if (detail.value) void skipCooldown(detail.value);
  },
  onDryRun: openDryRunDetail,
  onPlay: openDetailPlayer,
}));

watch(detailProps, (props) => detailModal?.patch(props));
watch(detailOpen, (open) => {
  if (!open) detailModal?.close();
});

onBeforeUnmount(() => {
  batchModal?.close();
  detailModal?.close();
  playerModal?.close();
});
</script>
