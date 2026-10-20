<template>
  <div class="min-h-screen">
    <AppRoot>
      <main
        class="relative flex min-h-screen w-full items-center justify-center overflow-hidden bg-default px-4 py-8 sm:px-6"
      >
        <div
          class="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgb(59_130_246/0.16),transparent_42%),radial-gradient(circle_at_bottom_right,rgb(14_165_233/0.12),transparent_40%)] dark:bg-[radial-gradient(circle_at_top_left,rgb(59_130_246/0.22),transparent_42%),radial-gradient(circle_at_bottom_right,rgb(14_165_233/0.14),transparent_40%)]"
        />

        <div class="relative w-full max-w-md space-y-5">
          <div class="flex justify-end gap-1">
            <ThemeButton :square="true" :show-label="false" />
          </div>

          <section class="mw-card space-y-6 p-5 shadow-2xl sm:p-8">
            <header class="space-y-4 text-center">
              <div
                class="mx-auto flex size-16 items-center justify-center rounded-2xl border border-default bg-default shadow-sm"
              >
                <img
                  :src="uri('/images/favicon-192.png')"
                  alt="Millwright logo"
                  class="size-10 rounded-xl object-contain"
                />
              </div>
              <div>
                <p class="text-sm font-semibold tracking-[0.18em] text-primary uppercase">
                  Millwright
                </p>
              </div>
            </header>

            <slot />
          </section>
        </div>
      </main>
    </AppRoot>
  </div>
</template>

<script setup lang="ts">
import ThemeButton from '~/components/ThemeButton.vue';
import AppRoot from '~/components/AppRoot.vue';
import { uri } from '~/utils';
</script>
