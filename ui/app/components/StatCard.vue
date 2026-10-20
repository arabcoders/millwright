<template>
  <div class="mw-card p-3">
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0 space-y-1">
        <p class="text-[11px] font-semibold uppercase tracking-[0.18em] text-toned">
          <slot name="label" :label="label"
            ><bdi>{{ label }}</bdi></slot
          >
        </p>
        <UTooltip v-if="tooltip" :text="tooltip" :content="{ side: 'left' }">
          <div class="flex items-baseline gap-1.5">
            <NuxtLink v-if="to" :to="to" :class="[valueClass, 'hover:underline']">
              <slot name="value" :value="value"
                ><bdi>{{ value }}</bdi></slot
              >
            </NuxtLink>
            <a
              v-else-if="href"
              :href="href"
              target="_blank"
              rel="noreferrer"
              :class="[valueClass, 'hover:underline']"
            >
              <slot name="value" :value="value"
                ><bdi>{{ value }}</bdi></slot
              >
            </a>
            <span v-else :class="valueClass"
              ><slot name="value" :value="value"
                ><bdi>{{ value }}</bdi></slot
              ></span
            >
            <span
              v-if="hint"
              :class="[
                'min-w-0 text-xs leading-5 text-toned',
                hintWrap ? 'line-clamp-2' : 'truncate',
              ]"
              ><bdi>{{ hint }}</bdi></span
            >
          </div>
        </UTooltip>
        <div v-else class="flex items-baseline gap-1.5">
          <NuxtLink v-if="to" :to="to" :class="[valueClass, 'hover:underline']">
            <slot name="value" :value="value"
              ><bdi>{{ value }}</bdi></slot
            >
          </NuxtLink>
          <a
            v-else-if="href"
            :href="href"
            target="_blank"
            rel="noreferrer"
            :class="[valueClass, 'hover:underline']"
          >
            <slot name="value" :value="value"
              ><bdi>{{ value }}</bdi></slot
            >
          </a>
          <span v-else :class="valueClass"
            ><slot name="value" :value="value"
              ><bdi>{{ value }}</bdi></slot
            ></span
          >
          <span
            v-if="hint"
            :class="[
              'min-w-0 text-xs leading-5 text-toned',
              hintWrap ? 'line-clamp-2' : 'truncate',
            ]"
            ><bdi>{{ hint }}</bdi></span
          >
        </div>
      </div>
      <div v-if="$slots.actions" class="flex shrink-0 items-center gap-1">
        <slot name="actions" />
      </div>
      <span
        v-else
        :class="[
          'flex size-8 shrink-0 items-center justify-center rounded-md ring-1 ring-inset',
          iconTileClass,
        ]"
      >
        <UIcon :name="icon" :class="['size-4', iconTextClass]" />
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
type Color = 'primary' | 'success' | 'error' | 'warning' | 'info' | 'neutral';

const props = withDefaults(
  defineProps<{
    label: string;
    value: string | number;
    icon: string;
    hint?: string;
    hintWrap?: boolean;
    tooltip?: string;
    href?: string;
    to?: string;
    color?: Color;
    valueWrap?: boolean;
  }>(),
  { color: 'primary', hint: '', hintWrap: false, tooltip: '', href: '', to: '', valueWrap: false },
);

const TILE_BG: Record<Color, string> = {
  primary: 'bg-elevated',
  success: 'bg-success/10',
  error: 'bg-error/10',
  warning: 'bg-warning/10',
  info: 'bg-info/10',
  neutral: 'bg-elevated',
};

const TILE_RING: Record<Color, string> = {
  primary: 'ring-default',
  success: 'ring-success/30',
  error: 'ring-error/30',
  warning: 'ring-warning/30',
  info: 'ring-info/30',
  neutral: 'ring-default',
};

const TILE_TEXT: Record<Color, string> = {
  primary: 'text-primary',
  success: 'text-success',
  error: 'text-error',
  warning: 'text-warning',
  info: 'text-info',
  neutral: 'text-toned',
};

const iconTileClass = computed(() => `${TILE_BG[props.color]} ${TILE_RING[props.color]}`);
const iconTextClass = computed(() => TILE_TEXT[props.color]);
const valueClass = computed(() => [
  'block text-sm font-semibold text-highlighted',
  props.hintWrap ? 'shrink-0' : props.valueWrap ? 'wrap-break-word' : 'truncate',
]);
</script>
