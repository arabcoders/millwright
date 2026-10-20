<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="webhooks">
      <template #actions>
        <div class="flex flex-wrap items-center gap-2">
          <UButton
            size="sm"
            color="neutral"
            variant="outline"
            icon="i-lucide-plus"
            :disabled="!!busyReason"
            :title="busyReason || undefined"
            @click="openEditor()"
            >New webhook</UButton
          >
          <UButton
            size="sm"
            color="neutral"
            variant="outline"
            icon="i-lucide-refresh-cw"
            :loading="loading"
            :disabled="deleting !== null"
            :title="deleting !== null ? 'Wait for deletion to finish.' : undefined"
            @click="load"
            >Refresh</UButton
          >
        </div>
      </template>
    </PageHeader>
    <UAlert
      v-if="error"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load webhooks"
      :description="error"
    />
    <div v-if="loading && !items.length" class="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      <USkeleton v-for="index in 3" :key="index" class="h-52 rounded-lg" />
    </div>
    <div v-else-if="items.length" class="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      <article
        v-for="item in items"
        :key="item.name"
        class="mw-card flex h-full min-w-0 flex-col gap-3 p-4"
      >
        <div class="min-w-0">
          <h2 class="truncate font-semibold text-highlighted">{{ item.name }}</h2>
          <p class="text-xs text-toned">{{ item.type }}</p>
        </div>
        <UFormField label="API key" size="sm">
          <div class="flex items-center gap-2">
            <UInput :model-value="item.api_key" readonly size="sm" class="min-w-0 flex-1" />
            <UButton
              size="sm"
              color="neutral"
              variant="outline"
              class="shrink-0"
              icon="i-lucide-copy"
              aria-label="Copy API key"
              @click="copyText(item.api_key)"
            />
          </div>
        </UFormField>
        <UFormField label="Receiver URL" size="sm">
          <div class="flex items-center gap-2">
            <UInput :model-value="receiverUrl(item)" readonly size="sm" class="min-w-0 flex-1" />
            <UButton
              size="sm"
              color="neutral"
              variant="outline"
              class="shrink-0"
              icon="i-lucide-copy"
              aria-label="Copy receiver URL"
              @click="copyText(receiverUrl(item))"
            />
          </div>
        </UFormField>
        <dl v-if="item.path_replacements.length" class="text-xs">
          <div class="min-w-0">
            <dt class="text-dimmed">Path replacements</dt>
            <dd class="mt-0.5 text-toned">
              <ul class="space-y-1">
                <li v-for="replacement in item.path_replacements" :key="replacement.source">
                  <UTooltip :text="`${replacement.source} → ${replacement.destination}`">
                    <span class="block truncate"
                      >{{ replacement.source }} → {{ replacement.destination }}</span
                    >
                  </UTooltip>
                </li>
              </ul>
            </dd>
          </div>
        </dl>
        <div class="mt-auto flex flex-wrap items-center justify-end gap-2">
          <UButton
            size="sm"
            color="neutral"
            variant="outline"
            icon="i-lucide-pen-line"
            :disabled="!!busyReason"
            :title="busyReason || undefined"
            @click="openEditor(item)"
            >Edit</UButton
          >
          <UButton
            size="sm"
            color="neutral"
            variant="outline"
            icon="i-lucide-trash-2"
            :loading="deleting === item.name"
            :disabled="!!busyReason"
            :title="busyReason || undefined"
            @click="remove(item)"
            >Delete</UButton
          >
        </div>
      </article>
    </div>
    <div
      v-else-if="!error"
      class="mw-card flex flex-col items-center justify-center gap-2 py-10 text-center"
    >
      <UIcon name="i-lucide-webhook" class="size-7 text-toned" />
      <p class="text-sm text-toned">No incoming webhooks configured yet.</p>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import WebhookEditorModal from '~/components/WebhookEditorModal.vue';
import { useDialog } from '~/composables/useDialog';
import type { WebhookRegistration } from '~/types/api';
import { copyText, parse_api_error, request, to_api_error, uri } from '~/utils';

const items = ref<WebhookRegistration[]>([]);
const loading = ref(false);
const deleting = ref<string | null>(null);
const error = ref('');
const busyReason = computed(() =>
  loading.value
    ? 'Loading webhooks.'
    : deleting.value !== null
      ? 'Wait for deletion to finish.'
      : '',
);
const notify = useNotification();
const { confirmDialog } = useDialog();
const overlay = useOverlay();
const receiverUrl = (item: WebhookRegistration): string =>
  new URL(
    `${uri('/api/webhooks/receive')}?api_key=${encodeURIComponent(item.api_key)}`,
    window.location.origin,
  ).href;

const load = async (): Promise<void> => {
  if (loading.value) return;
  loading.value = true;
  error.value = '';
  try {
    const response = await request('/api/webhooks');
    if (!response.ok)
      throw new Error(await parse_api_error(await response.json().catch(() => null)));
    items.value = (await response.json()) as WebhookRegistration[];
  } catch (failure) {
    error.value = to_api_error(failure).message;
  } finally {
    loading.value = false;
  }
};

const openEditor = async (registration: WebhookRegistration | null = null): Promise<void> => {
  if (busyReason.value) return;
  const modal = overlay.create(WebhookEditorModal, {
    props: { registration },
    destroyOnClose: true,
  });
  const saved = await modal.open().result;
  if (saved) {
    items.value = [...items.value.filter((item) => item.name !== saved.name), saved].sort((a, b) =>
      a.name.localeCompare(b.name),
    );
    notify.success('Webhook saved.');
    await load();
  }
};

const remove = async (registration: WebhookRegistration): Promise<void> => {
  if (busyReason.value) return;
  deleting.value = registration.name;
  try {
    const { status } = await confirmDialog({
      title: 'Delete webhook',
      message: `Delete "${registration.name}" and revoke its API key?`,
      confirmText: 'Delete',
      confirmColor: 'error',
      confirmIcon: 'i-lucide-trash-2',
    });
    if (!status) return;
    const response = await request(`/api/webhooks/${encodeURIComponent(registration.name)}`, {
      method: 'DELETE',
    });
    if (!response.ok)
      throw new Error(await parse_api_error(await response.json().catch(() => null)));
    items.value = items.value.filter((item) => item.name !== registration.name);
    notify.success('Webhook deleted and API key revoked.');
  } catch (failure) {
    notify.error(to_api_error(failure).message);
  } finally {
    deleting.value = null;
  }
};

onMounted(() => void load());
</script>
