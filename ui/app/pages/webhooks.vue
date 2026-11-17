<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="webhooks">
      <template #actions>
        <div class="flex flex-wrap items-center gap-2">
          <UButton
            size="sm"
            color="primary"
            variant="solid"
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
    <div v-if="loading && !items.length" class="space-y-3">
      <USkeleton v-for="index in 3" :key="index" class="h-40 rounded-lg" />
    </div>
    <div v-else-if="items.length" class="mw-card divide-y divide-default" :aria-busy="loading">
      <article
        v-for="item in items"
        :key="item.name"
        class="grid min-w-0 gap-4 p-4 sm:p-5 xl:grid-cols-[minmax(0,0.7fr)_minmax(0,1.5fr)_auto]"
      >
        <div class="min-w-0">
          <h2 class="wrap-anywhere text-base font-semibold text-highlighted">{{ item.name }}</h2>
          <p class="mt-2 flex items-center gap-1.5 text-xs text-toned">
            <UIcon name="i-lucide-webhook" class="size-3.5 shrink-0" aria-hidden="true" />
            {{ item.type }}
          </p>
        </div>
        <dl class="min-w-0 space-y-4 text-sm">
          <div class="min-w-0 space-y-1">
            <dt class="flex items-center gap-1.5 text-xs text-muted">
              <UIcon name="i-lucide-key-round" class="size-3.5 shrink-0" aria-hidden="true" />
              API key
            </dt>
            <dd class="flex min-w-0 items-start gap-2">
              <code class="min-w-0 flex-1 wrap-anywhere py-1 text-xs">{{ item.api_key }}</code>
              <UButton
                size="xs"
                color="neutral"
                variant="ghost"
                class="shrink-0"
                icon="i-lucide-copy"
                aria-label="Copy API key"
                @click="copyText(item.api_key)"
              />
            </dd>
          </div>
          <div class="min-w-0 space-y-1">
            <dt class="flex items-center gap-1.5 text-xs text-muted">
              <UIcon name="i-lucide-link" class="size-3.5 shrink-0" aria-hidden="true" />
              Receiver URL
            </dt>
            <dd class="flex min-w-0 items-start gap-2">
              <code class="min-w-0 flex-1 wrap-anywhere py-1 text-xs">{{ receiverUrl(item) }}</code>
              <UButton
                size="xs"
                color="neutral"
                variant="ghost"
                class="shrink-0"
                icon="i-lucide-copy"
                aria-label="Copy receiver URL"
                @click="copyText(receiverUrl(item))"
              />
            </dd>
          </div>
          <div v-if="item.path_replacements.length" class="min-w-0 space-y-1">
            <dt class="flex items-center gap-1.5 text-xs text-muted">
              <UIcon name="i-lucide-folder-sync" class="size-3.5 shrink-0" aria-hidden="true" />
              Path replacements
            </dt>
            <dd class="text-xs">
              <ul class="space-y-1">
                <li
                  v-for="replacement in item.path_replacements"
                  :key="replacement.source"
                  class="wrap-anywhere font-mono"
                >
                  {{ replacement.source }} → {{ replacement.destination }}
                </li>
              </ul>
            </dd>
          </div>
        </dl>
        <div class="flex flex-wrap items-start justify-end gap-1.5">
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
          <UDropdownMenu :items="webhookActions(item)" :modal="false" :content="{ align: 'end' }">
            <UButton
              size="sm"
              color="neutral"
              variant="ghost"
              icon="i-lucide-ellipsis"
              square
              :aria-label="`Actions for ${item.name}`"
              :loading="deleting === item.name"
              :disabled="!!busyReason"
              :title="busyReason || undefined"
            />
          </UDropdownMenu>
        </div>
      </article>
    </div>
    <UEmpty
      v-else-if="!error"
      icon="i-lucide-webhook"
      title="No incoming webhooks configured yet."
    />
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import type { DropdownMenuItem } from '@nuxt/ui';
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

const webhookActions = (registration: WebhookRegistration): DropdownMenuItem[] => [
  {
    label: 'Delete',
    icon: 'i-lucide-trash-2',
    color: 'error',
    disabled: !!busyReason.value,
    onSelect: () => remove(registration),
  },
];

onMounted(() => void load());
</script>
