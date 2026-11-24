<template>
  <div class="flex min-w-0 flex-wrap items-center gap-2">
    <UInput
      v-model.lazy="query"
      type="search"
      :disabled="disabled"
      icon="i-lucide-filter"
      :placeholder="searchPlaceholder"
      aria-label="Search"
      size="sm"
      class="w-full sm:min-w-48 sm:flex-1 lg:max-w-md"
    />
    <slot />
    <UButton
      v-if="filtersActive"
      color="neutral"
      variant="outline"
      size="sm"
      icon="i-lucide-filter-x"
      :disabled="disabled"
      @click="emit('clearFilters')"
    >
      Clear filters
    </UButton>
    <UDropdownMenu :items="sortItems" :content="{ align: 'end' }" :modal="false">
      <UButton
        color="neutral"
        variant="outline"
        size="sm"
        :icon="directionIcon"
        trailing-icon="i-lucide-chevron-down"
        :aria-label="sortLabel"
        :disabled="disabled"
      >
        Sort: {{ sortName }}
      </UButton>
    </UDropdownMenu>
    <div v-if="$slots.trailing" class="ms-auto flex flex-wrap items-center gap-2">
      <slot name="trailing" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { DropdownMenuItem } from '@nuxt/ui';

const query = defineModel<string>('query', { required: true });
const props = defineProps<{
  searchPlaceholder: string;
  sortBy: string;
  sortDir: 'asc' | 'desc';
  sortOptions: Array<{ label: string; value: string }>;
  sortLabel: string;
  filtersActive: boolean;
  disabled?: boolean;
}>();
const emit = defineEmits<{
  'update:sortBy': [value: string];
  'update:sortDir': [value: 'asc' | 'desc'];
  clearFilters: [];
}>();
const directionIcon = computed(() =>
  props.sortDir === 'asc' ? 'i-lucide-arrow-up' : 'i-lucide-arrow-down',
);
const sortName = computed(
  () => props.sortOptions.find((option) => option.value === props.sortBy)?.label,
);
const sortItems = computed<DropdownMenuItem[]>(() =>
  props.sortOptions.map((option) => ({
    label:
      option.value === props.sortBy
        ? `${option.label} (${props.sortDir === 'asc' ? 'ASC' : 'DESC'})`
        : option.label,
    icon: option.value === props.sortBy ? directionIcon.value : undefined,
    color: option.value === props.sortBy ? 'primary' : 'neutral',
    disabled: props.disabled,
    onSelect: () => {
      if (option.value === props.sortBy)
        emit('update:sortDir', props.sortDir === 'asc' ? 'desc' : 'asc');
      else emit('update:sortBy', option.value);
    },
  })),
);
</script>
