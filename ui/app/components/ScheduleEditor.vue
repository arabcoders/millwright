<template>
  <div class="space-y-3">
    <URadioGroup
      :model-value="mode"
      :items="modeItems"
      aria-label="Schedule type"
      @update:model-value="setMode"
    />

    <div v-if="mode === 'grid'" class="space-y-1 overflow-x-auto pb-1">
      <p class="text-xs text-toned">
        Click or drag to set when this library accepts work, Monday through Sunday.
      </p>

      <div class="flex w-max items-center gap-1 text-center text-[10px] text-toned tabular-nums">
        <span class="w-10 shrink-0" aria-hidden="true" />
        <span v-for="hour in 24" :key="hour" class="w-5 shrink-0">
          {{ String(hour - 1).padStart(2, '0') }}
        </span>
      </div>

      <div v-for="(row, day) in cells" :key="day" class="flex w-max items-center gap-1">
        <button
          type="button"
          class="w-10 shrink-0 text-start text-xs font-medium text-toned hover:text-default"
          :aria-label="`Toggle all hours for ${DAYS[day]}`"
          @click="toggleDay(day)"
        >
          {{ DAYS[day] }}
        </button>

        <button
          v-for="(on, hour) in row"
          :key="hour"
          type="button"
          class="size-5 shrink-0 rounded-sm transition-colors"
          :class="on ? 'bg-primary' : 'bg-elevated ring-1 ring-inset ring-default'"
          :aria-label="`${DAYS[day]} ${hour}:00 ${on ? 'on' : 'off'}`"
          :title="`${DAYS[day]} ${String(hour).padStart(2, '0')}:00`"
          @pointerdown.prevent="startPaint(day, hour)"
          @pointerenter="paint(day, hour)"
          @keydown.enter.prevent="toggleCell(day, hour)"
          @keydown.space.prevent="toggleCell(day, hour)"
        />
      </div>
    </div>

    <p v-if="error" class="text-sm text-error">{{ error }}</p>
  </div>
</template>

<script setup lang="ts">
import type { ScheduleSpec } from '~/types/api';

type Mode = 'always' | 'grid';

const props = defineProps<{
  modelValue: ScheduleSpec | null;
  error?: string | null;
}>();

const emit = defineEmits<{
  'update:modelValue': [value: ScheduleSpec | null];
}>();

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

const modeItems: Array<{ label: string; value: Mode }> = [
  { label: 'Always active', value: 'always' },
  { label: 'Weekly schedule', value: 'grid' },
];

const mode = ref<Mode>(props.modelValue?.type ?? 'always');

const cells = ref<boolean[][]>(
  Array.from({ length: 7 }, (_, day) =>
    Array.from(
      { length: 24 },
      (_, hour) =>
        props.modelValue?.type === 'grid' && props.modelValue.cells[day]?.[hour] === true,
    ),
  ),
);

const emitValue = (): void => {
  if (mode.value === 'grid') {
    emit('update:modelValue', { type: 'grid', cells: cells.value });
  } else {
    emit('update:modelValue', null);
  }
};

const setMode = (value: unknown): void => {
  mode.value = value === 'grid' ? value : 'always';
  emitValue();
};

const setCell = (day: number, hour: number, value: boolean): void => {
  const row = cells.value[day];
  if (!row || row[hour] === value) return;
  row[hour] = value;
  emitValue();
};

const painting = ref(false);
const paintValue = ref(false);

const startPaint = (day: number, hour: number): void => {
  painting.value = true;
  paintValue.value = !(cells.value[day]?.[hour] ?? false);
  setCell(day, hour, paintValue.value);
};

const paint = (day: number, hour: number): void => {
  if (painting.value) setCell(day, hour, paintValue.value);
};

const stopPaint = (): void => {
  painting.value = false;
};

onMounted(() => window.addEventListener('pointerup', stopPaint));
onBeforeUnmount(() => window.removeEventListener('pointerup', stopPaint));

const toggleCell = (day: number, hour: number): void => {
  setCell(day, hour, !(cells.value[day]?.[hour] ?? false));
};

const toggleDay = (day: number): void => {
  const row = cells.value[day];
  if (!row) return;
  const turnOn = row.some((cell) => !cell);
  cells.value[day] = row.map(() => turnOn);
  emitValue();
};
</script>
