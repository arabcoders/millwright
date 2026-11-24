<template>
  <UFieldGroup size="sm" class="min-w-0">
    <UButton
      color="neutral"
      variant="outline"
      icon="i-lucide-chevron-left"
      aria-label="Previous page"
      :disabled="page <= 1 || disabled"
      @click="changePage(page - 1)"
    />
    <UPopover :open="open" :content="popoverContent" @update:open="updateOpen">
      <UButton
        ref="trigger"
        color="neutral"
        variant="outline"
        trailing-icon="i-lucide-chevron-down"
        class="min-w-0 flex-1 justify-center whitespace-nowrap sm:min-w-40"
        :label="pageLabel"
        :aria-label="`${pageLabel}. ${jumpLabel}`"
        :disabled="pageCount <= 1 || disabled"
      />

      <template #content>
        <div class="w-sm max-w-[calc(100vw-1rem)] space-y-3 p-3">
          <div class="flex flex-wrap justify-center gap-1" role="group" aria-label="Nearby pages">
            <UButton
              v-for="number in pages"
              :key="number"
              :color="number === page ? 'primary' : 'neutral'"
              :variant="number === page ? 'soft' : 'ghost'"
              size="sm"
              class="min-w-8 justify-center tabular-nums"
              :label="number.toLocaleString('en')"
              :aria-label="`Page ${number.toLocaleString('en')}`"
              :aria-current="number === page ? 'page' : undefined"
              :disabled="disabled"
              @click="changePage(number)"
            />
          </div>

          <div class="flex flex-wrap items-center justify-between gap-2">
            <UButton
              color="neutral"
              variant="ghost"
              size="sm"
              icon="i-lucide-chevrons-left"
              label="First"
              aria-label="First page"
              :disabled="page <= 1 || disabled"
              @click="changePage(1)"
            />
            <form class="flex items-center gap-1" @submit.prevent="changePage(Number(pageNumber))">
              <UInput
                v-model.number="pageNumber"
                type="number"
                inputmode="numeric"
                required
                :min="1"
                :max="pageCount"
                :step="1"
                size="sm"
                class="w-20"
                aria-label="Page number"
                :disabled="disabled"
              />
              <UButton
                type="submit"
                color="neutral"
                variant="outline"
                size="sm"
                :disabled="disabled"
              >
                Go
              </UButton>
            </form>
            <UButton
              color="neutral"
              variant="ghost"
              size="sm"
              trailing-icon="i-lucide-chevrons-right"
              label="Last"
              aria-label="Last page"
              :disabled="page >= pageCount || disabled"
              @click="changePage(pageCount)"
            />
          </div>
        </div>
      </template>
    </UPopover>
    <UButton
      color="neutral"
      variant="outline"
      icon="i-lucide-chevron-right"
      aria-label="Next page"
      :disabled="page >= pageCount || disabled"
      @click="changePage(page + 1)"
    />
  </UFieldGroup>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { nearbyPages } from '~/utils/listPagination';

const props = defineProps<{
  page: number;
  pageCount: number;
  jumpLabel: string;
  disabled?: boolean;
}>();
const emit = defineEmits<{ change: [page: number] }>();
const open = ref(false);
const pageNumber = ref<number | string>('');
const trigger = ref<{ $el: HTMLButtonElement } | null>(null);
let jumped = false;

const pageLabel = computed(
  () => `Page ${props.page.toLocaleString('en')} of ${props.pageCount.toLocaleString('en')}`,
);
const pages = computed(() => nearbyPages(props.page, props.pageCount));
const popoverContent = computed(() => ({
  'aria-label': props.jumpLabel,
  onCloseAutoFocus: (event: Event) => {
    if (!jumped) return;
    // The bottom pager scrolls to the results after a jump.
    event.preventDefault();
    trigger.value?.$el.focus({ preventScroll: true });
  },
}));

const updateOpen = (value: boolean): void => {
  if (value) {
    pageNumber.value = props.page;
    jumped = false;
  }
  open.value = value;
};

const changePage = (value: number): void => {
  if (props.disabled || !Number.isInteger(value) || value < 1 || value > props.pageCount) return;
  jumped = open.value && value !== props.page;
  open.value = false;
  if (value !== props.page) emit('change', value);
};
</script>
