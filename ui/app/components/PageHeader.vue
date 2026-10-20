<template>
  <div class="mw-page-header">
    <div class="mw-page-heading">
      <span class="mw-page-icon">
        <UIcon :name="pageShell.icon" class="size-5" />
      </span>

      <div class="min-w-0 space-y-2">
        <div class="mw-page-kicker">
          <span>{{ pageShell.sectionLabel }}</span>
          <span>/</span>
          <span>{{ pageShell.pageLabel }}</span>
        </div>

        <p class="max-w-3xl text-sm text-toned">{{ pageShell.description }}</p>
      </div>
    </div>

    <div class="flex w-full flex-wrap items-center gap-2 xl:w-auto xl:justify-end">
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
