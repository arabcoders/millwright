<template>
  <div class="min-h-dvh">
    <AppRoot>
      <main
        class="shell-surface flex min-h-dvh w-full items-center justify-center px-4 py-8 sm:px-6"
      >
        <div class="w-full max-w-md space-y-6">
          <header class="space-y-3 text-center">
            <div
              class="mx-auto inline-flex size-14 items-center justify-center rounded-md border border-default bg-elevated/80 shadow-sm"
            >
              <img
                :src="uri('/images/favicon-192.png')"
                alt="Millwright logo"
                class="size-10 rounded-md object-contain"
              />
            </div>
            <p class="text-2xl font-semibold text-highlighted">Millwright</p>
          </header>
          <slot />
        </div>
      </main>
    </AppRoot>
  </div>
</template>

<script setup lang="ts">
import AppRoot from '~/components/AppRoot.vue';
import { uri } from '~/utils';
</script>
