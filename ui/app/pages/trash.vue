<template>
  <main class="@container w-full min-w-0 max-w-full space-y-6">
    <DefinePagination v-slot="{ position }">
      <ListPagination
        v-if="!lastError && (entries.length || offset > 0)"
        label="Trash"
        :position="position"
        :limit="limit"
        :offset="offset"
        :total="total"
        :shown="entries.length"
        :disabled="busy"
        @update:limit="changeLimit($event, position)"
        @update:offset="changeOffset($event, position)"
      >
        <UButton
          v-if="entries.length"
          size="sm"
          color="neutral"
          variant="outline"
          :icon="pageSelected ? 'i-lucide-square-check' : 'i-lucide-square'"
          :disabled="actionsBlocked"
          @click="pageSelected ? clearSelection() : selectVisible()"
          >{{ pageSelected ? 'Clear selection' : 'Select page' }}</UButton
        >
        <UBadge v-if="selected.size" color="primary" variant="subtle"
          >{{ selected.size }} selected</UBadge
        >
        <UButton
          v-if="selected.size"
          size="sm"
          color="neutral"
          variant="outline"
          icon="i-lucide-trash-2"
          :loading="batchDeleting"
          :disabled="actionsBlocked"
          @click="deleteSelected"
          >Delete</UButton
        >
      </ListPagination>
    </DefinePagination>
    <PageHeader page="trash">
      <template #actions>
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-refresh-cw"
          aria-label="Refresh trash"
          :loading="loading"
          :disabled="mutationBusy"
          @click="refreshTrash"
        >
          Refresh
        </UButton>
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-trash-2"
          :loading="emptying"
          :disabled="actionsBlocked || total === 0"
          @click="emptyTrash"
        >
          Empty trash
        </UButton>
      </template>
    </PageHeader>

    <UAlert
      v-if="lastError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load trash"
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
      search-placeholder="Search trash..."
      :sort-by="sortBy"
      :sort-dir="sortDir"
      :sort-options="trashSortOptions"
      sort-label="Sort trash"
      :filters-active="filtersActive"
      :disabled="mutationBusy"
      @update:sort-by="updateQuery({ sort_by: $event })"
      @update:sort-dir="updateQuery({ sort_dir: $event })"
      @clear-filters="updateQuery({ query: '', library_id: TRASH_FILTER_ALL })"
    >
      <USelectMenu
        value-key="value"
        v-model="libraryId"
        :items="libraryOptions"
        :disabled="mutationBusy"
        size="sm"
        class="w-full sm:w-48"
        aria-label="Filter by library"
        @update:model-value="updateQuery({ library_id: libraryId })"
      />
      <template #trailing>
        <UBadge class="ml-auto self-center" color="neutral" variant="subtle"
          >{{ formatBytes(totalSizeBytes) }} in trash</UBadge
        >
      </template>
    </ListFilters>

    <div ref="results" class="scroll-mt-4 space-y-3">
      <PaginationControls position="top" />
      <div v-if="loading && !entries.length" class="space-y-3" aria-label="Loading trash">
        <USkeleton v-for="index in 6" :key="index" class="h-36 rounded-lg" />
      </div>
      <UEmpty
        v-else-if="entries.length === 0 && !lastError"
        :icon="query || libraryId !== TRASH_FILTER_ALL ? 'i-lucide-search-x' : 'i-lucide-trash-2'"
        :title="
          query || libraryId !== TRASH_FILTER_ALL
            ? 'No trash matches the current filters.'
            : 'Trash is empty.'
        "
      />
      <div v-else-if="entries.length" class="mw-card divide-y divide-default" :aria-busy="loading">
        <DeferredContent v-for="entry in entries" :key="entry.id" :min-height="144" class="min-w-0">
          <article
            class="grid min-w-0 gap-4 p-4 sm:p-5 xl:grid-cols-[minmax(0,1fr)_22rem_auto]"
            :class="{ 'bg-primary/5': selected.has(entry.id) }"
          >
            <div class="flex min-w-0 items-start gap-3">
              <div class="min-w-0 flex-1">
                <h2 class="min-w-0">
                  <UTooltip :text="entry.original_path" :content="{ align: 'start' }">
                    <button
                      v-if="entry.current_file_id !== null"
                      type="button"
                      :aria-label="`Open details for ${fileName(entry.original_path)}`"
                      :disabled="mutationBusy"
                      @click="openDetail(entry.current_file_id)"
                      class="w-full wrap-anywhere text-left text-base font-semibold text-highlighted hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                    >
                      {{ fileName(entry.original_path) }}
                    </button>
                    <span v-else class="wrap-anywhere text-base font-semibold text-highlighted">{{
                      fileName(entry.original_path)
                    }}</span>
                  </UTooltip>
                </h2>
                <p class="mt-2 flex min-w-0 items-start gap-1.5 text-xs text-toned">
                  <UIcon
                    name="i-lucide-folder"
                    class="mt-0.5 size-3.5 shrink-0"
                    aria-hidden="true"
                  />
                  <span class="wrap-anywhere font-mono">{{
                    trashDirectory(entry.original_path)
                  }}</span>
                </p>
              </div>
            </div>
            <dl class="grid min-w-0 grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
              <div class="min-w-0 space-y-1">
                <dt class="flex items-center gap-1.5 text-xs text-muted">
                  <UIcon name="i-lucide-hard-drive" class="size-3.5 shrink-0" aria-hidden="true" />
                  File size
                </dt>
                <dd class="tabular-nums">
                  {{ formatBytes(entry.size_bytes) }}
                </dd>
              </div>
              <div class="min-w-0 space-y-1">
                <dt class="flex items-center gap-1.5 text-xs text-muted">
                  <UIcon name="i-lucide-clock-3" class="size-3.5 shrink-0" aria-hidden="true" />
                  Trashed
                </dt>
                <dd>
                  <UTooltip :text="formatDate(entry.trashed_at, 'detail')">
                    <time :datetime="toIsoString(entry.trashed_at)">{{
                      formatRelativeTime(entry.trashed_at, 'en')
                    }}</time>
                  </UTooltip>
                </dd>
              </div>
              <div class="min-w-0 space-y-1">
                <dt class="flex items-center gap-1.5 text-xs text-muted">
                  <UIcon
                    name="i-lucide-calendar-clock"
                    class="size-3.5 shrink-0"
                    aria-hidden="true"
                  />
                  Purges
                </dt>
                <dd>
                  <UTooltip :text="formatDate(entry.purge_at, 'detail')">
                    <time :datetime="toIsoString(entry.purge_at)">{{
                      formatRelativeTime(entry.purge_at, 'en')
                    }}</time>
                  </UTooltip>
                </dd>
              </div>
            </dl>
            <div class="flex flex-wrap items-start justify-end gap-1.5" @click.stop>
              <UCheckbox
                class="self-center shrink-0"
                :model-value="selected.has(entry.id)"
                :aria-label="`Select file ${fileName(entry.original_path)}`"
                :disabled="actionsBlocked"
                @update:model-value="toggleSelection(entry.id, $event)"
              />
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-undo-2"
                :loading="restoringId === entry.id"
                :disabled="actionsBlocked"
                @click="restore(entry)"
                :aria-label="`Restore ${fileName(entry.original_path)}`"
                >Restore</UButton
              >
              <UDropdownMenu
                :items="entryActions(entry)"
                :modal="false"
                :content="{ align: 'end' }"
              >
                <UButton
                  color="neutral"
                  variant="ghost"
                  size="sm"
                  icon="i-lucide-ellipsis"
                  square
                  :aria-label="`Actions for ${fileName(entry.original_path)}`"
                  :loading="deletingId === entry.id"
                  :disabled="actionsBlocked"
                />
              </UDropdownMenu>
            </div>
          </article>
        </DeferredContent>
      </div>
      <PaginationControls position="bottom" />
    </div>
    <FileDetailModal
      v-model:open="detailOpen"
      @update:open="updateDetailOpen"
      v-model:raw-open="rawOpen"
      :detail="detail"
      :transcode-disabled-reason="transcodeDisabledReason"
      :health-checking="healthChecking || actionBusy"
      :transcoding="transcoding"
      :refreshing-metadata="refreshingMetadata === detail?.id"
      :refresh-disabled-reason="refreshDisabledReason"
      @transcode="runTranscode"
      @health-check="runHealthCheck"
      @refresh-metadata="detail && refreshMetadata(detail)"
      @skip-cooldown="detail && skipCooldown(detail)"
    />
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { createReusableTemplate } from '@vueuse/core';
import type { DropdownMenuItem } from '@nuxt/ui';
import ListFilters from '~/components/ListFilters.vue';
import ListPagination from '~/components/ListPagination.vue';
import { useDialog } from '~/composables/useDialog';
import { useLibraries } from '~/composables/useLibraries';
import { useLibraryProgress } from '~/composables/useLibraryProgress';
import { useEvents } from '~/composables/useEvents';
import { useTrash, type TrashFilters } from '~/composables/useTrash';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import { useListPagination } from '~/composables/useListPagination';
import type { MediaFileDetail, MediaFileRow, TrashEntry } from '~/types/api';
import { formatBytes } from '~/utils';
import { formatDate, toIsoString } from '~/utils/date';
import { formatRelativeTime } from '~/utils/relativeTime';
import {
  manualTranscodePrerequisite,
  refreshMetadataPrerequisite,
  useFiles,
} from '~/composables/useFiles';
import {
  applyFileProgress,
  applyQueuedFileJob,
  newerJobTime,
  retainFileJobStatus,
  type FileJobProgress,
} from '~/composables/stateHelpers';
import {
  TRASH_FILTER_ALL,
  trashDirectory,
  trashQueryKeys,
  trashRouteFilters,
  trashSortOptions,
  updateTrashRoute,
} from '~/utils/trashList';

