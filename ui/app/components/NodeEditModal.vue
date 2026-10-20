<template>
  <UModal
    :open="modalOpen"
    :title="`Edit ${props.node?.display_name ?? 'node'}`"
    :dismissible="!saving"
    :close="{ disabled: saving }"
    :ui="{ content: 'w-full sm:max-w-md' }"
    @update:open="handleOpenChange"
  >
    <template #body>
      <form v-if="props.node" id="nodeLimitForm" class="space-y-2" @submit.prevent="submit">
        <UFormField label="Display name" required>
          <UInput v-model="displayName" :disabled="saving" required class="w-full" />
        </UFormField>
        <UFormField v-for="limit in limits" :key="limit.key" :label="limit.label" required>
          <UInput
            v-model.number="draft[limit.key]"
            type="number"
            :min="limit.min"
            :step="1"
            :disabled="saving"
            required
            class="w-full"
          />
        </UFormField>
      </form>
    </template>
    <template #footer>
      <div class="flex w-full flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <UButton
          type="submit"
          form="nodeLimitForm"
          icon="i-lucide-save"
          :loading="saving"
          :disabled="!valid || saving"
          :title="!valid ? 'Check the name and limits.' : undefined"
          >Save changes</UButton
        >
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { NodeChanges, NodeRow } from '~/types/api';

const props = defineProps<{
  node: NodeRow | null;
  saving: boolean;
}>();

const emit = defineEmits<{
  close: [];
  save: [value: NodeChanges];
}>();

const modalOpen = ref(Boolean(props.node));
const limits = [
  { key: 'max_transcodes', label: 'Max transcodes', min: 1 },
  { key: 'max_healthchecks', label: 'Max health checks', min: 0 },
  { key: 'max_mkv_stats', label: 'Max MKV statistics', min: 0 },
] as const;
const draft = ref({
  max_transcodes: props.node?.max_transcodes ?? 1,
  max_healthchecks: props.node?.max_healthchecks ?? 1,
  max_mkv_stats: props.node?.max_mkv_stats ?? 1,
});
const displayName = ref(props.node?.display_name ?? '');
const initial = ref({ display_name: displayName.value, ...draft.value });
watch(
  () => props.node?.id,
  () => {
    draft.value = {
      max_transcodes: props.node?.max_transcodes ?? 1,
      max_healthchecks: props.node?.max_healthchecks ?? 1,
      max_mkv_stats: props.node?.max_mkv_stats ?? 1,
    };
    displayName.value = props.node?.display_name ?? '';
    initial.value = { display_name: displayName.value, ...draft.value };
    modalOpen.value = Boolean(props.node);
  },
);
const valid = computed(() => {
  return (
    Boolean(displayName.value.trim()) &&
    limits.every(({ key, min }) => Number.isInteger(draft.value[key]) && draft.value[key] >= min)
  );
});
const dirty = computed(
  () =>
    !props.saving &&
    (limits.some(({ key }) => draft.value[key] !== initial.value[key]) ||
      displayName.value !== initial.value.display_name),
);
const { handleOpenChange } = useDirtyCloseGuard(modalOpen, dirty, () => emit('close'));
const submit = (): void => {
  if (!valid.value || !displayName.value.trim() || props.saving) return;
  const changes: NodeChanges = {};
  if (displayName.value.trim() !== initial.value.display_name)
    changes.display_name = displayName.value.trim();
  for (const { key } of limits)
    if (draft.value[key] !== initial.value[key]) changes[key] = draft.value[key];
  emit('save', changes);
};
</script>
