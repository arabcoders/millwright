<template>
  <UInput
    v-model="value"
    :type="visible ? 'text' : 'password'"
    :disabled="disabled"
    :ui="{ trailing: 'pe-1' }"
  >
    <template #trailing>
      <UButton
        type="button"
        color="neutral"
        variant="ghost"
        size="xs"
        :icon="visible ? 'i-lucide-eye-off' : 'i-lucide-eye'"
        :aria-label="visible ? 'Hide password' : 'Show password'"
        :aria-pressed="visible"
        :disabled="disabled"
        @click="visible = !visible"
      />
    </template>
  </UInput>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';

withDefaults(defineProps<{ disabled?: boolean }>(), { disabled: false });
const value = defineModel<string>({ default: '' });
const visible = ref(false);

watch(value, (value) => {
  if (!value) visible.value = false;
});
</script>
