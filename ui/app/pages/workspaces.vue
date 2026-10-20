<template>
  <main class="@container w-full min-w-0 max-w-full space-y-6">
    <DefinePagination v-slot="{ position }">
      <ListPagination
        v-if="!lastError && (rows.length || offset > 0)"
        label="Workspaces"
        :position="position"
        :limit="limit"
        :offset="offset"
        :total="snapshot?.total ?? 0"
        :shown="rows.length"
        :disabled="busy"
        @update:limit="updateQuery({ limit: String($event) })"
        @update:offset="updateQuery({ offset: String($event) })"
      >
        <UTooltip
          :text="
            blockedReason ??
            (!eligible.length
              ? 'No unused workspaces on this page.'
              : 'Select unused workspaces on this page')
          "
        >
          <span class="inline-flex">
            <UButton
              v-if="rows.length"
              size="sm"
              color="neutral"
              variant="outline"
              :icon="pageSelected ? 'i-lucide-square-check' : 'i-lucide-square'"
              :disabled="busy || !eligible.length"
              @click="togglePage"
              >{{ pageSelected ? 'Clear selection' : 'Select page' }}</UButton
            >
          </span>
        </UTooltip>
        <UBadge v-if="selected.size" color="primary" variant="subtle"
          >{{ selected.size }} selected</UBadge
        >
        <UButton
          v-if="selected.size"
          size="xs"
          color="neutral"
          variant="ghost"
          :disabled="busy"
          @click="selected = new Set()"
          >Clear selection</UButton
        >
        <UTooltip v-if="selected.size" :text="blockedReason ?? 'Remove selected temporary files'">
          <span class="inline-flex">
            <UButton
              size="sm"
              color="neutral"
              variant="outline"
              icon="i-lucide-trash-2"
              :loading="pruning"
              :disabled="busy"
              @click="pruneSelected"
              >Prune selected</UButton
            >
          </span>
        </UTooltip>
      </ListPagination>
    </DefinePagination>
    <PageHeader page="workspaces">
      <template #actions>
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-refresh-cw"
          :loading="loading"
          :disabled="busy"
          @click="loadWorkspaces"
          >Refresh</UButton
        >
        <UTooltip :text="pruneUnusedReason ?? 'Remove unused temporary files'">
          <span class="inline-flex"
            ><UButton
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-trash-2"
              :loading="pruning"
              :disabled="busy || !summary?.summary.unused_count"
              @click="pruneUnused"
              >Prune unused</UButton
            ></span
          ></UTooltip
        >
      </template>
    </PageHeader>
    <UAlert
      v-if="lastError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not inspect workspaces"
      :description="lastError"
    />
    <WorkspaceSummary :snapshot="summary" />
    <ListFilters
      v-model:query="query"
      search-placeholder="Search workspaces..."
      :sort-by="sortBy"
      :sort-dir="sortDir"
      :sort-options="workspaceSortOptions"
      sort-label="Sort workspaces by"
      :disabled="pruning"
      @update:sort-by="updateQuery({ sort_by: $event })"
      @update:sort-dir="updateQuery({ sort_dir: $event })"
    >
      <USelectMenu
        value-key="value"
        :model-value="filter"
        :items="filters"
        :disabled="pruning"
        size="sm"
        class="w-full sm:w-48"
        aria-label="Workspace eligibility"
        @update:model-value="(value) => value !== undefined && updateQuery({ eligibility: value })"
      />
    </ListFilters>
    <UAlert
      v-for="entry in outcomes"
      :key="entry.workspace_id"
      :color="entry.outcome === 'error' ? 'error' : 'warning'"
      variant="soft"
      icon="i-lucide-triangle-alert"
      :title="`Workspace ${entry.workspace_id}: ${entry.outcome}`"
      :description="entry.reason ?? undefined"
    />
    <PaginationControls position="top" />
    <div v-if="loading && !snapshot" class="space-y-3">
      <USkeleton v-for="index in 3" :key="index" class="h-28 rounded-lg" />
    </div>
    <UEmpty
      v-else-if="snapshot && !rows.length && !lastError"
      :title="summary?.summary.count ? 'No workspaces match these filters.' : 'No workspaces.'"
      variant="naked"
      :ui="{
        root: 'gap-1 p-0 sm:p-0 lg:p-0',
        header: 'max-w-none gap-1',
        title: 'text-sm font-normal text-toned',
      }"
      class="mw-card py-10 sm:py-10 lg:py-10"
    >
      <template #leading>
        <UIcon
          :name="summary?.summary.count ? 'i-lucide-search-x' : 'i-lucide-folder-tree'"
          class="size-6 text-toned"
        />
      </template>
    </UEmpty>
    <div v-else class="space-y-2">
      <DeferredContent v-for="row in rows" :key="row.workspace_id" :min-height="112" keep-mounted>
        <WorkspaceRowCard
          :workspace="row"
          selectable
          :selected="selected.has(row.workspace_id)"
          :busy="busy"
          @select="toggle(row.workspace_id)"
          @open="openJob"
        />
      </DeferredContent>
    </div>
    <PaginationControls position="bottom" />
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { createReusableTemplate } from '@vueuse/core';
import ListFilters from '~/components/ListFilters.vue';
import ListPagination from '~/components/ListPagination.vue';
import JobDetailModal from '~/components/JobDetailModal.vue';
import { useWorkspaces, type WorkspaceFilters } from '~/composables/useWorkspaces';
import { useDialog } from '~/composables/useDialog';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import type { WorkspacePruneResult } from '~/types/api';
import {
  workspaceQueryKeys,
  workspaceRouteFilters,
  workspaceSortOptions,
  updateWorkspaceRoute,
} from '~/utils/workspaceList';

