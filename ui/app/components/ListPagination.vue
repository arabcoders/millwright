<template>
  <nav
    :aria-label="`${label} pagination (${position})`"
    class="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-3"
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
    <div class="flex w-full min-w-0 flex-wrap items-center gap-2 sm:w-auto">
      <USelectMenu
        value-key="value"
        :model-value="limit"
        :items="pageSizeItems"
        size="sm"
        class="w-36 shrink-0"
        :aria-label="`${label} per page`"
        :disabled="disabled"
        @update:model-value="(value) => value !== undefined && emit('update:limit', value)"
      />
      <PageNavigation
        class="basis-64 grow sm:basis-auto sm:grow-0"
        :page="currentPage"
        :page-count="pageCount"
        :jump-label="`Jump to ${label.toLowerCase()} page`"
        :disabled="disabled"
        @change="emit('update:offset', ($event - 1) * limit)"
      />
    </div>
    <slot name="footer" />
  </nav>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import PageNavigation from '~/components/PageNavigation.vue';
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
const pageCount = computed(() => Math.max(1, Math.ceil(props.total / props.limit)));
</script>
