<template>
  <div class="border-t border-default pt-3">
    <button
      type="button"
      class="flex w-full items-center gap-1.5 text-xs font-medium text-toned hover:text-default"
      :aria-expanded="open"
      @click="open = !open"
    >
      <span class="min-w-0 text-start">{{ label }}</span>
      <span class="ms-auto flex shrink-0 items-center gap-1.5">
        <UBadge v-if="condition.mode === 'custom'" color="warning" variant="subtle" size="sm">
          custom
        </UBadge>
        <UBadge v-else-if="condition.rows.length" color="neutral" variant="subtle" size="sm">
          {{ condition.rows.length }}
        </UBadge>
        <UIcon
          :name="open ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
          class="size-3.5 shrink-0"
        />
      </span>
    </button>

    <div v-if="open" class="space-y-3 pt-3">
      <UFormField
        v-if="condition.mode === 'custom'"
        label="Custom condition"
        description="Nested conditions must be edited as raw JSON. Saving requires valid JSON."
      >
        <UTextarea v-model="condition.custom" :rows="4" class="w-full font-mono text-xs" />
      </UFormField>

      <template v-else>
        <div v-if="condition.rows.length" class="space-y-2">
          <div
            v-for="(row, rowIndex) in condition.rows"
            :key="row.uid"
            class="flex flex-wrap items-center gap-2"
          >
            <UInputMenu
              v-model="row.key"
              :items="facetOptions"
              placeholder="Facet key"
              size="sm"
              class="min-w-44 flex-1"
            />
            <USelectMenu
              v-model="row.op"
              :items="OPERATORS"
              value-key="value"
              size="sm"
              class="w-20"
              aria-label="Operator"
            />
            <UInput
              v-if="isNumericOperand(row, factType(row.key))"
              v-model.number="row.value"
              type="number"
              step="any"
              size="sm"
              class="min-w-44 flex-1"
              aria-label="Value"
            />
            <UInput
              v-else
              :model-value="String(row.value)"
              size="sm"
              class="min-w-44 flex-1"
              aria-label="Value"
              @update:model-value="(value) => (row.value = value)"
            />
            <UButton
              color="neutral"
              variant="outline"
              size="xs"
              icon="i-lucide-trash-2"
              aria-label="Remove condition"
              @click="condition.rows.splice(rowIndex, 1)"
            />
          </div>
        </div>
        <p v-else class="text-xs text-toned">{{ emptyHint }}</p>

        <div class="flex flex-wrap items-center gap-2">
          <UButton
            color="neutral"
            variant="outline"
            size="xs"
            icon="i-lucide-plus"
            @click="addConditionRow(condition)"
          >
            Add condition
          </UButton>

          <div class="ms-auto flex items-center gap-1.5 text-xs text-toned">
            <span>Match</span>
            <UButton
              size="xs"
              color="neutral"
              :variant="condition.mode === 'all' ? 'solid' : 'soft'"
              @click="condition.mode = 'all'"
            >
              all
            </UButton>
            <UButton
              size="xs"
              color="neutral"
              :variant="condition.mode === 'any' ? 'solid' : 'soft'"
              @click="condition.mode = 'any'"
            >
              any
            </UButton>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import type { FlowFact } from '~/types/api';
import {
  OPERATORS,
  isNumericOperand,
  addConditionRow,
  type DraftCondition,
} from '~/utils/condition';

const condition = defineModel<DraftCondition>({ required: true });

const props = withDefaults(
  defineProps<{
    label?: string;
    emptyHint?: string;
    facetOptions?: string[];
    facts?: FlowFact[];
  }>(),
  {
    label: 'Condition',
    emptyHint: 'No condition. Runs whenever the step is reached.',
    facetOptions: undefined,
    facts: () => [],
  },
);

const open = ref(condition.value.mode === 'custom' || condition.value.rows.length > 0);
const factType = (key: string): string | undefined =>
  props.facts?.find((fact) => fact.key === key)?.type;
</script>
