<template>
  <USlideover
    v-model:open="open"
    title="Web UI settings"
    description="Changes are saved to this browser."
    side="right"
    :ui="{ content: 'w-full sm:max-w-lg' }"
    @after:leave="$emit('after:leave')"
  >
    <template #body>
      <div class="space-y-6">
        <section class="mw-card space-y-4 p-4">
          <h2 class="flex items-center gap-2 font-semibold text-highlighted">
            <UIcon name="i-lucide-palette" class="size-4 text-toned" />
            Appearance
          </h2>
          <UFormField label="Theme">
            <USelect v-model="color.preference" :items="themes" class="w-full" />
          </UFormField>
          <USwitch v-model="animations" label="Page animations" />
        </section>

        <section class="mw-card space-y-4 p-4">
          <h2 class="flex items-center gap-2 font-semibold text-highlighted">
            <UIcon name="i-lucide-image" class="size-4 text-toned" />
            Background
          </h2>
          <USwitch v-model="backgroundEnabled" label="Library artwork background" />
          <template v-if="backgroundEnabled">
            <UFormField
              label="Background visibility"
              :hint="`${Math.round(backgroundVisibility * 100)}%`"
            >
              <USlider v-model="backgroundVisibility" :min="0" :max="0.4" :step="0.01" />
            </UFormField>
            <UButton
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-refresh-cw"
              :loading="backgroundLoading"
              @click="loadBackground(true)"
              >Another background</UButton
            >
            <p v-if="backgroundEmpty" class="text-sm text-toned">
              {{
                backgroundImage
                  ? 'No replacement artwork found. Keeping the current background.'
                  : 'No artwork found. Using the default background.'
              }}
            </p>
            <UAlert
              v-if="backgroundError"
              color="error"
              variant="soft"
              icon="i-lucide-triangle-alert"
              title="Background request failed"
              :description="backgroundError"
            />
          </template>
        </section>
      </div>
    </template>
  </USlideover>
</template>

<script setup lang="ts">
import { useStorage } from '@vueuse/core';
import { useBackground } from '~/composables/useBackground';

const open = defineModel<boolean>('open', { default: false });
defineEmits<{ 'after:leave': [] }>();

const color = useColorMode();
const themes = [
  { label: 'System', value: 'system' },
  { label: 'Light', value: 'light' },
  { label: 'Dark', value: 'dark' },
];
const animations = useStorage('page_anims', true);
const {
  enabled: backgroundEnabled,
  visibility: backgroundVisibility,
  image: backgroundImage,
  loading: backgroundLoading,
  empty: backgroundEmpty,
  error: backgroundError,
  load: loadBackground,
} = useBackground();
</script>
