<template>
  <div
    class="relative size-12 shrink-0"
    role="progressbar"
    :aria-label="label"
    aria-valuemin="0"
    aria-valuemax="100"
    :aria-valuenow="percent === null ? undefined : Math.round(percent)"
    :aria-valuetext="ariaValueText"
  >
    <svg class="size-full -rotate-90" viewBox="0 0 36 36" aria-hidden="true">
      <circle
        cx="18"
        cy="18"
        r="15.5"
        fill="none"
        stroke="var(--ui-border-accented)"
        stroke-width="3.5"
        pathLength="100"
      />
      <circle
        cx="18"
        cy="18"
        r="15.5"
        fill="none"
        :class="[
          props.status === 'hold' || remaining !== null
            ? cooldownColor(percent)
            : color === 'success'
              ? 'stroke-success'
              : 'stroke-primary',
          percent === null ? 'origin-center stroke-(--ui-text-muted)' : '',
          percent === null && props.status !== 'hold' ? 'animate-spin' : '',
        ]"
        stroke-width="3.5"
        stroke-linecap="round"
        pathLength="100"
        :stroke-dasharray="percent === null ? '28 72' : `${percent} 100`"
        stroke-dashoffset="0"
      />
    </svg>
    <span
      class="absolute inset-0 flex items-center justify-center text-center text-xs font-semibold text-highlighted tabular-nums"
    >
      {{
        remaining !== null
          ? formatCountdown(remaining)
          : props.status === 'hold'
            ? 'Hold'
            : percent === null
              ? statusLabel
              : `${Math.round(percent)}%`
      }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue';
import { useNow } from '@vueuse/core';
import {
  cooldownColor,
  cooldownPercent,
  cooldownRemaining,
  formatCountdown,
} from '~/utils/progress';

const props = withDefaults(
  defineProps<{
    status: string;
    percent: number | null;
    cooldownStartedAt?: string | null;
    cooldownUntil?: string | null;
    label?: string;
    color?: 'primary' | 'success';
  }>(),
  {
    cooldownStartedAt: null,
    cooldownUntil: null,
    label: 'Job progress',
    color: 'primary',
  },
);
const { now, pause, resume } = useNow({ interval: 1000, controls: true, immediate: false });
const remaining = computed(() =>
  cooldownRemaining(props.status, props.cooldownStartedAt, props.cooldownUntil, now.value),
);
const cooldown = computed(() =>
  props.status === 'hold' || remaining.value !== null
    ? cooldownPercent(props.cooldownStartedAt, props.cooldownUntil, now.value)
    : null,
);
const cooldownActive = computed(() => remaining.value !== null && remaining.value > 0);
watch(cooldownActive, (active) => (active ? resume() : pause()), { immediate: true });
const percent = computed(() =>
  props.status === 'hold' || remaining.value !== null
    ? cooldown.value
    : props.status === 'pending'
      ? null
      : props.percent,
);
const ariaValueText = computed(() =>
  remaining.value !== null
    ? `${remaining.value} seconds cooldown remaining${cooldown.value === null ? '' : `, ${Math.round(cooldown.value)}% cooldown elapsed`}`
    : props.status === 'hold'
      ? 'Hold progress unavailable'
      : percent.value === null
        ? `${statusLabel.value} progress unavailable`
        : `${Math.round(percent.value)}%`,
);
const statusLabel = computed(() => {
  if (props.status === 'pending') return 'Wait';
  if (props.status === 'claimed') return 'Claim';
  if (props.status === 'running') return 'Run';
  if (props.status === 'verifying') return 'Verify';
  if (props.status === 'finalizing') return 'Finish';
  return props.status;
});
</script>
