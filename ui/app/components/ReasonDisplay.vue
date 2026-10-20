<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { formatReason } from '~/utils/jobList';

const props = defineProps<{ reason: string }>();
const text = computed(() => formatReason(props.reason));
const expanded = ref(false);
watch(
  () => props.reason,
  () => {
    expanded.value = false;
  },
);
</script>

<template>
  <button
    type="button"
    :aria-label="expanded ? 'Collapse reason' : 'Expand reason'"
    :aria-expanded="expanded"
    class="nodrag nopan nowheel flex w-full min-w-0 items-start gap-1.5 text-left text-xs text-toned hover:text-default focus-visible:outline-2 focus-visible:outline-primary"
    @click.stop="expanded = !expanded"
    @keydown.stop
  >
    <span
      class="min-w-0 flex-1 whitespace-pre-wrap wrap-break-word"
      :class="{ 'line-clamp-2': !expanded }"
      >{{ text }}</span
    >
    <UIcon
      :name="expanded ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
      class="mt-0.5 size-3.5 shrink-0"
    />
  </button>
</template>
