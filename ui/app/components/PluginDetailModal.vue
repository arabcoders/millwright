<template>
  <UModal
    v-model:open="open"
    title="Plugin details"
    :ui="{ content: 'w-full sm:max-w-2xl', body: 'max-h-[80vh] overflow-y-auto' }"
  >
    <template v-if="plugin" #body>
      <div class="space-y-5">
        <section class="space-y-3">
          <div class="flex items-start gap-3">
            <UIcon
              name="i-lucide-puzzle"
              class="mt-1 size-5 shrink-0 text-toned"
              aria-hidden="true"
            />
            <div class="min-w-0">
              <h2 class="wrap-anywhere text-lg font-semibold text-highlighted">
                {{ plugin.manifest.name }}
              </h2>
              <p class="wrap-anywhere text-sm text-toned">{{ plugin.manifest.id }}</p>
            </div>
          </div>
          <p class="wrap-anywhere text-sm text-toned">{{ plugin.manifest.description }}</p>
        </section>
        <section class="space-y-4 border-t border-default pt-5">
          <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
            <UIcon name="i-lucide-info" class="size-5 text-toned" aria-hidden="true" />
            Plugin metadata
          </h3>
          <dl class="grid min-w-0 gap-x-8 gap-y-4 text-sm sm:grid-cols-3">
            <div class="min-w-0 space-y-1">
              <dt class="text-xs text-muted">Kind</dt>
              <dd class="wrap-anywhere">{{ plugin.manifest.kind }}</dd>
            </div>
            <div class="min-w-0 space-y-1">
              <dt class="text-xs text-muted">Version</dt>
              <dd class="wrap-anywhere">{{ plugin.manifest.version }}</dd>
            </div>
            <div class="min-w-0 space-y-1">
              <dt class="text-xs text-muted">Source</dt>
              <dd class="wrap-anywhere">{{ plugin.source }}</dd>
            </div>
            <div class="min-w-0 space-y-1">
              <dt class="text-xs text-muted">State</dt>
              <dd><StatusBadge :status="plugin.state" group="plugin" /></dd>
            </div>
            <div class="min-w-0 space-y-1">
              <dt class="text-xs text-muted">Author</dt>
              <dd class="wrap-anywhere">
                <a
                  :href="plugin.manifest.url"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="text-primary underline"
                  >{{ plugin.manifest.author }}</a
                >
              </dd>
            </div>
            <div class="min-w-0 space-y-1">
              <dt class="text-xs text-muted">Requires</dt>
              <dd class="wrap-anywhere">
                {{ plugin.manifest.requires.length ? plugin.manifest.requires.join(', ') : 'None' }}
              </dd>
            </div>
            <div class="min-w-0 space-y-1 sm:col-span-3">
              <dt class="text-xs text-muted">Required plugins</dt>
              <dd class="wrap-anywhere">
                {{
                  plugin.manifest.require_plugins.length
                    ? plugin.manifest.require_plugins.join(', ')
                    : 'None'
                }}
              </dd>
            </div>
          </dl>
        </section>
        <section
          v-if="Object.keys(plugin.manifest.facets).length"
          class="space-y-3 border-t border-default pt-5"
        >
          <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
            <UIcon name="i-lucide-info" class="size-5 text-toned" aria-hidden="true" />
            Provided facts
          </h3>
          <div class="divide-y divide-default">
            <div
              v-for="[key, type] in Object.entries(plugin.manifest.facets)"
              :key="key"
              class="flex min-w-0 flex-wrap items-center justify-between gap-2 py-3 text-sm"
            >
              <span class="wrap-anywhere">{{
                `${plugin.manifest.id.split('.')[1] ?? plugin.manifest.id}.${key}`
              }}</span>
              <UBadge color="neutral" variant="subtle" size="sm">{{ type }}</UBadge>
            </div>
          </div>
        </section>
        <section v-if="plugin.manifest.help" class="space-y-3 border-t border-default pt-5">
          <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
            <UIcon name="i-lucide-book-open" class="size-5 text-toned" aria-hidden="true" />
            Help
          </h3>
          <p class="wrap-anywhere whitespace-pre-wrap text-sm text-toned">
            {{ plugin.manifest.help.trim() }}
          </p>
        </section>
        <section class="space-y-3 border-t border-default pt-5">
          <h3
            class="flex items-center justify-between gap-2 text-base font-semibold text-highlighted"
          >
            <span class="flex items-center gap-2">
              <UIcon name="i-lucide-settings-2" class="size-5 text-toned" aria-hidden="true" />
              Inputs
            </span>
            <UButton
              v-if="plugin.inputs.length"
              variant="ghost"
              color="neutral"
              size="sm"
              :trailing-icon="inputsVisible ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
              :aria-label="inputsVisible ? 'Hide inputs' : 'Show inputs'"
              :aria-expanded="inputsVisible"
              @click="inputsVisible = !inputsVisible"
            />
          </h3>
          <div v-if="plugin.inputs.length && inputsVisible" class="divide-y divide-default">
            <div
              v-for="field in plugin.inputs"
              :key="field.name"
              class="min-w-0 py-3 wrap-anywhere text-sm"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-medium">{{ field.name }}</span
                ><UBadge color="neutral" variant="subtle" size="sm">{{ field.type }}</UBadge>
              </div>
              <p v-if="field.help" class="mt-1 text-toned">{{ field.help }}</p>
              <p class="mt-1 text-xs text-toned">
                Default: {{ defaultLabel(field.default)
                }}<span v-if="field.min !== null"> · Min: {{ field.min }}</span
                ><span v-if="field.max !== null"> · Max: {{ field.max }}</span
                ><span v-if="field.values"> · Values: {{ field.values.join(', ') }}</span>
              </p>
            </div>
          </div>
          <UEmpty
            v-if="!plugin.inputs.length"
            size="sm"
            variant="naked"
            icon="i-lucide-settings-2"
            title="No inputs"
          />
        </section>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { useStorage } from '@vueuse/core';

import type { PluginInputField, PluginRow } from '~/types/api';

const { plugin } = defineProps<{ plugin: PluginRow | null }>();
const open = defineModel<boolean>('open', { required: true });
const inputsVisible = useStorage('plugin_inputs_visible', true);

const defaultLabel = (value: PluginInputField['default']): string =>
  Array.isArray(value) ? value.join(', ') : String(value);
</script>
