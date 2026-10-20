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
      body: fullscreen
        ? 'min-h-0 overflow-y-auto p-4 sm:p-6'
        : 'max-h-[75vh] overflow-y-auto p-4 sm:p-6',
    }"
    @update:open="handleOpenChange"
  >
    <template #body>
      <div :class="fullscreen ? 'flex h-full min-h-0 flex-col gap-5' : 'space-y-5'">
        <div class="flex flex-wrap items-center justify-between gap-2 text-sm text-toned">
          <div class="flex flex-wrap items-center gap-2">
            <span>Revision {{ flow.version }}</span>
            <span>{{ flow.definition.nodes.length }} nodes</span>
            <span>{{ flow.definition.edges.length }} edges</span>
            <span>{{ bindingCount }} libraries</span>
          </div>
        </div>
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
      <div class="flex w-full flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div class="flex flex-col gap-2 sm:flex-row">
          <UDropdownMenu :items="arrangeItems">
            <UButton
              type="button"
              color="neutral"
              variant="outline"
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
              icon="i-lucide-share-2"
              trailing-icon="i-lucide-chevron-down"
              class="justify-center"
              >Share</UButton
            >
          </UDropdownMenu>
          <UButton
            type="button"
            color="neutral"
            variant="outline"
            :icon="fullscreen ? 'i-lucide-minimize' : 'i-lucide-maximize'"
            class="justify-center"
            @click="fullscreen = !fullscreen"
          >
            {{ fullscreen ? 'Exit fullscreen' : 'Fullscreen' }}
          </UButton>
        </div>
        <div class="flex flex-col gap-2 sm:flex-row sm:justify-end">
          <UButton
            type="button"
            color="neutral"
            variant="outline"
            icon="i-lucide-pen-line"
            class="justify-center"
            @click="$emit('edit', flow)"
          >
            Edit
          </UButton>
          <UButton
            type="button"
            color="primary"
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
