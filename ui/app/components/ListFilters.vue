<template>
  <div class="mw-card flex flex-wrap items-end gap-3 p-3 sm:p-4">
    <UInput
      v-model.lazy="query"
      :disabled="disabled"
      icon="i-lucide-search"
      :placeholder="searchPlaceholder"
      size="sm"
      class="w-full sm:min-w-56 sm:flex-1 lg:max-w-md"
    />
    <slot />
    <USelectMenu
      value-key="value"
      :model-value="sortBy"
      :disabled="disabled"
      :items="sortOptions"
      size="sm"
      class="w-full sm:w-auto sm:min-w-36"
      :aria-label="sortLabel"
      @update:model-value="(value) => value !== undefined && emit('update:sortBy', value)"
    />
    <UTooltip
      :text="
        sortDir === 'asc' ? 'Ascending. Switch to descending' : 'Descending. Switch to ascending'
      "
    >
      <UButton
        color="neutral"
        variant="outline"
        size="sm"
        :icon="sortDir === 'asc' ? 'i-lucide-arrow-up' : 'i-lucide-arrow-down'"
        :aria-label="sortDir === 'asc' ? 'Sort descending' : 'Sort ascending'"
        :disabled="disabled"
        @click="emit('update:sortDir', sortDir === 'asc' ? 'desc' : 'asc')"
      />
    </UTooltip>
    <slot name="trailing" />
  </div>
</template>

<script setup lang="ts">
const query = defineModel<string>('query', { required: true });
defineProps<{
  searchPlaceholder: string;
  sortBy: string;
  sortDir: 'asc' | 'desc';
  sortOptions: Array<{ label: string; value: string }>;
  sortLabel: string;
  disabled?: boolean;
}>();
const emit = defineEmits<{
  'update:sortBy': [value: string];
  'update:sortDir': [value: 'asc' | 'desc'];
}>();
</script>
