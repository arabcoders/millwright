<template>
  <div class="mw-page-header">
    <div class="mw-page-heading">
      <span class="mw-page-icon" :class="{ 'hidden sm:inline-flex': !$slots.icon }">
        <slot name="icon" :icon="pageShell.icon">
          <UIcon :name="pageShell.icon" class="size-5" aria-hidden="true" />
        </slot>
      </span>

      <div class="min-w-0 space-y-2">
        <p class="mw-page-kicker">{{ pageShell.sectionLabel }}</p>
        <h1 class="text-2xl font-semibold tracking-tight text-highlighted sm:text-3xl">
          {{ pageShell.pageLabel }}
        </h1>
        <p v-if="pageShell.description" class="max-w-3xl text-sm text-toned">
          {{ pageShell.description }}
        </p>
      </div>
    </div>

    <div
      v-if="$slots.actions"
      class="flex w-full flex-wrap items-center gap-2 xl:w-auto xl:justify-end"
    >
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { requirePageShell } from '~/utils/topLevelNavigation';

const props = defineProps<{ page: string }>();
const pageShell = computed(() => requirePageShell(props.page));
</script>
