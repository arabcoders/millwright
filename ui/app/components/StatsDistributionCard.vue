<template>
  <section :class="framed ? 'mw-card space-y-3 p-4' : 'space-y-3'" :aria-label="title">
    <div class="flex items-center gap-2">
      <UIcon :name="icon" class="size-4 text-toned" />
      <h3 class="font-semibold text-highlighted">{{ title }}</h3>
    </div>
    <p v-if="!rows.length" class="text-sm text-toned">
      <slot name="empty">No data available.</slot>
    </p>
    <div v-else class="space-y-2">
      <div v-for="row in rows" :key="row.label" class="space-y-1">
        <div class="flex justify-between text-xs">
          <span
            ><slot name="label" :row="row">{{ row.label }}</slot></span
          ><span class="text-toned">{{ row.value.toLocaleString('en') }} ({{ row.percent }}%)</span>
        </div>
        <div class="h-1.5 overflow-hidden rounded-full bg-elevated" role="presentation">
          <div
            class="h-full rounded-full"
            :class="row.barClass"
            :style="{ width: `${row.percent}%` }"
          />
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { statusInfo, statusOptions, type StatusColor } from '~/utils/status';

const props = withDefaults(
  defineProps<{
    title: string;
    icon: string;
    values: Record<string, number>;
    statusGroup?: string;
    framed?: boolean;
    showZero?: boolean;
  }>(),
  { statusGroup: undefined, framed: true, showZero: false },
);
const humanize = (label: string) =>
  label === 'Other'
    ? label
    : label.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
const palette = ['bg-primary', 'bg-info', 'bg-success', 'bg-warning', 'bg-error'];
const statusBars: Record<StatusColor, string> = {
  success: 'bg-success',
  error: 'bg-error',
  neutral: 'bg-muted',
  warning: 'bg-warning',
  info: 'bg-info',
};
const barClass = (key: string, index: number) => {
  const normalized = key.toLowerCase();
  if (key === 'Other') return 'bg-muted';
  if (props.statusGroup) return statusBars[statusInfo(key, props.statusGroup).color];
  if (['successful', 'success', 'succeeded', 'healthy', 'transcoded'].includes(normalized))
    return 'bg-success';
  if (['failed', 'error', 'corrupt'].includes(normalized)) return 'bg-error';
  if (['cancelled', 'pending', 'queued', 'staging', 'hold'].includes(normalized))
    return 'bg-warning';
  if (normalized === 'skipped') return 'bg-info';
  return palette[index % palette.length];
};
const rows = computed(() => {
  const order =
    props.statusGroup === 'job'
      ? statusOptions('job')
          .filter((status) => status.value && !status.active)
          .map((status) => status.value)
      : null;
  const values =
    props.showZero && order
      ? { ...Object.fromEntries(order.map((status) => [status, 0])), ...props.values }
      : props.values;
  const sorted = Object.entries(values)
    .filter(([, value]) => value > 0 || props.showZero)
    .sort((a, b) =>
      order ? order.indexOf(a[0]) - order.indexOf(b[0]) : b[1] - a[1] || a[0].localeCompare(b[0]),
    );
  const total = sorted.reduce((sum, [, value]) => sum + value, 0);
  const visible = sorted.slice(0, 6);
  if (sorted.length > 6)
    visible.push(['Other', sorted.slice(6).reduce((sum, [, value]) => sum + value, 0)]);
  return visible.map(([key, value], index) => ({
    key,
    label:
      props.statusGroup && key !== 'Other'
        ? statusInfo(key, props.statusGroup).label
        : humanize(key),
    barClass: barClass(key, index),
    value,
    percent: total ? Math.round((value / total) * 100) : 0,
  }));
});
</script>
