<template>
  <UModal
    :open="modalOpen"
    :title="`Edit ${props.node?.display_name ?? 'node'}`"
    :dismissible="!saving"
    :close="{ disabled: saving }"
    :ui="{
      content: 'w-full sm:max-w-md',
      title: 'wrap-anywhere',
      body: 'max-h-[80vh] overflow-y-auto p-4 sm:p-5',
      footer: 'justify-end',
    }"
    @update:open="handleOpenChange"
  >
    <template #body>
      <form v-if="props.node" id="nodeLimitForm" class="space-y-5" @submit.prevent="submit">
        <UAlert
          v-if="saveError"
          color="error"
          variant="soft"
          icon="i-lucide-circle-alert"
          title="Could not save node"
          :description="saveError"
        />
        <UFormField
          label="Display name"
          :error="!displayName.trim() && 'Enter a display name.'"
          required
        >
          <UInput
            v-model="displayName"
            icon="i-lucide-server"
            :disabled="saving"
            required
            class="w-full"
          />
        </UFormField>
        <section class="mw-card-padded-lg space-y-4">
          <h3 class="flex items-center gap-2 font-semibold text-highlighted">
            <UIcon name="i-lucide-gauge" class="size-5 text-toned" aria-hidden="true" />
            Job limits
          </h3>
          <UFormField
            v-for="limit in limits"
            :key="limit.key"
            :label="limit.label"
            :error="
              (!Number.isInteger(draft[limit.key]) || draft[limit.key] < limit.min) &&
              `Enter a whole number of at least ${limit.min}.`
            "
            required
          >
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
        </section>
      </form>
    </template>
    <template #footer>
      <UButton
        type="submit"
        form="nodeLimitForm"
        size="sm"
        icon="i-lucide-save"
        :loading="saving"
        :disabled="!valid || saving"
        >Save changes</UButton
      >
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { NodeChanges, NodeRow } from '~/types/api';

const props = defineProps<{
  node: NodeRow | null;
  saving: boolean;
  saveError: string;
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
