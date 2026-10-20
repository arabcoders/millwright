<template>
  <div ref="target" :style="rendered ? undefined : { minHeight: `${height || minHeight}px` }">
    <slot v-if="rendered" />
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue';
import { useIntersectionObserver } from '@vueuse/core';

const props = withDefaults(defineProps<{ minHeight: number; keepMounted?: boolean }>(), {
  keepMounted: false,
});
const target = ref<HTMLElement | null>(null);
const rendered = ref(false);
const height = ref(0);
let timer: ReturnType<typeof setTimeout> | undefined;

const { stop } = useIntersectionObserver(
  target,
  ([entry]) => {
    clearTimeout(timer);
    if (entry?.isIntersecting) {
      rendered.value = true;
      if (props.keepMounted) stop();
    } else if (rendered.value && !props.keepMounted) {
      timer = setTimeout(() => {
        // Don't unmount the card that owns keyboard focus.
        if (target.value?.contains(document.activeElement)) return;
        height.value = target.value?.getBoundingClientRect().height ?? props.minHeight;
        rendered.value = false;
      }, 6000);
    }
  },
  { rootMargin: '600px' },
);

onBeforeUnmount(() => clearTimeout(timer));
</script>
