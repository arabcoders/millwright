<template>
  <UModal
    :open="open"
    title="Starter flow presets"
    class="sm:max-w-5xl"
    @update:open="$emit('update:open', $event)"
  >
    <template #body>
      <div class="space-y-4">
        <p class="text-sm text-toned">
          Presets open in the editor. Save the flow to keep your changes.
        </p>
        <div class="grid gap-3 md:grid-cols-2">
          <article
            v-for="preset in presets"
            :key="preset.id"
            class="rounded-lg border border-default p-4"
          >
            <h2 class="truncate font-semibold text-highlighted">
              <button
                type="button"
                class="text-start hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                @click="$emit('select', preset)"
              >
                {{ preset.name }}
              </button>
            </h2>
            <p class="mt-2 text-sm text-toned">{{ preset.description }}</p>
            <p class="mt-3 text-sm"><span class="font-medium">Route:</span> {{ preset.route }}</p>
            <div class="mt-3 flex flex-wrap gap-1.5">
              <UBadge
                v-for="concept in preset.concepts"
                :key="concept"
                color="neutral"
                variant="subtle"
                >{{ concept }}</UBadge
              >
              <UBadge color="neutral" variant="outline"
                >{{ preset.definition.nodes.length }} nodes</UBadge
              >
            </div>
          </article>
        </div>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import type { FlowPreset } from '~/types/api';

defineProps<{ open: boolean; presets: FlowPreset[] }>();
defineEmits<{ 'update:open': [open: boolean]; select: [preset: FlowPreset] }>();
</script>