const route = useRoute();
const router = useRouter();
const [DefinePagination, PaginationControls] = createReusableTemplate<{
  position: 'top' | 'bottom';
}>();

const { confirmDialog } = useDialog();
const {
  items: libraries,
  ensure: ensureLibraries,
  reload: reloadLibraries,
  lastError: libraryError,
} = useLibraries();
const { scanner } = useLibraryProgress();
const {
  items: entries,
  total,
  isLoading: loading,
  lastError,
  load,
  totalSizeBytes,
  expunge,
  empty,
  restore: restoreEntry,
} = useTrash();
const {
  loadDetail: loadFileDetail,
  queueHealthCheck,
  queueTranscode,
  refreshMetadata,
  skipCooldown,
  refreshingMetadata,
  actionBusy,
} = useFiles();
const deletingId = ref<string | null>(null);
const restoringId = ref<string | null>(null);
const emptying = ref(false);
const batchDeleting = ref(false);
const selected = ref(new Set<string>());
const notify = useNotification();
const { messageFor } = useApiErrorMessage();
const mutationBusy = computed(
  () =>
    actionBusy.value ||
    restoringId.value !== null ||
    deletingId.value !== null ||
    emptying.value ||
    batchDeleting.value,
);
const busy = computed(() => loading.value || mutationBusy.value);
const actionsBlocked = computed(() => busy.value);
const pageSelected = computed(
  () => entries.value.length > 0 && entries.value.every((entry) => selected.value.has(entry.id)),
);
const query = ref('');
const libraryId = ref(TRASH_FILTER_ALL);
const filtersActive = computed(() => !!query.value.trim() || libraryId.value !== TRASH_FILTER_ALL);
const { limit, results, applyRouteLimit, changeLimit, changeOffset } = useListPagination(
  'trash_page_size',
  updateQuery,
);
const offset = ref(0);
const sortBy = ref('trashed_at');
const sortDir = ref<'asc' | 'desc'>('desc');
const filters = computed<TrashFilters>(() => ({
  query: query.value.trim() || undefined,
  library_id: libraryId.value === TRASH_FILTER_ALL ? undefined : libraryId.value,
  limit: limit.value,
  offset: offset.value,
  sort_by: sortBy.value as TrashFilters['sort_by'],
  sort_dir: sortDir.value,
}));
const detailOpen = ref(false);
const detail = ref<MediaFileDetail | null>(null);
let detailLoad: {
  id: number;
  row?: MediaFileRow;
  progress: Map<number, FileJobProgress>;
} | null = null;
const detailFileId = ref<number | null>(null);
const terminalDetailRefreshes = new Set<string>();
const rawOpen = ref(false);
const healthChecking = ref(false);
const transcoding = ref(false);
const transcodeDisabledReason = computed(() =>
  detail.value ? manualTranscodePrerequisite(detail.value, libraries.value) : null,
);
const refreshDisabledReason = computed(() =>
  detail.value
    ? refreshMetadataPrerequisite(detail.value, libraries.value, !!scanner.value?.paused)
    : null,
);
const loadDetail = async (id: number): Promise<void> => {
  const request: NonNullable<typeof detailLoad> = { id, progress: new Map() };
  detailLoad = request;
  try {
    const file = await loadFileDetail(id);
    if (detailLoad === request && detailOpen.value && detailFileId.value === id) {
      const merged = { ...file, ...request.row };
      if (detail.value?.id === id) retainFileJobStatus(merged, detail.value);
      if (merged.active_job_id !== null) {
        const update = request.progress.get(merged.active_job_id);
        if (update) applyFileProgress(merged, update);
      }
      detail.value = merged;
    }
  } catch (error) {
    if (detailLoad === request && detailOpen.value) {
      updateDetailOpen(false);
      notify.error(error instanceof Error ? error.message : 'Could not load file.');
    }
  } finally {
    if (detailLoad === request) detailLoad = null;
  }
};
const openDetail = (id: number): void => {
  if (mutationBusy.value) return;
  terminalDetailRefreshes.clear();
  detailFileId.value = id;
  detail.value = null;
  detailOpen.value = true;
  void loadDetail(id);
};
const stopEvents = useEvents().on((event) => {
  if (
    event.type === 'file.status' &&
    (event.payload.action === 'deleted' || event.payload.action === 'pruned')
  ) {
    if (event.payload.media_file_id === detailFileId.value) updateDetailOpen(false);
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
  if (event.type === 'file.status' && event.payload.file) {
    const file = { ...event.payload.file };
    if (detailLoad?.id === file.id) detailLoad.row = file;
    if (!detailOpen.value || file.id !== detailFileId.value) return;
    if (detail.value) {
      retainFileJobStatus(file, detail.value);
      if (detail.value.active_job_id !== null && detail.value.active_job_updated_at !== null)
        applyFileProgress(file, {
          job_id: detail.value.active_job_id,
          progress: detail.value.active_job_progress,
          updated_at: detail.value.active_job_updated_at,
        });
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
    const job = event.payload.job;
    if (job.file?.id !== detail.value.id) return;
    retainFileJobStatus(detail.value, job);
    detail.value.jobs = [
      {
        id: job.id,
        kind: job.kind,
        status: job.status,
        error: job.error,
        created_at: job.created_at,
      },
      ...detail.value.jobs.filter((item) => item.id !== job.id),
    ].sort((a, b) => b.id - a.id);
    if (['succeeded', 'failed', 'cancelled', 'skipped'].includes(job.status)) {
      const key = `${job.id}:${job.status}`;
      if (!terminalDetailRefreshes.has(key)) {
        terminalDetailRefreshes.add(key);
        void loadDetail(detail.value.id);
      }
    }
  }
  if (event.type === 'job.deleted' && detail.value)
    detail.value.jobs = detail.value.jobs.filter((job) => job.id !== event.payload.job_id);
});
onBeforeUnmount(() => {
  stopEvents();
  detailLoad = null;
});
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
const runTranscode = async (): Promise<void> => {
  if (!detail.value || healthChecking.value || transcoding.value) return;
  transcoding.value = true;
  try {
    if (await queueTranscode(detail.value)) detailOpen.value = false;
  } finally {
    transcoding.value = false;
  }
};

const libraryOptions = computed(() => [
  { label: 'All libraries', value: TRASH_FILTER_ALL },
  ...libraries.value.map((library) => ({
    label: library.name,
    value: String(library.id),
  })),
]);

const fileName = (path: string): string => path.split(/[\\/]/).at(-1) || path;

const loadTrash = async (): Promise<void> => {
  if (mutationBusy.value) return;
  await load(filters.value).catch(() => {});
};
const refreshTrash = async (): Promise<void> => {
  await Promise.all([loadTrash(), reloadLibraries()]);
};

const clearSelection = (): void => {
  selected.value = new Set();
};
const selectVisible = (): void => {
  if (actionsBlocked.value) return;
  selected.value = new Set(entries.value.map((entry) => entry.id));
};
const toggleSelection = (id: string, value: boolean | 'indeterminate'): void => {
  if (actionsBlocked.value) return;
  const next = new Set(selected.value);
  if (value === true) next.add(id);
  else next.delete(id);
  selected.value = next;
};
watch(
  () => entries.value.map((entry) => entry.id),
  (ids) => {
    selected.value = new Set([...selected.value].filter((id) => ids.includes(id)));
  },
);

const sameManagedQuery = (
  source: Record<string, unknown>,
  values: Record<string, string>,
): boolean => trashQueryKeys.every((key) => source[key] === values[key]);

async function updateQuery(changes: Record<string, string>): Promise<boolean> {
  if (mutationBusy.value || ('offset' in changes && loading.value)) return false;
  return !(await router.replace({ query: updateTrashRoute(route.query, changes) }));
}

const deleteEntry = async (entry: TrashEntry): Promise<void> => {
  if (actionsBlocked.value) return;
  const result = await confirmDialog({
    title: 'Delete permanently?',
    message: 'This permanently deletes the trashed file and cannot be undone.',
    confirmColor: 'error',
    confirmText: 'Delete',
  });
  if (!result.status || actionsBlocked.value) return;

  deletingId.value = entry.id;
  try {
    const outcome = await expunge(entry.id);
    if (outcome.ok) {
      const next = new Set(selected.value);
      next.delete(entry.id);
      selected.value = next;
      notify.success('File deleted permanently.');
    } else notify.error(messageFor(outcome.error));
  } finally {
    deletingId.value = null;
  }
};
const entryActions = (entry: TrashEntry): DropdownMenuItem[] => [
  {
    label: 'Delete',
    icon: 'i-lucide-trash-2',
    color: 'error',
    disabled: actionsBlocked.value,
    onSelect: () => deleteEntry(entry),
  },
];

const deleteSelected = async (): Promise<void> => {
  if (actionsBlocked.value || !selected.value.size) return;
  const ids = [...selected.value];
  const result = await confirmDialog({
    title: 'Delete selected permanently?',
    message: `This permanently deletes ${ids.length} selected file${ids.length === 1 ? '' : 's'} and cannot be undone.`,
    confirmColor: 'error',
    confirmText: 'Delete',
  });
  if (!result.status || actionsBlocked.value) return;
  batchDeleting.value = true;
  let deleted = 0;
  let failed = 0;
  try {
    for (const id of ids) {
      const outcome = await expunge(id);
      if (outcome.ok) {
        deleted++;
        selected.value.delete(id);
      } else failed++;
    }
    notify.success(`${deleted} selected file${deleted === 1 ? '' : 's'} deleted permanently.`);
    if (failed)
      notify.error(`${failed} selected file${failed === 1 ? '' : 's'} could not be deleted.`);
  } finally {
    batchDeleting.value = false;
  }
};

const restore = async (entry: TrashEntry): Promise<void> => {
  if (actionsBlocked.value) return;
  const result = await confirmDialog({
    title: 'Restore file?',
    message: `Destination:\n${entry.original_path}\n\nReplacing without moving the destination to trash permanently overwrites it.\n\nDisk syncing can be slow. Without it, the trash copy may be deleted before the restored file is fully written to disk. A crash at that point can leave neither copy.`,
    confirmText: 'Restore',
    options: [
      { key: 'replace_existing', label: 'Replace existing destination' },
      {
        key: 'move_to_trash',
        label: 'Move destination file to trash (only applies when replacing)',
      },
      {
        key: 'sync_to_disk',
        label: 'Sync restored file to disk (fsync)',
        checked: true,
      },
    ],
  });
  if (!result.status || actionsBlocked.value) return;
  restoringId.value = entry.id;
  try {
    const outcome = await restoreEntry(
      entry.id,
      {
        replace_existing: result.value?.replace_existing ?? false,
        move_to_trash:
          (result.value?.replace_existing ?? false) && (result.value?.move_to_trash ?? false),
        sync_to_disk: result.value?.sync_to_disk ?? true,
      },
      filters.value,
    );
    if (outcome.ok) {
      const next = new Set(selected.value);
      next.delete(entry.id);
      selected.value = next;
      notify.success('File restored.');
      if (outcome.offset !== undefined)
        await router.replace({
          query: updateTrashRoute(route.query, {
            offset: String(outcome.offset),
          }),
        });
      if (outcome.refreshError) notify.warning(outcome.refreshError);
    } else notify.error(messageFor(outcome.error));
  } finally {
    restoringId.value = null;
  }
};

const emptyTrash = async (): Promise<void> => {
  if (actionsBlocked.value || total.value === 0) return;
  const result = await confirmDialog({
    title: 'Empty trash permanently?',
    message:
      'This permanently deletes every file in trash, including files outside the current filters. This cannot be undone.',
    confirmColor: 'error',
    confirmText: 'Empty trash',
  });
  if (!result.status || actionsBlocked.value) return;
  emptying.value = true;
  try {
    const outcome = await empty();
    if ('error' in outcome) notify.error(messageFor(outcome.error));
    else {
      notify.success(
        `${outcome.deleted} file${outcome.deleted === 1 ? '' : 's'} deleted permanently.`,
      );
      const failed = new Set(outcome.failed);
      selected.value = new Set([...selected.value].filter((id) => failed.has(id)));
      if (outcome.offset !== undefined)
        await router.replace({
          query: updateTrashRoute(route.query, {
            offset: String(outcome.offset),
          }),
        });
      if (outcome.refreshError) notify.warning(outcome.refreshError);
      if (outcome.failed.length)
        notify.error(
          `${outcome.failed.length} file${outcome.failed.length === 1 ? '' : 's'} could not be deleted.`,
        );
    }
  } finally {
    emptying.value = false;
  }
};

let mounted = false;
watch([total, offset, loading, mutationBusy], async () => {
  if (!mounted || busy.value || lastError.value) return;
  if (offset.value > 0 && offset.value >= total.value) {
    const lastOffset = Math.max(0, Math.ceil(total.value / limit.value) - 1) * limit.value;
    await updateQuery({ offset: String(lastOffset) });
  }
});
watch(
  () => route.query,
  async (current, previous) => {
    const values = trashRouteFilters(current);
    if (previous && !sameManagedQuery(trashRouteFilters(previous), values)) clearSelection();
    query.value = values.query ?? '';
    libraryId.value = values.library_id ?? TRASH_FILTER_ALL;
    applyRouteLimit(values.limit);
    offset.value = Number(values.offset ?? 0);
    sortBy.value = values.sort_by ?? 'trashed_at';
    sortDir.value = (values.sort_dir as 'asc' | 'desc' | undefined) ?? 'desc';
    if (!sameManagedQuery(current, values)) {
      await router.replace({ query: updateTrashRoute(current, {}) });
      return;
    }
    if (mounted && !mutationBusy.value) await loadTrash();
  },
  { deep: true, immediate: true },
);
watch(query, async (value) => {
  const normalized = value.trim();
  if (normalized !== (typeof route.query.query === 'string' ? route.query.query.trim() : ''))
    await updateQuery({ query: normalized });
});
onMounted(() => {
  mounted = true;
  void loadTrash();
  void ensureLibraries().catch(() => {});
});
</script>
