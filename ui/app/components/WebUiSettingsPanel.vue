<template>
  <USlideover
    v-model:open="open"
    title="Web UI settings"
    description="Changes are saved to this browser."
    side="right"
    :ui="{
      content: 'w-full sm:max-w-lg',
      body: 'p-4 sm:p-5',
    }"
    @after:leave="$emit('after:leave')"
  >
    <template #body>
      <div class="w-full space-y-4">
        <section class="mw-card-padded-lg space-y-4">
          <h2 class="flex items-center gap-2 font-semibold text-highlighted">
            <UIcon name="i-lucide-palette" class="size-5 text-toned" aria-hidden="true" />
            Appearance
          </h2>
          <UFormField label="Theme">
            <USelect v-model="color.preference" :items="themes" class="w-full" />
          </UFormField>
          <USwitch
            v-model="animations"
            label="Page animations"
            color="neutral"
            :ui="settingsSwitchUi"
          />
        </section>

        <section class="mw-card-padded-lg space-y-4">
          <h2 class="flex items-center gap-2 font-semibold text-highlighted">
            <UIcon name="i-lucide-image" class="size-5 text-toned" aria-hidden="true" />
            Background
          </h2>
          <USwitch
            v-model="backgroundEnabled"
            label="Library artwork background"
            color="neutral"
            :ui="settingsSwitchUi"
          />
          <template v-if="backgroundEnabled">
            <UFormField
              label="Background visibility"
              :hint="`${Math.round(backgroundVisibility * 100)}%`"
            >
              <USlider
                v-model="backgroundVisibility"
                :min="0"
                :max="1"
                :step="0.01"
                aria-label="Background visibility"
              />
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
const settingsSwitchUi = {
  root: 'w-full flex-row-reverse justify-between gap-3',
  wrapper: 'ms-0 flex-1',
};
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
