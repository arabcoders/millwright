<template>
  <UModal
    :open="open"
    :title="target ? 'Edit notification target' : 'New notification target'"
    :dismissible="!saving"
    :close="{ disabled: saving }"
    :ui="{ content: 'w-full sm:max-w-lg', body: 'max-h-[80vh] overflow-y-auto p-4 sm:p-5' }"
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
            icon="i-lucide-bell"
            class="w-full"
            autocomplete="off"
            :disabled="Boolean(target) || saving"
            required
          />
        </UFormField>
        <UFormField label="Apprise URL" required>
          <UInput
            v-model="url"
            icon="i-lucide-link"
            :disabled="saving"
            class="w-full"
            autocomplete="off"
            required
          />
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
        <p v-if="saveError" class="wrap-anywhere text-sm text-error">{{ saveError }}</p>
      </form>
    </template>
    <template #footer>
      <div class="flex w-full flex-wrap items-center justify-end gap-2">
        <UButton
          type="button"
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-x"
          :disabled="saving"
          @click="handleOpenChange(false)"
          >Cancel</UButton
        >
        <UButton
          type="submit"
          form="notificationTargetForm"
          size="sm"
          icon="i-lucide-save"
          :loading="saving"
        >
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
