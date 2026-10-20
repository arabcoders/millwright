<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="flows">
      <template #actions>
        <div class="flex flex-wrap items-center gap-2">
          <UButton
            size="sm"
            color="neutral"
            variant="outline"
            icon="i-lucide-plus"
            @click="openEditor()"
            >New flow</UButton
          >
          <UButton
            size="sm"
            color="neutral"
            variant="outline"
            icon="i-lucide-upload"
            @click="importOpen = true"
          >
            Import flow
          </UButton>
          <UButton
            v-if="flows.length"
            size="sm"
            color="neutral"
            variant="outline"
            icon="i-lucide-folder-down"
            :loading="exportLoading"
            @click="exportFlows"
          >
            Export all
          </UButton>

          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-refresh-cw"
            aria-label="Refresh flows"
            :loading="loading"
            @click="loadFlows"
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
      title="Could not load flows"
      :description="loadError"
    />
    <div v-if="loading" class="space-y-2">
      <USkeleton v-for="index in 4" :key="index" class="h-20 rounded-xl" />
    </div>
    <div v-else-if="flows.length" class="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
      <article v-for="flow in flows" :key="flow.id" class="mw-card flex h-full flex-col gap-3 p-4">
        <div class="min-w-0">
          <h2 class="truncate font-semibold text-highlighted">
            <button
              type="button"
              class="truncate text-start hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
              @click="openDetails(flow)"
            >
              {{ flow.name }}
            </button>
          </h2>
        </div>
        <div class="grid grid-cols-4 gap-3 text-sm">
          <p class="truncate"><span class="text-toned">Revision:</span> {{ flow.version }}</p>
          <p class="truncate">
            <span class="text-toned">Nodes:</span>
            {{ flow.definition.nodes.length }}
          </p>
          <p class="truncate">
            <span class="text-toned">Edges:</span>
            {{ flow.definition.edges.length }}
          </p>
          <p class="truncate">
            <span class="text-toned">Libraries:</span> {{ bindingCount(flow) }}
          </p>
        </div>
        <div class="mt-auto flex items-center justify-between gap-2" @click.stop>
          <p class="min-w-0 truncate text-xs text-toned">
            Updated
            <UTooltip :text="formatDate(flow.updated_at, 'detail')">
              <time :datetime="toIsoString(flow.updated_at)">{{
                formatRelativeTime(flow.updated_at, 'en')
              }}</time>
            </UTooltip>
          </p>
          <div class="flex shrink-0 items-center gap-2">
            <UButton
              size="sm"
              color="neutral"
              variant="outline"
              icon="i-lucide-pen-line"
              @click="openEditor(flow)"
              >Edit</UButton
            >
            <UDropdownMenu :items="flowActions(flow)" :modal="false">
              <UButton
                color="neutral"
                variant="outline"
                size="xs"
                icon="i-lucide-settings-2"
                trailing-icon="i-lucide-chevron-down"
              >
                Actions
              </UButton>
            </UDropdownMenu>
          </div>
        </div>
      </article>
    </div>
    <div
      v-else-if="!lastError"
      class="mw-card flex flex-col items-center justify-center gap-1 py-10 text-center"
    >
      <UIcon name="i-lucide-workflow" class="size-6 text-toned" />
      <p class="text-sm text-toned">No flows saved yet.</p>
      <UButton
        class="mt-2"
        size="sm"
        color="neutral"
        variant="outline"
        icon="i-lucide-book-open"
        @click="openPresets"
        >Start with a preset</UButton
      >
    </div>

    <FlowDetailsModal
      v-if="selectedFlow"
      :open="!!selectedFlow"
      :flow="selectedFlow"
      :plugins="plugins"
      :binding-count="bindingCount(selectedFlow)"
      @update:open="closeDetails"
      @edit="openEditor"
      @copy="copyFlow(selectedFlow)"
      @download="downloadFlow(selectedFlow)"
      :saving-positions="layoutSaving"
      :positions-dirty="layoutDirty"
      @move="moveFlowNode"
      @arrange="arrangeFlow"
      @save-positions="saveLayout"
    />

    <FlowEditorModal
      ref="editor"
      v-if="editorOpen"
      :open="editorOpen"
      :title="editorTitle"
      :flow="editingFlow"
      :plugins="plugins"
      :codec-options="codecOptions"
      :facts="facts"
      :notification-targets="notificationTargetNames"
      :notification-target-error="notificationTargetError ?? ''"
      :editor-key="editorKey"
      :saving="saving"
      @update:open="handleOpenChange"
      @presets="openPresets"
      @submit="saveFlow"
    />

    <FlowImportModal
      v-if="importOpen"
      :open="importOpen"
      @update:open="importOpen = $event"
      @import="openEditor"
    />

    <FlowPresetModal
      v-if="presetOpen"
      :open="presetOpen"
      :presets="presets"
      @update:open="presetOpen = $event"
      @select="usePreset"
    />
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, toRaw, watch } from 'vue';
import type { DropdownMenuItem } from '@nuxt/ui';
import FlowDetailsModal from '~/components/FlowDetailsModal.vue';
import FlowEditorModal from '~/components/FlowEditorModal.vue';
import FlowImportModal from '~/components/FlowImportModal.vue';
import FlowPresetModal from '~/components/FlowPresetModal.vue';
import { copyText } from '~/utils';
import { encodeFlow, flowExport, hasFlowPasswords } from '~/utils/flowTransfer';
import { formatDate, toIsoString } from '~/utils/date';
import { formatRelativeTime } from '~/utils/relativeTime';
import { arrangeFlow as arrangeDefinition } from '~/utils/flowLayout';
import type { FlowOrientation } from '~/utils/flowLayout';
import { useDialog } from '~/composables/useDialog';
import { useFlows } from '~/composables/useFlows';
import { useLibraries } from '~/composables/useLibraries';
import { useNodes } from '~/composables/useNodes';
import { useNotificationTargets } from '~/composables/useNotificationTargets';
import { usePlugins } from '~/composables/usePlugins';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import type { CodecOptions, FlowDefinition, FlowPreset, FlowRow, FlowUiNodePos } from '~/types/api';

