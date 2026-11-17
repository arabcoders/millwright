<template>
  <UModal
    :open="open"
    :title="registration ? 'Edit webhook' : 'New webhook'"
    :dismissible="!saving"
    :close="{ disabled: saving }"
    :ui="{ content: 'w-full sm:max-w-2xl', body: 'max-h-[80vh] overflow-y-auto p-4 sm:p-5' }"
    @update:open="handleOpenChange"
    @after:leave="emit('after:leave')"
  >
    <template #body>
      <form id="webhookForm" class="space-y-5" @submit.prevent="save">
        <div class="grid gap-4 sm:grid-cols-2">
          <UFormField label="Name" required>
            <UInput
              v-model="name"
              icon="i-lucide-webhook"
              class="w-full"
              :disabled="!!registration || saving"
              required
            />
          </UFormField>
          <UFormField label="Type" required>
            <USelect v-model="type" :items="types" :disabled="saving" class="w-full" />
          </UFormField>
        </div>
        <UAlert color="info" variant="soft" icon="i-lucide-info">
          <template #description>
            <div v-if="type === 'generic'" class="space-y-3">
              <ul class="list-disc space-y-1 pl-4">
                <li>Method: <code>POST</code></li>
                <li>Content-Type: <code>application/json</code></li>
              </ul>
              <div>
                <p class="font-medium">One path</p>
                <code class="block break-all">{"path":"/media/tv/Show/episode.mkv"}</code>
              </div>
              <div>
                <p class="font-medium">Several paths</p>
                <code class="block break-all"
                  >{"path":["/media/tv/Show","/media/movies/Film.mkv"]}</code
                >
              </div>
              <p>Paths can be files or folders, including ones that were deleted.</p>
            </div>
            <div v-else class="space-y-3">
              <ol class="list-decimal space-y-1 pl-4">
                <li>In {{ type === 'sonarr' ? 'Sonarr' : 'Radarr' }}, add a Webhook connection.</li>
                <li>Select <code>POST</code> and paste the receiver URL.</li>
                <li>Enable import, upgrade, rename, and deletion events.</li>
              </ol>
            </div>
          </template>
        </UAlert>
        <section class="space-y-4 border-t border-default pt-5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h3 class="flex items-center gap-2 font-semibold text-highlighted">
              <UIcon name="i-lucide-folder-symlink" class="size-5 text-toned" aria-hidden="true" />
              Path replacements
            </h3>
            <UButton
              size="sm"
              color="neutral"
              variant="outline"
              icon="i-lucide-plus"
              :disabled="saving"
              @click="pathReplacements.push({ source: '', destination: '' })"
              >Add replacement</UButton
            >
          </div>
          <p class="text-xs text-toned">
            Use these when the sending app and Millwright use different paths for the same folder.
          </p>
          <div
            v-for="(replacement, index) in pathReplacements"
            :key="index"
            class="grid items-end gap-3 border-t border-default pt-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]"
          >
            <UFormField
              label="Source prefix"
              help="The folder path Sonarr, Radarr, or your other app reports."
              class="min-w-0 flex-1"
              required
            >
              <UInput v-model="replacement.source" class="w-full" :disabled="saving" required />
            </UFormField>
            <UFormField
              label="Destination prefix"
              help="The same folder as seen by the Millwright server."
              class="min-w-0 flex-1"
              required
            >
              <UInput
                v-model="replacement.destination"
                class="w-full"
                :disabled="saving"
                required
              />
            </UFormField>
            <UButton
              color="neutral"
              variant="outline"
              size="sm"
              class="justify-self-end"
              icon="i-lucide-trash-2"
              :aria-label="`Remove replacement ${index + 1}`"
              :disabled="saving"
              @click="pathReplacements.splice(index, 1)"
            />
          </div>
        </section>
        <UAlert v-if="error" color="error" icon="i-lucide-triangle-alert" :description="error" />
      </form>
    </template>
    <template #footer>
      <div class="flex w-full flex-wrap items-center justify-end gap-2">
        <UButton
          type="button"
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-x"
          :disabled="saving"
          @click="handleOpenChange(false)"
          >Cancel</UButton
        >
        <UButton
          type="submit"
          form="webhookForm"
          size="sm"
          icon="i-lucide-save"
          :loading="saving"
          >{{ registration ? 'Save changes' : 'Add webhook' }}</UButton
        >
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { useDirtyCloseGuard } from '~/composables/useDirtyCloseGuard';
import { useDirtyState } from '~/composables/useDirtyState';
import type { WebhookRegistration, WebhookType } from '~/types/api';
import { parse_api_error, request, to_api_error } from '~/utils';

const props = withDefaults(
  defineProps<{ open?: boolean; registration: WebhookRegistration | null }>(),
  { open: false },
);
const emit = defineEmits<{
  (event: 'update:open', open: boolean): void;
  (event: 'after:leave'): void;
  // useOverlay infers its result from the last overload.
  (event: 'close', saved: WebhookRegistration | null): void;
}>();
const name = ref(props.registration?.name ?? '');
const type = ref<WebhookType>(props.registration?.type ?? 'sonarr');
const pathReplacements = ref(
  props.registration?.path_replacements.map((item) => ({ ...item })) ?? [],
);
const types = [
  { label: 'Sonarr', value: 'sonarr' },
  { label: 'Radarr', value: 'radarr' },
  { label: 'Generic', value: 'generic' },
];
const saving = ref(false);
const error = ref('');
const modalOpen = ref(true);
const dirty = useDirtyState(
  computed(() => ({
    name: name.value,
    type: type.value,
    pathReplacements: pathReplacements.value,
  })),
);
dirty.markClean();
const { handleOpenChange } = useDirtyCloseGuard(modalOpen, dirty.isDirty, () => {
  if (!saving.value) {
    dirty.markClean();
    modalOpen.value = false;
    emit('close', null);
  }
});

const save = async (): Promise<void> => {
  if (saving.value) return;
  saving.value = true;
  error.value = '';
  try {
    const listing = await request('/api/webhooks');
    if (!listing.ok) {
      error.value = await parse_api_error(await listing.json().catch(() => null));
      return;
    }
    const current = ((await listing.json()) as WebhookRegistration[]).find(
      (item) => item.name === name.value,
    );
    if (
      props.registration
        ? !current || current.updated_at !== props.registration.updated_at
        : current
    ) {
      error.value = props.registration
        ? 'This webhook changed or was deleted. Your draft is kept. Copy your changes, then reopen the editor to review its current settings.'
        : 'A webhook with this name already exists. Choose another name or edit that webhook.';
      return;
    }
    const response = await request(`/api/webhooks/${encodeURIComponent(name.value)}`, {
      method: 'PUT',
      body: JSON.stringify({ type: type.value, path_replacements: pathReplacements.value }),
    });
    if (!response.ok) {
      error.value = await parse_api_error(await response.json().catch(() => null));
      return;
    }
    const saved = (await response.json()) as WebhookRegistration;
    dirty.markClean();
    modalOpen.value = false;
    emit('close', saved);
  } catch (failure) {
    error.value = to_api_error(failure).message;
  } finally {
    saving.value = false;
  }
};
</script>
