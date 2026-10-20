<template>
  <UModal
    :open="open"
    title="Import flow"
    :dismissible="!reviewing"
    :close="{ disabled: reviewing }"
    :ui="{ content: 'w-full sm:max-w-2xl', body: 'max-h-[85vh] overflow-y-auto p-4 sm:p-6' }"
    @update:open="handleOpenChange"
  >
    <template #body>
      <fieldset :disabled="reviewing" :inert="reviewing" class="space-y-4">
        <div>
          <div class="mb-1 flex items-center justify-between gap-2">
            <p class="text-xs font-semibold text-toned">Share code or export</p>
            <UButton
              color="neutral"
              variant="ghost"
              size="xs"
              icon="i-lucide-upload"
              @click="dropzone?.triggerFileSelect()"
              >Choose JSON file</UButton
            >
          </div>
          <TextDropzone
            ref="dropzone"
            v-model="code"
            placeholder="Paste a flow share code or JSON export"
            accept="application/json,.json"
            :max-size="MAX"
            @error="error = $event"
          />
        </div>
        <p v-if="error" class="text-sm text-error">{{ error }}</p>
      </fieldset>
    </template>
    <template #footer>
      <div class="flex w-full flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <UButton
          color="primary"
          icon="i-lucide-search"
          class="justify-center"
          :loading="reviewing"
          @click="reviewCode"
          >Review import</UButton
        >
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import TextDropzone from './TextDropzone.vue';
import { useDirtyCloseGuard } from '~/composables/useDirtyCloseGuard';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import { useFlows, type FlowError } from '~/composables/useFlows';
import { decodeFlow, importedFlow } from '~/utils/flowTransfer';
import type { FlowRow } from '~/types/api';

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ 'update:open': [open: boolean]; import: [flow: FlowRow] }>();
const code = ref('');
const error = ref('');
const reviewing = ref(false);
const { normalize } = useFlows();
const { messageFor } = useApiErrorMessage();
const dropzone = ref<InstanceType<typeof TextDropzone> | null>(null);
const MAX = 1024 * 1024;
const modalOpen = ref(props.open);
watch(
  () => props.open,
  (value, previous) => {
    modalOpen.value = value;
    if (value && !previous) {
      code.value = '';
      error.value = '';
    }
  },
);
const { handleOpenChange } = useDirtyCloseGuard(
  modalOpen,
  computed(() => Boolean(code.value.trim())),
  () => emit('update:open', false),
);
const accept = async (parsed: unknown): Promise<void> => {
  const flow = importedFlow(parsed);
  if (!flow) {
    error.value = 'This is not a valid share code or flow export.';
    return;
  }
  reviewing.value = true;
  error.value = '';
  try {
    flow.definition = await normalize(flow.definition);
    code.value = '';
    emit('import', flow);
    emit('update:open', false);
  } catch (cause) {
    error.value = messageFor(
      (cause as FlowError)?.detail,
      cause instanceof Error ? cause.message : 'Could not prepare this flow for import.',
    );
  } finally {
    reviewing.value = false;
  }
};
const reviewCode = async (): Promise<void> => {
  if (reviewing.value) return;
  const raw = code.value.trim();
  if (!raw) {
    error.value = 'Enter a share code or flow export first.';
    return;
  }
  if (raw.length > MAX) {
    error.value = 'The input is too large.';
    return;
  }
  let parsed: unknown;
  try {
    parsed = decodeFlow(raw);
  } catch {
    try {
      parsed = JSON.parse(raw);
    } catch {
      error.value = 'This is not a valid share code or flow export.';
      return;
    }
  }
  await accept(parsed);
};
</script>