const { confirmDialog } = useDialog();
const notify = useNotification();
const { messageFor } = useApiErrorMessage();

const route = useRoute();
const {
  items: flows,
  isLoading: loading,
  lastError,
  ensure: ensureFlows,
  reload: reloadFlows,
  facts,
  presets,
  ensureFacts,
  ensurePresets,
  save: saveFlowRequest,
  removeSaved,
  saveLayout: saveLayoutRequest,
  exportZip,
} = useFlows();
const loadError = computed(() =>
  messageFor(
    lastError.value?.detail,
    lastError.value instanceof Error
      ? lastError.value.message
      : `Request failed${lastError.value?.status ? ` (HTTP ${lastError.value.status})` : ''}. Try again.`,
  ),
);
const { items: libraries, ensure: ensureLibraries } = useLibraries();
const { items: plugins, ensure: ensurePlugins, lastError: pluginError } = usePlugins();
const { items: codecNodes, reload: reloadCodecNodes } = useNodes();
const codecOptions = computed<CodecOptions>(() => {
  const nodes = codecNodes.value.filter((node) => node.state === 'online');
  const collect = (field: 'video_encoders' | 'audio_encoders' | 'audio_decoders') =>
    [...new Set(nodes.flatMap((node) => node.capabilities?.[field] ?? []))].sort();
  return {
    videoEncoders: collect('video_encoders'),
    audioEncoders: collect('audio_encoders'),
    audioDecoders: collect('audio_decoders'),
  };
});
const {
  items: notificationTargets,
  ensure: ensureNotificationTargets,
  lastError: targetError,
} = useNotificationTargets();
const notificationTargetError = computed(() =>
  targetError.value
    ? `${targetError.value} Close and reopen the editor after fixing the connection.`
    : '',
);

const loadFlows = async (): Promise<void> => {
  await reloadFlows();
  const id = Number(route.query.flow);
  if (Number.isInteger(id)) openDetails(flows.value.find((flow) => flow.id === id));
};

watch(
  () => route.query.flow,
  (value) => {
    const id = Number(value);
    if (Number.isInteger(id)) openDetails(flows.value.find((flow) => flow.id === id));
  },
);

onMounted(async () => {
  await Promise.allSettled([ensureFlows(), ensureLibraries(), ensurePlugins()]);
  const id = Number(route.query.flow);
  if (Number.isInteger(id)) openDetails(flows.value.find((flow) => flow.id === id));
});

const selectedFlow = ref<FlowRow | null>(null);
function clone<T>(value: T): T {
  return structuredClone(toRaw(value));
}

