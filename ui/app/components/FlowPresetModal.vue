<template>
  <UModal
    :open="open"
    title="Starter flow presets"
    class="sm:max-w-5xl"
    @update:open="$emit('update:open', $event)"
  >
    <template #body>
      <div class="space-y-4" :aria-busy="loading">
        <p class="text-sm text-toned">
          Presets open in the editor. Save the flow to keep your changes.
        </p>
        <UAlert
          v-if="error"
          color="error"
          variant="soft"
          icon="i-lucide-triangle-alert"
          title="Could not load starter presets"
          :description="error"
        >
          <template #actions>
            <UButton
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-refresh-cw"
              :loading="loading"
              :disabled="loading"
              @click="$emit('retry')"
              >Retry</UButton
            >
          </template>
        </UAlert>
        <div
          v-if="loading && !presets.length"
          class="space-y-3"
          role="status"
          aria-label="Loading starter presets"
        >
          <USkeleton v-for="index in 3" :key="index" class="h-32 rounded-lg" />
        </div>
        <div v-else-if="presets.length" class="mw-card divide-y divide-default">
          <article v-for="preset in presets" :key="preset.id" class="min-w-0 p-4 sm:p-5">
            <h2 class="font-semibold text-highlighted">
              <button
                type="button"
                class="wrap-anywhere text-start hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                @click="$emit('select', preset)"
              >
                {{ preset.name }}
              </button>
            </h2>
            <p class="mt-2 wrap-anywhere text-sm text-toned">{{ preset.description }}</p>
            <p class="mt-3 wrap-anywhere text-sm">
              <span class="text-muted">Route:</span> {{ preset.route }}
            </p>
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
        <UEmpty
          v-else-if="!error"
          size="sm"
          icon="i-lucide-book-open"
          title="No starter presets available."
        />
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import type { FlowPreset } from '~/types/api';

defineProps<{ open: boolean; presets: FlowPreset[]; loading: boolean; error: string }>();
defineEmits<{ 'update:open': [open: boolean]; select: [preset: FlowPreset]; retry: [] }>();
</script>
