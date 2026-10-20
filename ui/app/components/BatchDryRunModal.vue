<template>
  <UModal
    :open="open"
    :title="title"
    :fullscreen="fullscreen"
    :ui="{
      content: fullscreen ? '' : 'w-[calc(100vw-1rem)] max-w-7xl',
      wrapper: 'min-w-0 flex-1',
      close: 'static',
      body: fullscreen ? 'min-h-0 overflow-y-auto' : 'max-h-[80vh] overflow-y-auto',
    }"
    @update:open="close"
    @after:leave="emit('after:leave')"
  >
    <template #actions>
      <UTooltip :text="fullscreen ? 'Exit fullscreen' : 'Fullscreen'">
        <UButton
          color="neutral"
          variant="ghost"
          size="sm"
          :icon="fullscreen ? 'i-lucide-minimize' : 'i-lucide-maximize'"
          :aria-label="fullscreen ? 'Exit fullscreen' : 'Fullscreen'"
          :aria-pressed="fullscreen"
          @click="fullscreen = !fullscreen"
          >{{ fullscreen ? 'Exit fullscreen' : 'Fullscreen' }}</UButton
        >
      </UTooltip>
    </template>
    <template #body>
      <div class="min-w-0 space-y-4">
        <UFormField label="Flow">
          <UFieldGroup size="sm" class="w-full min-w-0">
            <USelectMenu
              v-model="selectedFlowId"
              :items="flowItems"
              value-key="value"
              placeholder="Pick a flow"
              :disabled="active || starting || cancelling || loading"
              class="min-w-0 flex-1"
            />
            <UTooltip
              :text="
                active || starting || cancelling
                  ? 'Available when the dry run stops.'
                  : 'Refresh flows'
              "
            >
              <UButton
                color="neutral"
                variant="outline"
                icon="i-lucide-refresh-cw"
                aria-label="Refresh flows"
                :loading="flowsLoading"
                :disabled="active || starting || cancelling"
                @click="reloadFlows"
                >Refresh</UButton
              >
            </UTooltip>
          </UFieldGroup>
        </UFormField>
        <UAlert
          v-if="!flows.length && !flowsLoading && !flowError"
          color="neutral"
          variant="soft"
          icon="i-lucide-workflow"
          title="No saved flows"
          description="Create a flow before starting a dry run."
        />
        <UAlert
          v-if="error || flowError"
          color="error"
          variant="soft"
          icon="i-lucide-circle-alert"
          title="Dry run unavailable"
          :description="error || flowError?.detail || 'Could not load flows.'"
        >
          <template v-if="error && report" #actions>
            <UButton
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-refresh-cw"
              :loading="loading"
              :disabled="starting || cancelling || exporting"
              @click="refresh"
              >Reload</UButton
            >
          </template>
        </UAlert>
        <div v-if="loading && !report" class="space-y-2" aria-label="Loading dry run">
          <USkeleton class="h-8" /><USkeleton class="h-24" />
        </div>
        <template v-else-if="report">
          <section class="space-y-3" aria-label="Dry-run summary">
            <div class="flex flex-wrap items-center justify-end gap-1.5">
              <UTooltip :text="`${report.flow_name} · revision ${report.flow_version}`">
                <StatusBadge :status="report.state" group="dry_run" tabindex="0" />
              </UTooltip>
              <UBadge color="neutral" variant="subtle" size="sm" class="tabular-nums">
                {{ progressLabel }}
              </UBadge>
              <UTooltip
                v-if="!active && (!report.snapshot_complete || report.processed < report.total)"
                text="Some files were not evaluated."
              >
                <UBadge color="warning" variant="soft" size="sm" tabindex="0">Incomplete</UBadge>
              </UTooltip>
              <UBadge
                v-for="entry in counts"
                :key="entry.status"
                :color="entry.color"
                variant="subtle"
                size="sm"
                class="gap-1"
              >
                <UIcon :name="entry.icon" class="size-3.5" />
                {{ entry.label }}: {{ entry.count }}
              </UBadge>
            </div>
            <UProgress
              v-if="active"
              :model-value="progress"
              size="sm"
              aria-label="Dry-run progress"
            />
          </section>
          <UAlert
            v-if="report.error"
            color="error"
            variant="soft"
            icon="i-lucide-circle-alert"
            title="Dry run failed"
            :description="report.error"
          />
          <section
            ref="results"
            aria-label="Batch dry-run results"
            :aria-busy="loading"
            class="space-y-2"
          >
            <UEmpty
              v-if="!rows.length"
              :title="
                statuses.length
                  ? active
                    ? 'No matching results yet.'
                    : 'No files match the selected statuses.'
                  : active
                    ? 'Waiting for file results.'
                    : !report.total
                      ? 'No indexed files to preview.'
                      : 'No file results on this page.'
              "
              variant="naked"
              :ui="{
                root: 'gap-1 p-0 sm:p-0 lg:p-0',
                header: 'max-w-none gap-1',
                title: 'text-sm font-normal text-toned',
              }"
              class="mw-card py-10 sm:py-10 lg:py-10"
            >
              <template #leading>
                <UIcon name="i-lucide-folder-search" class="size-6 text-toned" />
              </template>
            </UEmpty>
            <article
              v-for="row in rows"
              :key="row.file_id"
              class="mw-card-padded flex flex-wrap items-center gap-3 text-default"
            >
              <span
                class="flex size-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary"
              >
                <UIcon name="i-lucide-file-video" class="size-5" aria-hidden="true" />
              </span>
              <div class="min-w-0 flex-1">
                <div class="flex items-start justify-between gap-3">
                  <div class="min-w-0 flex-1">
                    <h3 class="truncate text-sm font-semibold text-highlighted">
                      <UTooltip :text="row.relativePath">
                        <button
                          type="button"
                          class="max-w-full truncate text-start hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                          :aria-label="`Open dry run for ${row.name}`"
                          @click="openFileDryRun(row.file_id)"
                        >
                          {{ row.name }}
                        </button>
                      </UTooltip>
                    </h3>
                    <UTooltip v-if="row.folder" :text="row.folder">
                      <p tabindex="0" class="mt-1 truncate text-xs text-dimmed">{{ row.folder }}</p>
                    </UTooltip>
                  </div>
                  <UTooltip :text="row.deferred" :disabled="!row.deferred">
                    <StatusBadge :status="row.status" group="flow" tabindex="0" class="shrink-0" />
                  </UTooltip>
                </div>
                <div
                  v-if="multipleRoots && row.root"
                  class="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-toned"
                >
                  <UTooltip :text="row.root">
                    <UBadge
                      color="neutral"
                      variant="subtle"
                      size="sm"
                      icon="i-lucide-folder-tree"
                      tabindex="0"
                      class="max-w-full"
                    >
                      <span class="truncate">{{ row.rootName }}</span>
                    </UBadge>
                  </UTooltip>
                </div>
                <ReasonDisplay v-if="row.reason" :reason="row.reason" class="mt-1" />
              </div>
            </article>
          </section>
        </template>
      </div>
    </template>
    <template #footer>
      <div class="flex w-full flex-wrap items-center gap-3">
        <nav
          v-if="report?.total"
          aria-label="Dry-run pagination"
          class="flex min-w-0 flex-wrap items-center gap-3"
        >
          <div class="flex min-w-0 flex-wrap items-center gap-2">
            <USelectMenu
              :model-value="statuses"
              :items="statusItems"
              value-key="value"
              multiple
              placeholder="All statuses"
              :clear="{ 'aria-label': 'Clear dry-run status filter' }"
              size="sm"
              class="w-full sm:w-64"
              aria-label="Filter by dry-run status"
              :disabled="starting || cancelling || exporting"
              @update:model-value="changeStatuses"
            />
            <span class="text-xs text-toned tabular-nums">{{ pageLabel }}</span>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <USelectMenu
              :model-value="pageSize"
              :items="pageSizeItems"
              value-key="value"
              size="sm"
              aria-label="Files per page"
              :disabled="paginationBusy"
              @update:model-value="changePage(1, $event)"
            />
            <UFieldGroup size="sm">
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-chevron-left"
                aria-label="Previous page"
                :disabled="page === 1 || paginationBusy"
                @click="changePage(page - 1)"
              />
              <USelectMenu
                :model-value="page"
                :items="pageItems"
                value-key="value"
                :search-input="{ placeholder: 'Find a page...' }"
                virtualize
                color="neutral"
                variant="outline"
                size="sm"
                class="min-w-44"
                aria-label="Jump to dry-run page"
                :disabled="paginationBusy"
                @update:model-value="changePage($event)"
              >
                Page {{ page.toLocaleString('en') }} of {{ pageCount.toLocaleString('en') }}
              </USelectMenu>
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-chevron-right"
                aria-label="Next page"
                :disabled="page >= pageCount || paginationBusy"
                @click="changePage(page + 1)"
              />
            </UFieldGroup>
          </div>
        </nav>
        <div class="ml-auto flex flex-wrap justify-end gap-2">
          <UTooltip v-if="report" :text="exportDisabledReason" :disabled="!exportDisabledReason">
            <span class="inline-flex">
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-download"
                :loading="exporting"
                :disabled="Boolean(exportDisabledReason)"
                @click="download"
                >Export CSV</UButton
              >
            </span>
          </UTooltip>
          <UTooltip
            v-if="active"
            :text="
              confirmingCancel ? 'Confirm cancellation in the dialog.' : 'Cancellation requested.'
            "
            :disabled="!cancelling && report?.state !== 'cancelling' && !confirmingCancel"
          >
            <span class="inline-flex">
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-square"
                :loading="cancelling"
                :disabled="cancelling || report?.state === 'cancelling' || confirmingCancel"
                @click="cancelRun"
                >Cancel dry run</UButton
              >
            </span>
          </UTooltip>
          <UTooltip v-else :text="startDisabledReason" :disabled="!startDisabledReason">
            <span class="inline-flex">
              <UButton
                color="primary"
                size="sm"
                icon="i-lucide-play"
                :loading="starting"
                :disabled="Boolean(startDisabledReason)"
                @click="run"
                >{{ report ? 'Run again' : 'Run dry run' }}</UButton
              >
            </span>
          </UTooltip>
        </div>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useStorage } from '@vueuse/core';