const bindingCount = (flow: FlowRow): number =>
  libraries.value.filter((library) => library.flow_id === flow.id).length;

const openDetails = (flow: FlowRow | undefined): void => {
  if (!flow) return;
  selectedFlow.value = clone(flow);
  layoutDirty.value = false;
  void ensurePlugins();
};

const closeDetails = (open: boolean): void => {
  if (!open) selectedFlow.value = null;
};

const layoutSaving = ref(false);
const layoutDirty = ref(false);
let layoutRevision = 0;
const moveFlowNode = (nodeId: string, position: FlowUiNodePos): void => {
  if (!selectedFlow.value) return;
  const definition = clone(selectedFlow.value.definition);
  definition._ui ??= {};
  definition._ui.nodes ??= {};
  definition._ui.nodes[nodeId] = position;
  selectedFlow.value.definition = definition;
  layoutRevision += 1;
  layoutDirty.value = true;
};
const arrangeFlow = (orientation: FlowOrientation): void => {
  if (!selectedFlow.value) return;
  const definition = clone(selectedFlow.value.definition);
  definition._ui = {
    ...(definition._ui ?? {}),
    nodes: arrangeDefinition(definition, orientation),
  };
  selectedFlow.value.definition = definition;
  layoutRevision += 1;
  layoutDirty.value = true;
};
const saveLayout = async (): Promise<void> => {
  if (!selectedFlow.value || !layoutDirty.value) return;
  layoutSaving.value = true;
  const flow = selectedFlow.value;
  const revision = layoutRevision;
  const definition = clone(flow.definition);
  const result = await saveLayoutRequest(flow, definition);
  if (!result.ok) {
    notify.error(messageFor(result.error.detail));
    layoutSaving.value = false;
    return;
  }
  notify.success('Positions saved.');
  const server = result.server;
  try {
    if (selectedFlow.value?.id === flow.id) {
      selectedFlow.value.version = server.version ?? selectedFlow.value.version;
      selectedFlow.value.updated_at = server.updated_at ?? selectedFlow.value.updated_at;
      if (layoutRevision === revision) layoutDirty.value = false;
    }
  } finally {
    layoutSaving.value = false;
  }
};

const confirmFlowExport = async (flow: FlowRow, confirmText: string): Promise<boolean> => {
  await ensurePlugins();
  if (pluginError.value) {
    notify.error(`Could not check flow password fields. ${pluginError.value.message}`);
    return false;
  }
  if (!hasFlowPasswords(flow, plugins.value)) return true;
  return (
    await confirmDialog({
      title: 'Credentials will be included',
      message:
        'This flow contains populated password fields. Their saved values will be included without encryption. Anyone with the exported file or share code can read them.\n\nRemove passwords and API keys before sharing the flow publicly.',
      confirmText,
      confirmColor: 'warning',
      confirmIcon: 'i-lucide-triangle-alert',
    })
  ).status;
};

const downloadFlow = async (flow: FlowRow): Promise<void> => {
  if (!(await confirmFlowExport(flow, 'Export with credentials'))) return;
  // Exports omit server-only identity, version, and timestamps.
  const payload = flowExport(flow);
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: 'application/json',
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `${flow.name}.flow.json`;
  link.click();
  URL.revokeObjectURL(url);
};
const exportLoading = ref(false);
const exportFlows = async (): Promise<void> => {
  if (!flows.value.length || exportLoading.value) return;
  exportLoading.value = true;
  try {
    if (
      !(
        await confirmDialog({
          title: 'Export all flows?',
          message:
            'This archive includes the complete flow configurations. Any passwords and API keys stored in them are included without encryption.\n\nRemove credentials before sharing the archive publicly.',
          confirmText: 'Export all flows',
          confirmColor: 'warning',
          confirmIcon: 'i-lucide-triangle-alert',
        })
      ).status
    )
      return;
    const url = URL.createObjectURL(await exportZip());
    const link = document.createElement('a');
    link.href = url;
    link.download = 'flows.zip';
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
  } finally {
    exportLoading.value = false;
  }
};
const copyFlow = async (flow: FlowRow): Promise<void> => {
  if (!(await confirmFlowExport(flow, 'Copy with credentials'))) return;
  copyText(encodeFlow(flow));
};
const importOpen = ref(false);
const presetOpen = ref(false);

