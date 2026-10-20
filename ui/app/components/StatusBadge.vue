<script setup lang="ts">
import { computed } from 'vue';
import { statusInfo } from '~/utils/status';

const props = withDefaults(defineProps<{ status: string; group?: string }>(), { group: 'job' });
const info = computed(() => statusInfo(props.status, props.group));
</script>

<template>
  <UBadge :color="info.color" variant="soft" size="sm" class="gap-1">
    <UIcon :name="info.icon" class="size-3.5 shrink-0" />
    <span>{{ info.label }}</span>
  </UBadge>
</template>
