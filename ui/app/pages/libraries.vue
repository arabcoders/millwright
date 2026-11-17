<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="libraries">
      <template #actions>
        <div class="flex flex-wrap items-center gap-2">
          <UButton
            size="sm"
            color="primary"
            variant="solid"
            icon="i-lucide-plus"
            @click="openEditor()"
            >New library</UButton
          >

          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-refresh-cw"
            aria-label="Refresh libraries"
            :loading="isLoading"
            @click="refreshAll"
          >
            Refresh
          </UButton>
        </div>
      </template>
    </PageHeader>
    <UAlert
      v-if="lastError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load libraries"
      :description="lastError.message"
    />
    <UAlert
      v-if="flowError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load library flows"
      :description="flowError.detail || 'Try Refresh to load the available flows.'"
    />
    <UAlert
      v-if="progressError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load library activity"
      :description="progressError.message"
    />
    <div v-if="isLoading && !libraries.length" class="space-y-2">
      <USkeleton v-for="index in 2" :key="index" class="h-40 rounded-xl" />
    </div>
    <div v-else-if="libraries.length" class="mw-card divide-y divide-default">
      <article
        v-for="library in libraries"
        :key="library.id"
        class="w-full min-w-0 max-w-full space-y-4 p-4 sm:p-5"
      >
        <div class="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.5fr)_auto]">
          <div class="min-w-0 space-y-3">
            <h2 class="min-w-0 font-semibold text-highlighted">
              <NuxtLink
                :to="{ path: '/files', query: { library_id: library.id } }"
                :aria-label="`View files in ${library.name}`"
                class="wrap-anywhere hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                >{{ library.name }}</NuxtLink
              >
            </h2>
            <p class="wrap-anywhere text-xs text-toned">
              <UIcon
                name="i-lucide-workflow"
                class="me-1 inline-block size-3.5 align-text-bottom"
                aria-hidden="true"
              />
              Flow: {{ flowName(library) }}
            </p>
            <UDropdownMenu
              :modal="false"
              :items="
                library.roots.map((root) => ({
                  type: 'label' as const,
                  label: root,
                  icon: 'i-lucide-folder',
                }))
              "
              :content="{ align: 'end', side: 'bottom', sideOffset: 8 }"
              :ui="{
                content: 'w-96 max-w-[calc(100vw-1rem)]',
                viewport: 'max-h-64',
                label: 'items-start font-normal text-default',
                itemLabel: 'overflow-visible whitespace-pre-wrap break-all text-clip',
              }"
            >
              <template #default="{ open }">
                <UButton
                  color="neutral"
                  variant="outline"
                  size="sm"
                  icon="i-lucide-folder-tree"
                  :trailing-icon="open ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
                  :aria-label="`Root folders for ${library.name}`"
                >
                  Root folders
                  <UBadge color="neutral" variant="subtle" size="sm">
                    {{ library.roots.length }}
                  </UBadge>
                </UButton>
              </template>
            </UDropdownMenu>
          </div>
          <div class="min-w-0 space-y-3">
            <dl class="grid min-w-0 grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
              <div class="min-w-0 space-y-1">
                <dt class="flex items-center gap-1.5 text-xs text-muted">
                  <UIcon name="i-lucide-eye" class="size-3.5 shrink-0" aria-hidden="true" />
                  Watch
                </dt>
                <dd>{{ library.watch ? 'On' : 'Off' }}</dd>
              </div>
              <div class="min-w-0 space-y-1">
                <dt class="flex items-center gap-1.5 text-xs text-muted">
                  <UIcon name="i-lucide-clock-3" class="size-3.5 shrink-0" aria-hidden="true" />
                  Job timing
                </dt>
                <dd class="wrap-anywhere">{{ scheduleLabel(library) }}</dd>
              </div>
              <div class="min-w-0 space-y-1">
                <dt class="flex items-center gap-1.5 text-xs text-muted">
                  <UIcon name="i-lucide-timer" class="size-3.5 shrink-0" aria-hidden="true" />
                  Cooldown
                </dt>
                <dd class="wrap-anywhere">{{ library.cooldown_s }}s</dd>
              </div>
              <div class="min-w-0 space-y-1">
                <dt class="flex items-center gap-1.5 text-xs text-muted">
                  <UIcon name="i-lucide-radar" class="size-3.5 shrink-0" aria-hidden="true" />
                  Scan on start
                </dt>
                <dd>{{ library.scan_on_start ? 'On' : 'Off' }}</dd>
              </div>
              <div class="min-w-0 space-y-1">
                <dt class="flex items-center gap-1.5 text-xs text-muted">
                  <UIcon name="i-lucide-repeat-2" class="size-3.5 shrink-0" aria-hidden="true" />
                  Recurring scans
                </dt>
                <dd class="wrap-anywhere">
                  {{
                    library.scan_interval_s === null
                      ? 'Off'
                      : `Every ${library.scan_interval_s / (library.scan_interval_s % 86400 === 0 ? 86400 : 3600)} ${library.scan_interval_s % 86400 === 0 ? 'days' : 'hours'}`
                  }}
                </dd>
              </div>
              <div class="min-w-0 space-y-1">
                <dt class="flex items-center gap-1.5 text-xs text-muted">
                  <UIcon name="i-lucide-heart-pulse" class="size-3.5 shrink-0" aria-hidden="true" />
                  Health check
                </dt>
                <dd>{{ library.healthcheck_enabled ? 'On' : 'Off' }}</dd>
              </div>
              <div class="min-w-0 space-y-1">
                <dt class="flex items-center gap-1.5 text-xs text-muted">
                  <UIcon name="i-lucide-trash-2" class="size-3.5 shrink-0" aria-hidden="true" />
                  Trash
                </dt>
                <dd class="wrap-anywhere">{{ trashSummary(library) }}</dd>
              </div>
            </dl>
            <div class="grid min-w-0 gap-3 text-sm sm:grid-cols-3">
              <LibraryActivity
                v-if="libraryProgress[library.id]"
                :progress="libraryProgress[library.id]"
              />
              <USkeleton v-else-if="progressLoading" class="h-10 rounded-lg" />
            </div>
          </div>
          <div class="flex items-center justify-end gap-1.5 self-start">
            <UTooltip text="Wait for the current library action to finish." :disabled="!actionBusy">
              <span class="inline-flex items-center gap-1.5">
                <UButton
                  v-if="!library.deleted"
                  color="neutral"
                  variant="outline"
                  size="sm"
                  icon="i-lucide-pen-line"
                  :aria-label="`Edit ${library.name}`"
                  :disabled="actionBusy"
                  @click="openEditor(library)"
                  >Edit</UButton
                >
                <UDropdownMenu
                  :items="libraryActions(library)"
                  :modal="false"
                  :content="{ align: 'end' }"
                  :ui="{ content: 'w-72', itemDescription: 'whitespace-normal' }"
                >
                  <UButton
                    color="neutral"
                    variant="ghost"
                    size="sm"
                    icon="i-lucide-ellipsis"
                    square
                    :aria-label="`Actions for ${library.name}`"
                    :loading="
                      scanningId === library.id ||
                      pausingId === library.id ||
                      cancellingId === library.id ||
                      deletingId === library.id
                    "
                    :disabled="actionBusy"
                  />
                </UDropdownMenu>
              </span>
            </UTooltip>
          </div>
        </div>
        <UAlert
          v-if="library.deleted"
          :color="library.delete_error ? 'error' : 'info'"
          icon="i-lucide-trash-2"
          :description="
            library.delete_error
              ? `Deletion failed: ${library.delete_error}`
              : 'Cancelling active work and removing indexed records. User files are kept.'
          "
        />
        <UAlert
          v-if="library.suspended && !library.deleted"
          color="warning"
          icon="i-lucide-pause"
          description="Library suspended. Scanning, probing, and new processing are stopped for this library. Review roots, flow, and settings before resuming."
        />
      </article>
    </div>
    <UEmpty
      v-else-if="!lastError"
      icon="i-lucide-folder-tree"
      title="No libraries configured yet."
    />

    <LibraryEditorModal
      v-if="editorOpen"
      v-model:open="editorOpen"
      :library="editingLibrary"
      :flow-items="flowItems"
      :form-key="editorKey"
      :error="saveError"
      :saving="saving"
      @update:open="handleOpenChange"
      @submit="saveLibrary"
    />
  </main>
