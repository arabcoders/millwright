<template>
  <UModal
    :open="open"
    :title="flow.name"
    :dismissible="!savingPositions"
    :close="{ disabled: savingPositions }"
    :fullscreen="fullscreen"
    :ui="{
      content: [
        fullscreen ? '' : 'max-w-7xl',
        'data-[state=open]:animate-[fade-in_200ms_var(--ease-out)]',
      ],
      wrapper: 'min-w-0 flex-1',
      title: 'wrap-anywhere',
      close: 'static',
      body: fullscreen
        ? 'min-h-0 overflow-y-auto p-4 sm:p-6'
        : 'max-h-[75vh] overflow-y-auto p-4 sm:p-6',
    }"
    @update:open="handleOpenChange"
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
      <div :class="fullscreen ? 'flex h-full min-h-0 flex-col gap-5' : 'space-y-5'">
        <dl
          class="mw-card grid shrink-0 grid-cols-2 gap-x-6 gap-y-3 px-4 py-3 text-sm sm:grid-cols-4"
        >
          <div class="min-w-0 space-y-1">
            <dt class="flex items-center gap-1.5 text-xs text-muted">
              <UIcon name="i-lucide-history" class="size-3.5 shrink-0" aria-hidden="true" />Revision
            </dt>
            <dd class="tabular-nums">{{ flow.version }}</dd>
          </div>
          <div class="min-w-0 space-y-1">
            <dt class="flex items-center gap-1.5 text-xs text-muted">
              <UIcon name="i-lucide-workflow" class="size-3.5 shrink-0" aria-hidden="true" />Nodes
            </dt>
            <dd class="tabular-nums">{{ flow.definition.nodes.length }}</dd>
          </div>
          <div class="min-w-0 space-y-1">
            <dt class="flex items-center gap-1.5 text-xs text-muted">
              <UIcon name="i-lucide-git-branch" class="size-3.5 shrink-0" aria-hidden="true" />Edges
            </dt>
            <dd class="tabular-nums">{{ flow.definition.edges.length }}</dd>
          </div>
          <div class="min-w-0 space-y-1">
            <dt class="flex items-center gap-1.5 text-xs text-muted">
              <UIcon
                name="i-lucide-folder-tree"
                class="size-3.5 shrink-0"
                aria-hidden="true"
              />Libraries
            </dt>
            <dd class="tabular-nums">{{ bindingCount }}</dd>
          </div>
        </dl>
        <DecisionTraceFlow
          :definition="flow.definition"
          :plugins="plugins"
          :fullscreen="fullscreen"
          :movable="!savingPositions"
          :class="fullscreen ? 'min-h-0 flex-1' : ''"
          @move="forwardMove"
        />
      </div>
    </template>
    <template #footer>
      <div class="flex w-full flex-wrap items-center justify-between gap-3">
        <UAlert
          v-if="error"
          class="w-full"
          color="error"
          variant="soft"
          icon="i-lucide-circle-alert"
          title="Could not save positions"
          :description="error"
          :ui="{ description: 'whitespace-pre-wrap wrap-anywhere' }"
        />
        <div class="flex flex-wrap items-center gap-2">
          <UDropdownMenu :items="arrangeItems">
            <UButton
              type="button"
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-layout-dashboard"
              trailing-icon="i-lucide-chevron-down"
              :disabled="savingPositions"
              class="justify-center"
              >Arrange</UButton
            >
          </UDropdownMenu>
          <UDropdownMenu
            :modal="false"
            :items="[
              {
                label: 'Copy share code',
                icon: 'i-lucide-copy',
                onSelect: () => $emit('copy'),
              },
              {
                label: 'Export flow',
                icon: 'i-lucide-download',
                onSelect: () => $emit('download'),
              },
            ]"
          >
            <UButton
              type="button"
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-share-2"
              trailing-icon="i-lucide-chevron-down"
              class="justify-center"
              >Share</UButton
            >
          </UDropdownMenu>
        </div>
        <div class="ml-auto flex flex-wrap items-center justify-end gap-2">
          <UButton
            type="button"
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-pen-line"
            :disabled="savingPositions"
            class="justify-center"
            @click="$emit('edit', flow)"
          >
            Edit
          </UButton>
          <UButton
            type="button"
            color="primary"
            size="sm"
            icon="i-lucide-save"
            :loading="savingPositions"
            :disabled="!positionsDirty || savingPositions"
            class="justify-center"
            @click="$emit('save-positions')"
          >
            Save positions
          </UButton>
        </div>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useStorage } from '@vueuse/core';
import DecisionTraceFlow from '~/components/DecisionTraceFlow.vue';
import { useDirtyCloseGuard } from '~/composables/useDirtyCloseGuard';
import type { FlowRow, FlowUiNodePos, PluginRow } from '~/types/api';
import type { FlowOrientation } from '~/utils/flowLayout';

const props = defineProps<{
  open: boolean;
  flow: FlowRow;
  bindingCount: number;
  positionsDirty: boolean;
  savingPositions: boolean;
  error: string;
  plugins: PluginRow[];
}>();
const emit = defineEmits<{
  'update:open': [open: boolean];
  edit: [flow: FlowRow];
  copy: [];
  download: [];
  move: [nodeId: string, position: FlowUiNodePos];
  arrange: [orientation: FlowOrientation];
  'save-positions': [];
}>();
const modalOpen = ref(props.open);
const fullscreen = useStorage('flow_details_fullscreen', false);
watch(
  () => props.open,
  (value) => (modalOpen.value = value),
);
const { handleOpenChange } = useDirtyCloseGuard(
  modalOpen,
  computed(() => props.positionsDirty && !props.savingPositions),
  () => emit('update:open', false),
);
const arrangeItems = [
  {
    label: 'Vertical',
    icon: 'i-lucide-arrow-down',
    onSelect: () => emit('arrange', 'vertical'),
  },
  {
    label: 'Horizontal',
    icon: 'i-lucide-arrow-right',
    onSelect: () => emit('arrange', 'horizontal'),
  },
];
const forwardMove = (nodeId: string, position: FlowUiNodePos): void =>
  emit('move', nodeId, position);
</script>
