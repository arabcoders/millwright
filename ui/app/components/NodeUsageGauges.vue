<template>
  <div class="space-y-1.5">
    <div class="flex items-center gap-1.5">
      <p v-if="offline && usage" class="text-[11px] text-toned">Last known</p>
      <UTooltip text="Usage reflects the whole system">
        <span
          tabindex="0"
          role="img"
          aria-label="About system usage"
          class="ms-auto inline-flex shrink-0 cursor-help text-dimmed"
        >
          <UIcon name="i-lucide-circle-help" class="size-3.5" aria-hidden="true" />
        </span>
      </UTooltip>
    </div>
    <div
      class="grid min-w-0 grid-cols-2 gap-3"
      :class="gauges.length >= 4 ? 'sm:grid-cols-4' : gauges.length === 3 ? 'sm:grid-cols-3' : ''"
    >
      <UTooltip
        v-for="gauge in gauges"
        :key="gauge.key"
        :text="gauge.device"
        :disabled="!gauge.device"
      >
        <div class="flex min-w-0 flex-col items-center gap-1">
          <div
            role="progressbar"
            :aria-label="`${gauge.label} usage${gauge.device ? ` (${gauge.device})` : ''}`"
            :aria-valuemin="0"
            :aria-valuemax="100"
            :aria-valuenow="gauge.percent == null ? undefined : Math.round(gauge.percent)"
            :aria-valuetext="gauge.percent == null ? 'Unavailable' : undefined"
            :class="['relative size-16 shrink-0', gauge.device ? 'cursor-help' : '']"
          >
            <svg class="size-full -rotate-90" viewBox="0 0 36 36" aria-hidden="true">
              <circle
                cx="18"
                cy="18"
                r="15.5"
                fill="none"
                stroke="var(--ui-border-accented)"
                stroke-width="4"
                pathLength="100"
              />
              <circle
                cx="18"
                cy="18"
                r="15.5"
                fill="none"
                :class="gauge.percent == null ? 'stroke-muted' : gaugeColor(gauge)"
                stroke-width="4"
                stroke-linecap="round"
                pathLength="100"
                :stroke-dasharray="`${gaugeValue(gauge)} 100`"
                :stroke-dashoffset="0"
                class="transition-[stroke-dasharray] duration-300"
              />
            </svg>
            <span
              class="absolute inset-0 flex flex-col items-center justify-center gap-0.5 text-center text-highlighted"
            >
              <span class="max-w-full truncate text-[9px] font-medium leading-tight text-toned">
                {{ gauge.label }}
              </span>
              <span class="text-xs font-semibold leading-tight">{{ gaugeText(gauge) }}</span>
            </span>
          </div>
        </div>
      </UTooltip>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { NodeUsage } from '~/types/api';

const props = defineProps<{
  usage: NodeUsage | null;
  cpuName?: string;
  cpuCores?: number;
  gpuNames?: string[];
  offline?: boolean;
}>();

type Gauge = { key: string; label: string; percent: number | null; device?: string };
const gauges = computed<Gauge[]>(() => {
  const usage = props.usage;
  const cpuDevice = [props.cpuName, props.cpuCores ? `${props.cpuCores} cores` : '']
    .filter(Boolean)
    .join(' · ');
  const rows: Gauge[] = [
    { key: 'cpu', label: 'CPU', percent: usage?.cpu_percent ?? null, device: cpuDevice },
    {
      key: 'memory',
      label: 'MEM',
      percent: usage?.memory_percent ?? null,
    },
  ];
  const gpus = usage?.gpus?.length
    ? usage.gpus
    : (props.gpuNames ?? []).map((name) => ({ name, percent: null }));
  gpus.forEach((gpu, index) =>
    rows.push({
      key: `gpu-${index}`,
      label: `GPU ${index + 1}`,
      percent: gpu.percent,
      device: gpu.name,
    }),
  );
  return rows;
});
const gaugeValue = (gauge: Gauge): number =>
  gauge.percent == null ? 0 : Math.min(100, Math.max(0, gauge.percent));
const gaugeColor = (gauge: Gauge): string => {
  const value = gaugeValue(gauge);
  return value >= 90 ? 'stroke-error' : value >= 70 ? 'stroke-warning' : 'stroke-success';
};
const gaugeText = (gauge: Gauge): string =>
  gauge.percent == null ? 'N/A' : `${Math.round(gauge.percent)}%`;
</script>