</template>

<script setup lang="ts">
import type { DropdownMenuItem } from '@nuxt/ui';
import { computed, onBeforeUnmount, onMounted, ref, toRaw, watch } from 'vue';
import LibraryEditorModal from '~/components/LibraryEditorModal.vue';
import BatchDryRunModal from '~/components/BatchDryRunModal.vue';
import LibraryActivity from '~/components/LibraryActivity.vue';
import { useDialog } from '~/composables/useDialog';
import { useEvents } from '~/composables/useEvents';
import { useFlows } from '~/composables/useFlows';
import { useLibraries } from '~/composables/useLibraries';
import { useLibraryProgress } from '~/composables/useLibraryProgress';
import type { LibraryBody, LibraryRow } from '~/types/api';

const notify = useNotification();
const { confirmDialog } = useDialog();
const overlay = useOverlay();
let dryModal: ReturnType<typeof overlay.create<typeof BatchDryRunModal>> | null = null;
const openDryRun = (library: LibraryRow): void => {
  if (!flows.value.length || flowError.value) return;
  dryModal?.close();
  const modal = overlay.create(BatchDryRunModal, {
    destroyOnClose: true,
    props: {
      open: true,
      scope: { library: structuredClone(toRaw(library)) },
      'onUpdate:open': (value: boolean) => {
        if (!value) modal.close();
      },
    },
  });
  dryModal = modal;
  void modal.open().result.then(() => {
    if (dryModal === modal) dryModal = null;
  });
};
onBeforeUnmount(() => dryModal?.close());

