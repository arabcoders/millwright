<template>
  <UModal
    :open="modalOpen"
    :title="title"
    description="Steps and conditions for processing files."
    :dismissible="!saving"
    :close="{ disabled: saving }"
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
      <fieldset :disabled="saving" :inert="saving" class="contents">
        <FlowForm
          ref="form"
          :key="editorKey"
          :flow="flow"
          :plugins="plugins"
          :codec-options="codecOptions"
          :facts="facts"
          :notification-targets="notificationTargets"
          :notification-target-error="notificationTargetError"
          :fullscreen="fullscreen"
          :disabled="saving"
          @submit="$emit('submit', $event)"
        />
      </fieldset>
    </template>
    <template #footer>
      <div class="flex w-full flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div class="flex flex-col gap-2 sm:flex-row">
          <UButton
            v-if="!flow"
            type="button"
            color="neutral"
            variant="outline"
            icon="i-lucide-book-open"
            class="justify-center"
            :disabled="saving"
            @click="$emit('presets')"
          >
            Starter presets
          </UButton>
          <UDropdownMenu :items="arrangeItems">
            <UButton
              type="button"
              color="neutral"
              variant="outline"
              icon="i-lucide-layout-dashboard"
              trailing-icon="i-lucide-chevron-down"
              :disabled="saving || !form?.canArrange()"
              class="justify-center"
              >Arrange</UButton
            >
          </UDropdownMenu>
          <UButton
            type="button"
            color="neutral"
            variant="outline"
            icon="i-lucide-circle-help"
            class="justify-center"
            :disabled="saving"
            @click="helpOpen = true"
          >
            Help
          </UButton>
          <UButton
            type="button"
            color="neutral"
            variant="outline"
            :icon="fullscreen ? 'i-lucide-minimize' : 'i-lucide-maximize'"
            class="justify-center"
            :disabled="saving"
            @click="fullscreen = !fullscreen"
          >
            {{ fullscreen ? 'Exit fullscreen' : 'Fullscreen' }}
          </UButton>
        </div>
        <div class="flex flex-col gap-2 sm:flex-row sm:justify-end">
          <UButton
            type="submit"
            form="flowForm"
            color="primary"
            icon="i-lucide-save"
            :loading="saving"
            class="justify-center"
          >
            Save
          </UButton>
        </div>
      </div>
    </template>
  </UModal>
  <FlowConnectionHelpModal v-model:open="helpOpen" />
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useStorage } from '@vueuse/core';
import FlowForm from '~/components/FlowForm.vue';
import FlowConnectionHelpModal from '~/components/FlowConnectionHelpModal.vue';
import type { CodecOptions, FlowFact, FlowRow, PluginRow } from '~/types/api';

const form = ref<InstanceType<typeof FlowForm>>();
const helpOpen = ref(false);
const fullscreen = useStorage('flow_editor_fullscreen', false);
const arrangeItems = [
  {
    label: 'Vertical',
    icon: 'i-lucide-arrow-down',
    onSelect: () => {
      if (!props.saving) form.value?.arrange('vertical');
    },
  },
  {
    label: 'Horizontal',
    icon: 'i-lucide-arrow-right',
    onSelect: () => {
      if (!props.saving) form.value?.arrange('horizontal');
    },
  },
] satisfies { label: string; icon: string; onSelect: () => void }[];

const props = defineProps<{
  open: boolean;
  title: string;
  flow: FlowRow | null;
  plugins: PluginRow[];
  codecOptions: CodecOptions;
  facts: FlowFact[];
  notificationTargets: string[];
  notificationTargetError: string;
  editorKey: number;
  saving: boolean;
}>();
const emit = defineEmits<{
  'update:open': [open: boolean];
  presets: [];
  submit: [payload: { name: string; definition: import('~/types/api').FlowDefinition }];
}>();
const modalOpen = ref(props.open);
defineExpose({ isDirty: computed(() => Boolean(form.value?.isDirty)) });
watch(
  () => props.open,
  (value) => (modalOpen.value = value),
);
const { handleOpenChange } = useDirtyCloseGuard(
  modalOpen,
  computed(() => Boolean(form.value?.isDirty)),
  () => emit('update:open', false),
);
</script>
