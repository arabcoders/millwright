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
        @update:limit="updateQuery({ limit: String($event) })"
        @update:offset="updateQuery({ offset: String($event) })"
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
          size="xs"
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
          @click="loadTrash"
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

    <ListFilters
      v-model:query="query"
      search-placeholder="Search trash..."
      :sort-by="sortBy"
      :sort-dir="sortDir"
      :sort-options="trashSortOptions"
      sort-label="Sort trash"
      :disabled="mutationBusy"
      @update:sort-by="updateQuery({ sort_by: $event })"
      @update:sort-dir="updateQuery({ sort_dir: $event })"
    >
      <UFormField label="" size="sm" class="w-full sm:w-auto">
        <USelectMenu
          value-key="value"
          v-model="libraryId"
          :items="libraryOptions"
          :disabled="mutationBusy"
          size="sm"
          class="w-full sm:min-w-40"
          aria-label="Filter by library"
          @update:model-value="updateQuery({ library_id: libraryId })"
        />
      </UFormField>
      <template #trailing>
        <UBadge class="ml-auto self-center" color="neutral" variant="subtle"
          >{{ formatBytes(totalSizeBytes) }} in trash</UBadge
        >
      </template>
    </ListFilters>

    <div class="space-y-3">
      <PaginationControls position="top" />
      <div
        v-if="loading && !entries.length"
        class="grid grid-cols-1 gap-3 @min-[40rem]:grid-cols-2 @min-[64rem]:grid-cols-3"
        aria-label="Loading trash"
      >
        <USkeleton v-for="index in 6" :key="index" class="h-64 rounded-xl" />
      </div>
      <div
        v-else-if="entries.length === 0 && !lastError"
        class="mw-card flex flex-col items-center justify-center gap-1 py-10 text-center"
      >
        <UIcon name="i-lucide-trash-2" class="size-6 text-toned" />
        <p class="text-sm text-toned">
          {{
            query || libraryId !== TRASH_FILTER_ALL
              ? 'No trash matches the current filters.'
              : 'Trash is empty.'
          }}
        </p>
      </div>
      <div
        v-else-if="entries.length"
        class="grid grid-cols-1 gap-3 @min-[40rem]:grid-cols-2 @min-[64rem]:grid-cols-3"
      >
        <DeferredContent v-for="entry in entries" :key="entry.id" :min-height="256" class="min-w-0">
          <article
            class="mw-card @container/trash-card flex h-full min-w-0 flex-col gap-3 p-4"
            :class="selected.has(entry.id) ? 'ring-2 ring-primary' : ''"
          >
            <div class="flex items-start gap-3">
              <UCheckbox
                class="shrink-0 self-center"
                :model-value="selected.has(entry.id)"
                :aria-label="`Select file ${fileName(entry.original_path)}`"
                :disabled="actionsBlocked"
                @update:model-value="toggleSelection(entry.id, $event)"
              />
              <div class="min-w-0 flex-1">
                <h2 class="min-w-0">
                  <UTooltip :text="entry.original_path">
                    <p class="mt-1 truncate text-xs text-dimmed">
                      {{ trashDirectory(entry.original_path) }}
                    </p>
                  </UTooltip>
                  <UTooltip :text="fileName(entry.original_path)">
                    <button
                      v-if="entry.current_file_id !== null"
                      type="button"
                      :aria-label="`Open details for ${fileName(entry.original_path)}`"
                      :disabled="mutationBusy"
                      @click="openDetail(entry.current_file_id)"
                      class="line-clamp-2 min-h-10 w-full break-all text-left text-sm font-semibold text-highlighted hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                    >
                      {{ fileName(entry.original_path) }}
                    </button>
                    <span
                      v-else
                      class="line-clamp-2 min-h-10 break-all text-sm font-semibold text-highlighted"
                      >{{ fileName(entry.original_path) }}</span
                    >
                  </UTooltip>
                </h2>
              </div>
              <span
                class="flex size-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary"
              >
                <UIcon name="i-lucide-file-video" class="size-5" aria-hidden="true" />
              </span>
            </div>
            <dl
              class="grid grid-cols-2 gap-x-3 gap-y-2 text-xs @min-[22rem]/trash-card:grid-cols-3"
            >
              <div>
                <dt class="text-dimmed">File size</dt>
                <dd class="mt-0.5 text-toned">
                  {{ formatBytes(entry.size_bytes) }}
                </dd>
              </div>
              <div>
                <dt class="text-dimmed">Trashed</dt>
                <dd class="mt-0.5 text-toned">
                  <UTooltip :text="formatDate(entry.trashed_at, 'detail')">
                    <time :datetime="toIsoString(entry.trashed_at)">{{
                      formatRelativeTime(entry.trashed_at, 'en')
                    }}</time>
                  </UTooltip>
                </dd>
              </div>
              <div>
                <dt class="text-dimmed">Purges</dt>
                <dd class="mt-0.5 text-toned">
                  <UTooltip :text="formatDate(entry.purge_at, 'detail')">
                    <time :datetime="toIsoString(entry.purge_at)">{{
                      formatRelativeTime(entry.purge_at, 'en')
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
                icon="i-lucide-trash-2"
                :aria-label="`Delete ${fileName(entry.original_path)}`"
                :loading="deletingId === entry.id"
                :disabled="actionsBlocked"
                @click="deleteEntry(entry)"
                >Delete</UButton
              >
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
import ListFilters from '~/components/ListFilters.vue';
import ListPagination from '~/components/ListPagination.vue';
import { useDialog } from '~/composables/useDialog';
import { useLibraries } from '~/composables/useLibraries';
import { useLibraryProgress } from '~/composables/useLibraryProgress';
import { useEvents } from '~/composables/useEvents';
import { useTrash, type TrashFilters } from '~/composables/useTrash';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
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
const { items: libraries, ensure: ensureLibraries } = useLibraries();
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
const limit = ref(50);
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
let detailLoad: { id: number; row?: MediaFileRow } | null = null;
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
  const request: NonNullable<typeof detailLoad> = { id };
  detailLoad = request;
  try {
    const file = await loadFileDetail(id);
    if (detailLoad === request && detailOpen.value) detail.value = { ...file, ...request.row };
  } catch {
    if (detailLoad === request && detailOpen.value) updateDetailOpen(false);
  } finally {
    if (detailLoad === request) detailLoad = null;
  }
};
const openDetail = (id: number): void => {
  if (mutationBusy.value) return;
  detail.value = null;
  detailOpen.value = true;
  void loadDetail(id);
};
const stopEvents = useEvents().on((event) => {
  if (event.type !== 'file.status' || !event.payload.file) return;
  const file = event.payload.file;
  if (detailLoad?.id === file.id) detailLoad.row = file;
  if (!detailOpen.value || (detail.value?.id !== file.id && detailLoad?.id !== file.id)) return;
  if (detail.value) Object.assign(detail.value, file);
  if (['probed', 'probe_error'].includes(event.payload.action)) void loadDetail(file.id);
});
onBeforeUnmount(() => {
  stopEvents();
  detailLoad = null;
});
const updateDetailOpen = (open: boolean): void => {
  detailOpen.value = open;
  if (open) return;
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

const updateQuery = async (changes: Record<string, string>): Promise<void> => {
  if (mutationBusy.value || ('offset' in changes && loading.value)) return;
  await router.replace({ query: updateTrashRoute(route.query, changes) });
};

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
watch(
  () => route.query,
  async (current, previous) => {
    const values = trashRouteFilters(current);
    if (previous && !sameManagedQuery(trashRouteFilters(previous), values)) clearSelection();
    query.value = values.query ?? '';
    libraryId.value = values.library_id ?? TRASH_FILTER_ALL;
    limit.value = Number(values.limit ?? 50);
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