const {
  items: libraries,
  ensure: ensureLibraries,
  reload: loadLibraries,
  isLoading,
  lastError,
  save: saveLibraryRequest,
  scan: scanLibraryRequest,
  setSuspended: setLibrarySuspended,
  cancelScan: cancelScanRequest,
  deleteLibrary,
} = useLibraries();
const { items: flows, ensure: ensureFlows, reload: loadFlows, lastError: flowError } = useFlows();
const {
  items: libraryProgress,
  ensure: ensureProgress,
  load: loadProgress,
  isLoading: progressLoading,
  lastError: progressError,
  update: updateProgress,
  scanner,
  scannerBusy,
} = useLibraryProgress();

const loadAll = async (): Promise<void> => {
  await Promise.all([ensureLibraries(), ensureFlows(), ensureProgress()]);
  const id = Number(route.query.library);
  const library = libraries.value.find((item) => item.id === id);
  if (Number.isInteger(id) && library) openEditor(library);
};

const refreshAll = async (): Promise<void> => {
  await Promise.all([loadLibraries(), loadFlows(), loadProgress()]);
};

const route = useRoute();
watch(
  () => route.query.library,
  (value) => {
    const id = Number(value);
    const library = libraries.value.find((item) => item.id === id);
    if (Number.isInteger(id) && library) openEditor(library);
  },
);

onMounted(() => {
  void loadAll();
});

const flowName = (lib: LibraryRow): string => {
  if (lib.flow_id === null) return 'Not assigned';
  return flows.value.find((flow) => flow.id === lib.flow_id)?.name ?? `#${lib.flow_id}`;
};

const scheduleLabel = (lib: LibraryRow): string => {
  if (!lib.schedule) return 'Always';
  return 'Weekly schedule';
};

