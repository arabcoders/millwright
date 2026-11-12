<script setup lang="ts">
import type { StatusColor, StatusInfo } from '~/utils/status';

defineProps<{ info: StatusInfo }>();

const colors: Record<StatusColor, string> = {
  neutral: 'bg-muted text-muted',
  info: 'bg-info/10 text-info',
  success: 'bg-success/10 text-success',
  error: 'bg-error/10 text-error',
  warning: 'bg-warning/10 text-warning',
};
</script>

<template>
  <UTooltip :text="info.label">
    <div
      tabindex="0"
      class="shrink-0 rounded-lg focus-visible:outline-2 focus-visible:outline-primary"
    >
      <slot>
        <span
          role="img"
          :aria-label="info.label"
          class="flex size-12 items-center justify-center rounded-lg"
          :class="colors[info.color]"
        >
          <UIcon :name="info.icon" class="size-5" aria-hidden="true" />
        </span>
      </slot>
    </div>
  </UTooltip>
</template>