const { snapshot, summary, isLoading: loading, lastError, load, prune } = useWorkspaces();
const route = useRoute();
const router = useRouter();
const [DefinePagination, PaginationControls] = createReusableTemplate<{
  position: 'top' | 'bottom';
}>();
const { confirmDialog } = useDialog();
const { messageFor } = useApiErrorMessage();
const notify = useNotification();
const query = ref('');
const filter = ref('all');
const limit = ref(50);
const offset = ref(0);
const sortBy = ref('workspace_id');
const sortDir = ref<'asc' | 'desc'>('desc');
const listFilters = computed<WorkspaceFilters>(() => ({
  query: query.value.trim() || undefined,
  eligibility: filter.value as WorkspaceFilters['eligibility'],
  sort_by: sortBy.value as WorkspaceFilters['sort_by'],
  sort_dir: sortDir.value,
  limit: limit.value,
  offset: offset.value,
}));
const filters = [
  { label: 'All workspaces', value: 'all' },
  { label: 'Unused', value: 'unused' },
  { label: 'Protected', value: 'protected' },
];
const selected = ref(new Set<number>());
const outcomes = ref<WorkspacePruneResult['items']>([]);
const pruning = ref(false);
const busy = computed(() => loading.value || pruning.value);
const blockedReason = computed(() => (busy.value ? 'Wait for the current action.' : null));
const pruneUnusedReason = computed(
  () =>
    blockedReason.value ??
    (!summary.value
      ? 'Load workspaces first.'
      : !summary.value.summary.unused_count
        ? 'No unused workspaces.'
        : null),
);
const rows = computed(() => snapshot.value?.items ?? []);
const eligible = computed(() => rows.value.filter((row) => row.eligible));
const pageSelected = computed(
  () =>
    eligible.value.length > 0 &&
    eligible.value.every((row) => selected.value.has(row.workspace_id)),
);
const toggle = (id: number): void => {
  if (busy.value) return;
  const next = new Set(selected.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selected.value = next;
};
const togglePage = (): void => {
  if (busy.value) return;
  const next = new Set(selected.value);
  for (const row of eligible.value) {
    if (pageSelected.value) next.delete(row.workspace_id);
    else next.add(row.workspace_id);
  }
  selected.value = next;
};
watch(eligible, (rows) => {
  const ids = new Set(rows.map((row) => row.workspace_id));
  selected.value = new Set([...selected.value].filter((id) => ids.has(id)));
});
const runPrune = async (allUnused: boolean): Promise<void> => {
  if (busy.value) return;
  const ids = [...selected.value];
  const result = await confirmDialog({
    title: allUnused ? 'Prune unused workspaces?' : 'Prune selected workspaces?',
    message: allUnused
      ? 'Delete unused workspace files, including those outside the current filters. Job history, media, and trash are kept.'
      : `Delete temporary files for ${ids.length} selected workspace${ids.length === 1 ? '' : 's'} if no longer in use. Job history, media, and trash are kept.`,
    confirmColor: 'error',
    confirmText: 'Prune',
  });
  if (!result.status || busy.value) return;
  pruning.value = true;
  let resultItems: WorkspacePruneResult['items'] | null = null;
  try {
    resultItems = (await prune(allUnused ? { all_unused: true } : { job_ids: ids })).items;
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
  } finally {
    const items = resultItems ?? [];
    outcomes.value = items.filter((entry) => entry.outcome !== 'removed');
    const removed = new Set(
      items.filter((entry) => entry.outcome === 'removed').map((entry) => entry.workspace_id),
    );
    selected.value = new Set([...selected.value].filter((id) => !removed.has(id)));
    if (removed.size)
      notify.success(`${removed.size} workspace${removed.size === 1 ? '' : 's'} pruned.`);
    const errors = outcomes.value.filter((entry) => entry.outcome === 'error').length;
    const skipped = outcomes.value.filter((entry) => entry.outcome === 'skipped').length;
    if (errors) notify.error(`${errors} workspace${errors === 1 ? '' : 's'} could not be pruned.`);
    if (skipped)
      notify.warning(
        `${skipped} workspace${skipped === 1 ? '' : 's'} kept. See the reasons below.`,
      );
    if (resultItems?.length === 0 && allUnused) notify.warning('No unused workspaces to prune.');
    pruning.value = false;
    await loadWorkspaces();
  }
};
const pruneSelected = (): Promise<void> => runPrune(false);
const pruneUnused = (): Promise<void> => runPrune(true);
const overlay = useOverlay();
let jobModal: ReturnType<typeof overlay.create<typeof JobDetailModal>> | null = null;
const openJob = (id: number): void => {
  jobModal?.close();
  jobModal = overlay.create(JobDetailModal, {
    props: { jobId: id },
    destroyOnClose: true,
  });
  const modal = jobModal;
  void modal.open().result.then(() => {
    if (jobModal === modal) jobModal = null;
  });
};
onBeforeUnmount(() => {
  mounted = false;
  jobModal?.close();
});
const loadWorkspaces = async (): Promise<void> => {
  const filters = listFilters.value;
  await load(filters);
  if (!mounted || filters !== listFilters.value || loading.value) return;
  if (
    !lastError.value &&
    snapshot.value &&
    offset.value >= snapshot.value.total &&
    offset.value > 0
  ) {
    const lastOffset = Math.max(0, Math.ceil(snapshot.value.total / limit.value) - 1) * limit.value;
    await router.replace({
      query: updateWorkspaceRoute(route.query, { offset: String(lastOffset) }),
    });
  }
};
const sameManagedQuery = (
  source: Record<string, unknown>,
  values: Record<string, string>,
): boolean => workspaceQueryKeys.every((key) => source[key] === values[key]);
const updateQuery = async (changes: Record<string, string>): Promise<void> => {
  if (pruning.value || ('offset' in changes && loading.value)) return;
  await router.replace({ query: updateWorkspaceRoute(route.query, changes) });
};
let mounted = false;
watch(
  () => route.query,
  async (current, previous) => {
    const values = workspaceRouteFilters(current);
    if (previous && !sameManagedQuery(workspaceRouteFilters(previous), values))
      selected.value = new Set();
    query.value = values.query ?? '';
    filter.value = values.eligibility ?? 'all';
    limit.value = Number(values.limit ?? 50);
    offset.value = Number(values.offset ?? 0);
    sortBy.value = values.sort_by ?? 'workspace_id';
    sortDir.value = (values.sort_dir as 'asc' | 'desc' | undefined) ?? 'desc';
    if (!sameManagedQuery(current, values)) {
      await router.replace({ query: updateWorkspaceRoute(current, {}) });
      return;
    }
    if (mounted && !pruning.value) await loadWorkspaces();
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
  void loadWorkspaces();
});
</script>