const trashModeLabel = (mode: LibraryRow['trash_mode']): string =>
  mode === 'none' ? 'None' : mode === 'transcode' ? 'Transcode' : 'Transcode + Remux';

const trashSummary = (library: LibraryRow): string =>
  library.trash_mode === 'none'
    ? 'None'
    : `${trashModeLabel(library.trash_mode)} · ${library.trash_retention_days} days`;

const flowItems = computed(() => [
  { label: 'No flow', value: 0 },
  ...flows.value.map((flow) => ({ label: flow.name, value: flow.id })),
]);

const scanningId = ref<number | null>(null);
const pausingId = ref<number | null>(null);
const cancellingId = ref<number | null>(null);
const deletingId = ref<number | null>(null);
const actionBusy = computed(
  () =>
    saving.value ||
    scanningId.value !== null ||
    pausingId.value !== null ||
    cancellingId.value !== null ||
    deletingId.value !== null,
);
const scannerDisabledReason = computed(() => {
  if (scannerBusy.value) return 'Wait for the activity control to finish.';
  if (progressLoading.value) return 'Loading activity state.';
  if (!scanner.value) return 'Refresh to check the current activity state.';
  return '';
});
const cancelScan = async (library: LibraryRow): Promise<void> => {
  if (cancellingId.value !== null) return;
  cancellingId.value = library.id;
  try {
    const { status } = await confirmDialog({
      title: `Cancel scan for "${library.name}"?`,
      message:
        'Keeps files already found and leaves unvisited files unchanged. Slow storage can delay cancellation. Processing continues.',
      confirmText: 'Cancel scan',
      confirmIcon: 'i-lucide-square',
    });
    if (!status) return;
    const progress = await cancelScanRequest(library);
    if (progress) updateProgress(progress);
  } finally {
    cancellingId.value = null;
  }
};
const togglePause = async (library: LibraryRow): Promise<void> => {
  if (pausingId.value !== null) return;
  const reviewed = structuredClone(toRaw(library));
  const suspended = !reviewed.suspended;
  const action = suspended ? 'Pause' : 'Resume';
  pausingId.value = library.id;
  try {
    const { status } = await confirmDialog({
      title: `${action} library "${reviewed.name}"?`,
      message: suspended
        ? 'Stops scanning, probing, and new processing. Queued jobs are kept, and running jobs are not cancelled. Any scan already running must finish before the pause is applied.'
        : scanner.value?.paused
          ? 'Activity remains paused. Resume it from the header to start new work.'
          : 'Scanning, probing, and automatic processing will resume with the current roots and flow.',
      confirmText: action,
      confirmIcon: suspended ? 'i-lucide-pause' : 'i-lucide-play',
    });
    if (!status) return;
    await setLibrarySuspended(reviewed, suspended);
  } finally {
    pausingId.value = null;
  }
};

const scanLibrary = async (lib: LibraryRow): Promise<void> => {
  if (scanningId.value !== null) return;
  scanningId.value = lib.id;
  try {
    await scanLibraryRequest(lib);
  } finally {
    scanningId.value = null;
  }
};

