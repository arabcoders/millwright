<template>
  <UModal
    :open="open"
    :title="target ? 'Edit notification target' : 'New notification target'"
    :dismissible="!saving"
    :close="{ disabled: saving }"
    :ui="{ content: 'w-full sm:max-w-lg' }"
    @update:open="handleOpenChange"
    @after:leave="clearForm"
  >
    <template #body>
      <form
        id="notificationTargetForm"
        class="space-y-4"
        @submit.prevent="emit('save', { name, url })"
      >
        <UFormField label="Name" required>
          <UInput
            v-model="name"
            class="w-full"
            autocomplete="off"
            :disabled="Boolean(target) || saving"
            required
          />
        </UFormField>
        <UFormField label="Apprise URL" required>
          <UInput v-model="url" :disabled="saving" class="w-full" autocomplete="off" required />
        </UFormField>
        <p class="text-xs text-toned">
          Find your service's URL format in the
          <a
            href="https://appriseit.com/services"
            target="_blank"
            rel="noopener noreferrer"
            class="text-primary underline hover:text-primary/80"
            >Apprise documentation</a
          >.
        </p>
        <p v-if="saveError" class="text-sm text-error">{{ saveError }}</p>
      </form>
    </template>
    <template #footer>
      <div class="flex w-full flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <UButton type="submit" form="notificationTargetForm" icon="i-lucide-save" :loading="saving">
          {{ target ? 'Save changes' : 'Add target' }}
        </UButton>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useDirtyCloseGuard } from '~/composables/useDirtyCloseGuard';
import { useDirtyState } from '~/composables/useDirtyState';
import type { Target } from '~/composables/useNotificationTargets';

const props = defineProps<{
  open: boolean;
  target: Target | null;
  saving: boolean;
  saveError: string | null;
}>();
const emit = defineEmits<{
  'update:open': [open: boolean];
  save: [payload: { name: string; url: string }];
}>();
const name = ref('');
const url = ref('');
const modalOpen = ref(props.open);
const dirty = useDirtyState(computed(() => ({ name: name.value, url: url.value })));

const resetForm = (): void => {
  name.value = props.target?.name ?? '';
  url.value = props.target?.url ?? '';
  dirty.markClean();
};

const clearForm = (): void => {
  name.value = '';
  url.value = '';
  dirty.markClean();
};

watch(
  () => props.open,
  (open) => {
    modalOpen.value = open;
    if (open) resetForm();
  },
);

const close = (): void => emit('update:open', false);
const { handleOpenChange } = useDirtyCloseGuard(modalOpen, dirty.isDirty, close);
</script>