const removeFlow = async (flow: FlowRow): Promise<void> => {
  if (loading.value) return;
  const { status } = await confirmDialog({
    title: `Delete flow "${flow.name}"`,
    message: 'Remove this flow from its libraries first if it is still in use.',
    confirmColor: 'error',
    confirmIcon: 'i-lucide-trash-2',
  });
  if (!status || loading.value) return;

  const result = await removeSaved(flow);
  if (result.ok) notify.success(`Flow "${flow.name}" deleted.`);
  else notify.error(messageFor(result.error.detail));
};

const editorOpen = ref(false);
const editor = ref<InstanceType<typeof FlowEditorModal>>();
const editingFlow = ref<FlowRow | null>(null);
const editorKey = ref(0);
const saving = ref(false);

const openEditor = (flow?: FlowRow): void => {
  if (saving.value) return;
  selectedFlow.value = null;
  editingFlow.value = flow ?? null;
  editorKey.value += 1;
  editorOpen.value = true;
  void Promise.allSettled([
    ensurePlugins(),
    ensureFacts(),
    ensureNotificationTargets(),
    reloadCodecNodes(),
  ]);
};

const usePreset = async (preset: FlowPreset): Promise<void> => {
  if (saving.value) return;
  const key = editorKey.value;
  if (editorOpen.value && editor.value?.isDirty) {
    const { status } = await confirmDialog({
      title: 'Discard changes?',
      message: 'Your unsaved changes will be lost.',
      confirmText: 'Discard changes',
      confirmColor: 'warning',
    });
    if (!status) return;
  }
  if (saving.value || key !== editorKey.value) return;
  const names = new Set(flows.value.map((flow) => flow.name));
  const base = preset.name;
  let name = base;
  for (let suffix = 2; names.has(name); suffix += 1) name = `${base} ${suffix}`;
  openEditor({
    id: 0,
    created_at: '',
    name,
    version: 0,
    definition: clone(preset.definition),
    updated_at: '',
  });
  presetOpen.value = false;
};

const openPresets = (): void => {
  if (saving.value) return;
  presetOpen.value = true;
  void ensurePresets().catch(() => undefined);
};

const notificationTargetNames = computed(() =>
  notificationTargets.value.map((target) => target.name),
);

const editorTitle = computed((): string => {
  if (!editingFlow.value) return 'New flow';
  return editingFlow.value.id
    ? `Edit ${editingFlow.value.name}`
    : `Import ${editingFlow.value.name || 'flow'}`;
});

const handleOpenChange = (open: boolean): void => {
  if (!open && !saving.value) {
    editorOpen.value = false;
    editingFlow.value = null;
  }
};

const saveFlow = async (payload: { name: string; definition: FlowDefinition }): Promise<void> => {
  if (saving.value) return;
  saving.value = true;
  try {
    let saved = await saveFlowRequest(
      payload,
      editingFlow.value?.id ? editingFlow.value : undefined,
    );
    if (!saved.ok && saved.error.status === 409 && !editingFlow.value?.id) {
      await loadFlows();
      const existing = flows.value.find((flow) => flow.name === payload.name);
      if (
        existing &&
        (
          await confirmDialog({
            title: `Replace flow "${existing.name}"?`,
            message:
              'This replaces the existing flow definition. Libraries using it will use the replacement.',
            confirmColor: 'error',
            confirmText: 'Replace',
          })
        ).status
      ) {
        saved = await saveFlowRequest(payload, existing);
      }
    }
    if (saved.ok) {
      notify.success(`Flow "${payload.name}" saved.`);
      editorOpen.value = false;
      editingFlow.value = null;
      const id = Number(route.query.flow);
      if (Number.isInteger(id)) openDetails(flows.value.find((flow) => flow.id === id));
    } else {
      notify.error(messageFor(saved.error.detail));
    }
  } finally {
    saving.value = false;
  }
};

const flowActions = (flow: FlowRow): DropdownMenuItem[][] => [
  [
    {
      label: 'Copy share code',
      icon: 'i-lucide-copy',
      onSelect: () => copyFlow(flow),
    },
    {
      label: 'Export flow',
      icon: 'i-lucide-download',
      onSelect: () => downloadFlow(flow),
    },
  ],
  [
    {
      label: 'Delete',
      icon: 'i-lucide-trash-2',
      disabled: loading.value,
      onSelect: () => void removeFlow(flow),
    },
  ],
];
</script>