const libraryActions = (library: LibraryRow): DropdownMenuItem[][] => {
  const controls: DropdownMenuItem[] = [];
  if (!library.suspended && libraryProgress.value[library.id]) {
    const reason = library.deleted
      ? 'Library deletion is in progress.'
      : scanner.value?.paused
        ? 'Resume activity from the header to scan now.'
        : scannerDisabledReason.value;
    controls.push({
      label: 'Scan now',
      icon: 'i-lucide-radar',
      description: reason || undefined,
      disabled: !!reason,
      onSelect: () => scanLibrary(library),
    });
  }
  controls.push({
    label: library.suspended ? 'Resume library' : 'Pause library',
    icon: library.suspended ? 'i-lucide-play' : 'i-lucide-pause',
    description: library.deleted ? 'Library deletion is in progress.' : undefined,
    disabled: library.deleted || pausingId.value !== null,
    onSelect: () => togglePause(library),
  });
  if (flows.value.length && !flowError.value)
    controls.push({
      label: 'Dry run',
      icon: 'i-lucide-play',
      onSelect: () => openDryRun(library),
    });
  const deletion: DropdownMenuItem[] = [
    {
      label: library.delete_error ? 'Retry deletion' : 'Delete library',
      icon: 'i-lucide-trash-2',
      color: 'error',
      disabled: library.deleted && !library.delete_error,
      onSelect: () => void requestDelete(library),
    },
  ];
  const progress = libraryProgress.value[library.id];
  if (!progress) return [controls, deletion];
  const scanActions: DropdownMenuItem[] = [];
  if (progress.scanner.discovery_pending)
    scanActions.push({
      label:
        progress.discovery.status === 'cancelling'
          ? 'Cancel scan (waiting for storage I/O)'
          : 'Cancel scan',
      icon: 'i-lucide-square',
      disabled: progress.discovery.status === 'cancelling',
      onSelect: () => void cancelScan(library),
    });
  return scanActions.length ? [controls, scanActions, deletion] : [controls, deletion];
};

const requestDelete = async (library: LibraryRow): Promise<void> => {
  if (deletingId.value !== null) return;
  deletingId.value = library.id;
  try {
    const { status } = await confirmDialog({
      title: `${library.delete_error ? 'Retry deletion of' : 'Delete'} "${library.name}"?`,
      message:
        'Removes the library, index, and pending jobs after cancelling active work. User files, history, completed artifacts, and Trash are kept.',
      confirmText: library.delete_error ? 'Retry deletion' : 'Delete library',
      confirmIcon: 'i-lucide-trash-2',
      confirmColor: 'error',
    });
    if (status) await deleteLibrary(library);
  } finally {
    deletingId.value = null;
  }
};

const { on } = useEvents();
const stopEvents = on((event) => {
  if (event.type === 'library.deletion') {
    if (event.payload.status === 'deleted')
      notify.success(`Library "${event.payload.library_name}" deleted.`);
    else if (event.payload.status === 'failed')
      notify.error(
        `Deletion of "${event.payload.library_name}" failed: ${event.payload.error ?? 'unknown error'}.`,
      );
    return;
  }
  if (event.type !== 'library.scan' || event.payload.reason !== 'manual') return;
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
});

onBeforeUnmount(() => stopEvents());

const editorOpen = ref(false);
const editingLibrary = ref<LibraryRow | null>(null);
const editorKey = ref(0);
const saving = ref(false);
const saveError = ref<string | null>(null);

const openEditor = (lib?: LibraryRow): void => {
  if (saving.value) return;
  editingLibrary.value = lib ? structuredClone(toRaw(lib)) : null;
  saveError.value = null;
  editorKey.value += 1;
  editorOpen.value = true;
};

const handleOpenChange = (open: boolean): void => {
  if (!open && !saving.value) {
    editorOpen.value = false;
  }
};

const saveLibrary = async (payload: LibraryBody): Promise<void> => {
  if (saving.value) return;
  const editing = editingLibrary.value;
  saving.value = true;
  saveError.value = null;
  try {
    if (editing && editing.flow_id !== payload.flow_id) {
      const result = await confirmDialog({
        title: 'Change flow?',
        message:
          payload.flow_id === null
            ? 'New files will not be enqueued automatically until a flow is assigned.'
            : 'Healthy files may be enqueued automatically once this flow is assigned.',
        confirmText: 'Save',
      });
      if (!result.status) return;
    }
    const detail = await saveLibraryRequest(payload, editing);
    if (!detail) {
      await loadProgress();
      editorOpen.value = false;
    } else {
      saveError.value = detail;
    }
  } finally {
    saving.value = false;
  }
};
</script>
