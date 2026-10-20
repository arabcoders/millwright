<template>
  <UModal
    :open="open"
    title="Transcode"
    description="Choose a saved flow for this file."
    @update:open="$emit('update:open', $event)"
    @after:leave="$emit('after:leave')"
  >
    <template #body>
      <div class="space-y-5">
        <div class="flex min-w-0 items-start gap-3">
          <span class="mw-detail-icon size-10 shrink-0">
            <UIcon name="i-lucide-file-video" class="size-5" aria-hidden="true" />
          </span>
          <UTooltip :text="filePath">
            <p class="min-w-0 truncate text-lg font-semibold text-highlighted">
              {{ filePath.split(/[\\/]/).at(-1) || filePath }}
            </p>
          </UTooltip>
        </div>
        <UFormField label="Flow">
          <USelectMenu
            v-model="selectedFlowId"
            :items="flowItems"
            value-key="value"
            placeholder="Pick a flow"
            :loading="loading"
            :disabled="loading || !!error || !flows.length"
            class="w-full"
          />
        </UFormField>
        <UAlert
          v-if="error"
          color="error"
          variant="soft"
          icon="i-lucide-circle-alert"
          title="Could not load saved flows"
          :description="error"
        />
        <UAlert
          v-else-if="!loading && !flows.length"
          color="info"
          variant="soft"
          icon="i-lucide-info"
          title="No saved flows"
          description="Save a flow on the Flows page, then reopen Transcode."
        />
      </div>
    </template>
    <template #footer>
      <div class="flex w-full justify-end gap-2">
        <UButton color="neutral" variant="outline" @click="$emit('update:open', false)">
          Cancel
        </UButton>
        <UButton
          color="neutral"
          variant="outline"
          icon="i-lucide-play"
          :loading="previewing"
          :disabled="loading || !!error || !flow || previewing"
          @click="$emit('dry-run')"
        >
          Dry run
        </UButton>
        <UButton
          color="primary"
          icon="i-lucide-film"
          :disabled="loading || !!error || !flow || previewing"
          @click="$emit('run')"
        >
          Transcode
        </UButton>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { FlowRow } from '~/types/api';

const props = defineProps<{
  open: boolean;
  filePath: string;
  flows: Pick<FlowRow, 'id' | 'name'>[];
  loading: boolean;
  previewing: boolean;
  error: string;
}>();
const selectedFlowId = defineModel<number | undefined>('selectedFlowId', { default: undefined });
const flow = computed(() => props.flows.find((flow) => flow.id === selectedFlowId.value));
const flowItems = computed(() => props.flows.map((flow) => ({ label: flow.name, value: flow.id })));
defineEmits<{
  'update:open': [open: boolean];
  run: [];
  'dry-run': [];
  'after:leave': [];
}>();
</script>