import StatusBadge from '~/components/StatusBadge.vue';
import ReasonDisplay from '~/components/ReasonDisplay.vue';
import FileDetailModal from '~/components/FileDetailModal.vue';
import { useFlows } from '~/composables/useFlows';
import { useDialog } from '~/composables/useDialog';
import { useBatchDryRun } from '~/composables/useBatchDryRun';
import { useFileDryRun } from '~/composables/useFileDryRun';
import { useFiles } from '~/composables/useFiles';
import { useLibraries } from '~/composables/useLibraries';
import { relativeDirectory } from '~/utils/location';
import { statusInfo, statusOptions } from '~/utils/status';
import type { BatchDryRunScope, MediaFileDetail } from '~/types/api';

const props = defineProps<{ open: boolean; scope: BatchDryRunScope }>();
const emit = defineEmits<{
  'update:open': [open: boolean];
  'after:leave': [];
}>();
const fullscreen = useStorage('library_dry_run_fullscreen', false);
const title = computed(() =>
  'library' in props.scope
    ? `Dry run: ${props.scope.library.name}`
    : `Dry run: ${props.scope.files.length} selected file${props.scope.files.length === 1 ? '' : 's'}`,
);
const {
  items: flows,
  ensure: ensureFlows,
  reload: reloadFlows,
  isLoading: flowsLoading,
  lastError: flowError,
} = useFlows();
const { confirmDialog } = useDialog();
const {
  report,
  error,
  loading,
  starting,
  cancelling,
  exporting,
  page,
  pageSize,
  statuses,
  active,
  open: loadReport,
  start,
  refresh,
  changePage: loadPage,
  changeStatuses,
  cancel,
  exportCsv,
  stop,
} = useBatchDryRun();
const paginationBusy = computed(
  () => loading.value || starting.value || cancelling.value || exporting.value,
);
const pageCount = computed(() =>
  Math.max(1, Math.ceil((report.value?.pagination.total ?? 0) / pageSize.value)),
);
const statusItems = statusOptions('flow').filter((option) => option.value);
const pageItems = computed(() =>
  Array.from({ length: pageCount.value }, (_, index) => ({
    label: `Page ${index + 1}`,
    value: index + 1,
  })),
);
const selectedFlowId = ref<number | undefined>(undefined);
const confirmingCancel = ref(false);
const results = ref<HTMLElement | null>(null);
let disposed = false;
const { loadDetail } = useFiles();
const { items: libraries } = useLibraries();
const notify = useNotification();
const overlay = useOverlay();
const detail = ref<MediaFileDetail | null>(null);
const detailOpen = ref(false);
const rawOpen = ref(false);
let fileRequest = 0;
let detailRequest = 0;
let detailModal: ReturnType<typeof overlay.create<typeof FileDetailModal>> | null = null;
const { open: openFilePreview, close: closeFilePreview } = useFileDryRun((id) => {
  void openFileDetail(id);
});
const openFileDryRun = async (id: number): Promise<void> => {
  if (!report.value || !props.open || disposed) return;
  const flowId = report.value.flow_id;
  const request = ++fileRequest;
  closeFilePreview();
  try {
    const file = await loadDetail(id);
    if (request !== fileRequest || !props.open || disposed) return;
    await openFilePreview(file, flowId);
  } catch (error) {
    if (request === fileRequest && props.open && !disposed)
      notify.error(error instanceof Error ? error.message : 'Could not load file.');
  }
};
const closeFileDetail = (open: boolean): void => {
  if (open) return;
  detailRequest += 1;
  detailOpen.value = false;
  detail.value = null;
  rawOpen.value = false;
  detailModal?.close();
  detailModal = null;
};
const detailProps = computed(() => ({
  open: detailOpen.value,
  detail: detail.value,
  rawOpen: rawOpen.value,
  readOnly: true,
  healthChecking: false,
  transcoding: false,
  'onUpdate:open': closeFileDetail,
  'onUpdate:rawOpen': (value: boolean) => (rawOpen.value = value),
}));
const openFileDetail = async (id: number): Promise<void> => {
  closeFileDetail(false);
  const request = ++detailRequest;
  detailOpen.value = true;
  detailModal = overlay.create(FileDetailModal, { props: detailProps.value, destroyOnClose: true });
  const modal = detailModal;
  void modal.open().result.then(() => {
    if (detailModal === modal) detailModal = null;
  });
  try {
    const file = await loadDetail(id);
    if (request === detailRequest && props.open && !disposed) detail.value = file;
  } catch (error) {
    if (request === detailRequest && props.open && !disposed) {
      closeFileDetail(false);
      notify.error(error instanceof Error ? error.message : 'Could not load file.');
    }
  }
};
watch(detailProps, (value) => detailModal?.patch(value));
const stopPreviews = (): void => {
  fileRequest += 1;
  closeFilePreview();
  closeFileDetail(false);
};
const flowItems = computed(() => flows.value.map((flow) => ({ label: flow.name, value: flow.id })));
const pageSizeItems = [50, 100, 200, 500, 1000].map((value) => ({
  label: `${value.toLocaleString('en')} per page`,
  value,
}));
const selectedFlow = computed(() => flows.value.find((flow) => flow.id === selectedFlowId.value));
const counts = computed(() =>
  Object.entries(report.value?.counts ?? {})
    .filter(([, count]) => count > 0)
    .map(([status, count]) => ({ status, count, ...statusInfo(status, 'flow') })),
);
const capturing = computed(
  () =>
    report.value?.state === 'preparing' ||
    (report.value?.state === 'cancelling' && !report.value.snapshot_complete),
);
const progressCount = computed(() =>
  capturing.value ? (report.value?.captured ?? 0) : (report.value?.processed ?? 0),
);
const progress = computed(() =>
  report.value?.total ? (progressCount.value / report.value.total) * 100 : undefined,
);
const progressLabel = computed(() =>
  capturing.value && !report.value?.total
    ? `${progressCount.value.toLocaleString('en')} files included`
    : `${progressCount.value.toLocaleString('en')} / ${(report.value?.total ?? 0).toLocaleString('en')} ${capturing.value ? 'included' : 'evaluated'}`,
);
const pageLabel = computed(() => {
  const offset = report.value?.pagination.offset ?? 0;
  return rows.value.length
    ? `${(offset + 1).toLocaleString('en')}–${(offset + rows.value.length).toLocaleString('en')} of ${report.value?.pagination.total.toLocaleString('en')}`
    : '0 files shown';
});
const exportDisabledReason = computed(() =>
  active.value || starting.value || cancelling.value
    ? 'Available when the dry run stops.'
    : loading.value
      ? 'Wait for the report to load.'
      : !report.value?.snapshot_complete
        ? 'Not all files were included in the dry run.'
        : '',
);
const startDisabledReason = computed(() =>
  starting.value || cancelling.value
    ? 'Wait for the current request.'
    : loading.value || flowsLoading.value
      ? 'Wait for loading to finish.'
      : exporting.value
        ? 'Wait for the export to finish.'
        : !selectedFlow.value
          ? 'Pick a saved flow first.'
          : '',
);
const multipleRoots = computed(
  () =>
    ('library' in props.scope
      ? props.scope.library.roots.length > 1
      : new Set(props.scope.files.map((file) => file.library_id)).size > 1) ||
    new Set(report.value?.items.map((row) => row.root)).size > 1,
);
const fileName = (path: string): string => path.split(/[\\/]/).at(-1) || path;
const rows = computed(() =>
  (report.value?.items ?? []).map((row) => ({
    ...row,
    name: fileName(row.file),
    relativePath: row.root ? row.file : fileName(row.file),
    folder: row.root ? relativeDirectory(`${row.root}/${row.file}`, [row.root]) : '',
    rootName: fileName(row.root),
    deferred:
      row.deferred_output && row.deferred_encoder
        ? 'Later output decisions and encoder selection deferred.'
        : row.deferred_output
          ? 'Later output decisions deferred.'
          : row.deferred_encoder
            ? 'Encoder selection deferred.'
            : '',
  })),
);
const close = (value: boolean): void => {
  if (!value) {
    stop();
    stopPreviews();
  }
  emit('update:open', value);
};
onMounted(async () => {
  await Promise.all([ensureFlows(), loadReport(props.scope)]);
  if (!props.open || disposed) return;
  const boundFlows =
    'library' in props.scope
      ? [props.scope.library.flow_id]
      : props.scope.files.map(
          (file) => libraries.value.find((library) => library.id === file.library_id)?.flow_id,
        );
  const id = report.value?.flow_id ?? (new Set(boundFlows).size === 1 ? boundFlows[0] : undefined);
  if (flows.value.some((flow) => flow.id === id)) selectedFlowId.value = id ?? undefined;
});
watch(
  () => props.open,
  (value) => {
    if (!value) {
      stop();
      stopPreviews();
    }
  },
);
onBeforeUnmount(() => {
  disposed = true;
  stop();
  stopPreviews();
});
const run = async (): Promise<void> => {
  if (props.open && !disposed && selectedFlow.value && !flowsLoading.value)
    await start(selectedFlow.value);
};
const changePage = async (value: number | undefined, size = pageSize.value): Promise<void> => {
  if (value === undefined) return;
  const id = report.value?.id;
  const previousPage = page.value;
  const previousSize = pageSize.value;
  await loadPage(value, size);
  await nextTick();
  if (
    props.open &&
    !disposed &&
    !error.value &&
    report.value?.id === id &&
    (previousPage !== value || previousSize !== size) &&
    page.value === value &&
    report.value?.pagination.offset === (value - 1) * size &&
    report.value?.pagination.limit === size
  )
    results.value?.scrollIntoView({ block: 'start' });
};
const cancelRun = async (): Promise<void> => {
  if (confirmingCancel.value || !active.value) return;
  const id = report.value?.id;
  confirmingCancel.value = true;
  try {
    const result = await confirmDialog({
      title: 'Cancel dry run?',
      message: 'Completed results will remain available. Remaining files will not be evaluated.',
      confirmText: 'Cancel dry run',
      confirmIcon: 'i-lucide-square',
    });
    if (result.status && props.open && !disposed && report.value?.id === id) await cancel();
  } finally {
    confirmingCancel.value = false;
  }
};
const download = async (): Promise<void> => {
  const id = report.value?.id;
  const blob = await exportCsv();
  if (!blob || !id || !props.open || disposed) return;
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement('a');
    link.href = url;
    link.download = `batch-dry-run-${id}.csv`;
    link.click();
  } finally {
    URL.revokeObjectURL(url);
  }
};
</script>
