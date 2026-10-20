<template>
  <UModal
    :open="modalOpen"
    :title="library ? `Edit ${library.name}` : 'New library'"
    :dismissible="!saving"
    :close="{ disabled: saving }"
    :ui="{
      content: 'w-full sm:max-w-2xl lg:max-w-4xl',
      body: 'max-h-[85vh] overflow-y-auto p-4 sm:p-6',
    }"
    @update:open="handleOpenChange"
  >
    <template #body>
      <USkeleton v-if="timezoneLoading" class="h-32 rounded-lg" />
      <UAlert
        v-else-if="!timezone"
        color="error"
        variant="soft"
        icon="i-lucide-triangle-alert"
        title="Could not load the configured timezone"
        description="Retry before editing library settings. Cutoff dates use the server timezone."
      >
        <template #actions>
          <UButton color="neutral" variant="outline" label="Retry" @click="loadTimezone" />
        </template>
      </UAlert>
      <fieldset :disabled="saving" class="contents">
        <LibraryForm
          v-if="timezone"
          ref="form"
          :key="formKey"
          :library="library"
          :flow-items="flowItems"
          :error="error"
          @submit="emit('submit', $event)"
        />
      </fieldset>
    </template>
    <template #footer>
      <div class="flex w-full flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <UButton
          type="submit"
          form="libraryForm"
          color="primary"
          icon="i-lucide-save"
          :loading="saving"
          :disabled="!timezone"
          class="justify-center"
          >Save</UButton
        >
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import LibraryForm from '~/components/LibraryForm.vue';
import type { LibraryBody, LibraryRow } from '~/types/api';
import { dateTimezone as timezone } from '~/utils/date';

const props = defineProps<{
  open: boolean;
  library: LibraryRow | null;
  flowItems: Array<{ label: string; value: number }>;
  formKey: number;
  error: string | null;
  saving: boolean;
}>();
const emit = defineEmits<{ 'update:open': [open: boolean]; submit: [payload: LibraryBody] }>();
const modalOpen = ref(props.open);
const form = ref<InstanceType<typeof LibraryForm>>();
const timezoneLoading = ref(true);
const { load: loadSystem } = useSystem();
const loadTimezone = async (): Promise<void> => {
  timezoneLoading.value = true;
  try {
    await loadSystem();
  } finally {
    timezoneLoading.value = false;
  }
};
onMounted(() => {
  if (timezone.value) timezoneLoading.value = false;
  else void loadTimezone();
});
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
