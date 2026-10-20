<template>
  <nav
    :aria-label="`${label} pagination (${position})`"
    class="flex flex-wrap items-center justify-between gap-3"
  >
    <div class="flex min-w-0 flex-wrap items-center gap-2">
      <slot />
      <span class="text-xs text-toned tabular-nums">
        {{
          shown
            ? `${(offset + 1).toLocaleString('en')}–${(offset + shown).toLocaleString('en')} of ${total.toLocaleString('en')}`
            : `0 ${label.toLowerCase()} shown`
        }}
      </span>
    </div>
    <div class="flex flex-wrap items-center gap-2">
      <USelectMenu
        value-key="value"
        :model-value="limit"
        :items="pageSizeItems"
        size="sm"
        :aria-label="`${label} per page`"
        :disabled="disabled"
        @update:model-value="(value) => value !== undefined && emit('update:limit', value)"
      />
      <UFieldGroup size="sm">
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-chevron-left"
          aria-label="Previous page"
          :disabled="offset === 0 || disabled"
          @click="changePage(currentPage - 1)"
        />
        <USelectMenu
          :model-value="currentPage"
          :items="pageItems"
          value-key="value"
          :search-input="{ placeholder: 'Find a page...' }"
          virtualize
          color="neutral"
          variant="outline"
          size="sm"
          class="min-w-44"
          :aria-label="`Jump to ${label.toLowerCase()} page`"
          :disabled="disabled"
          @update:model-value="changePage"
        >
          Page {{ currentPage.toLocaleString('en') }} of
          {{ pageCount.toLocaleString('en') }}
        </USelectMenu>
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-chevron-right"
          aria-label="Next page"
          :disabled="offset + limit >= total || disabled"
          @click="changePage(currentPage + 1)"
        />
      </UFieldGroup>
    </div>
    <slot name="footer" />
  </nav>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { LIST_PAGE_SIZES } from '~/utils/listPagination';

const props = defineProps<{
  label: string;
  position: 'top' | 'bottom';
  limit: number;
  offset: number;
  total: number;
  shown: number;
  disabled?: boolean;
}>();
const emit = defineEmits<{
  'update:limit': [value: number];
  'update:offset': [value: number];
}>();
const pageSizeItems: Array<{ label: string; value: number }> = LIST_PAGE_SIZES.map((value) => ({
  label: `${value} per page`,
  value,
}));
const currentPage = computed(() => Math.floor(props.offset / props.limit) + 1);
const pageCount = computed(() => Math.max(currentPage.value, Math.ceil(props.total / props.limit)));
const pageItems = computed(() =>
  Array.from({ length: pageCount.value }, (_, index) => ({
    label: `Page ${index + 1}`,
    value: index + 1,
  })),
);
const changePage = (value: number | undefined): void => {
  if (value === undefined || props.disabled) return;
  const selected = Math.min(pageCount.value, Math.max(1, value));
  emit('update:offset', (selected - 1) * props.limit);
};
</script>
