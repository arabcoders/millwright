<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="plugins">
      <template #actions>
        <div class="flex flex-wrap items-center gap-2">
          <UButton
            size="sm"
            color="neutral"
            variant="outline"
            icon="i-lucide-upload"
            :loading="uploading"
            @click="uploadInput?.click()"
          >
            Upload plugin
          </UButton>

          <input
            ref="uploadInput"
            type="file"
            accept=".zip,application/zip,application/x-zip-compressed"
            class="hidden"
            @change="onUploadFile"
          />

          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-refresh-cw"
            aria-label="Refresh plugins"
            :loading="loading"
            @click="reloadPlugins"
          >
            Refresh
          </UButton>
        </div>
      </template>
    </PageHeader>

    <UAlert
      v-if="lastError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load plugins"
      :description="lastError.message"
    />

    <div v-if="loading && !plugins.length" class="space-y-2">
      <USkeleton v-for="index in 4" :key="index" class="h-20 rounded-xl" />
    </div>
    <div v-else-if="plugins.length" class="space-y-8">
      <section
        v-for="kind in pluginKinds"
        :key="kind"
        v-show="groupedPlugins[kind].length"
        class="space-y-3"
      >
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="flex items-center gap-2 text-base font-semibold capitalize text-highlighted">
            <UIcon name="i-lucide-puzzle" class="size-5" />
            {{ kind }} plugins
          </h2>
          <UBadge :color="kindColor(kind)" variant="soft" size="sm">
            {{ groupedPlugins[kind].length }}
          </UBadge>
        </div>
        <div class="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
          <article
            v-for="plugin in groupedPlugins[kind]"
            :key="plugin.manifest.id"
            class="mw-card flex h-full flex-col gap-3 p-4"
          >
            <div class="min-w-0">
              <div class="flex min-w-0 items-center gap-2">
                <h3 class="truncate font-semibold text-highlighted">
                  <button
                    type="button"
                    class="truncate text-start hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                    @click="selectPlugin(plugin)"
                  >
                    {{ plugin.manifest.name }}
                  </button>
                </h3>
                <StatusBadge class="ms-auto shrink-0" :status="plugin.state" group="plugin" />
              </div>
              <p class="truncate text-xs text-toned">
                {{ plugin.manifest.id }}
              </p>
              <p class="mt-2 text-sm text-toned">
                {{ plugin.manifest.description }}
              </p>
            </div>
            <div class="grid grid-cols-3 gap-3 text-sm">
              <p class="truncate">
                <span class="text-toned">Version:</span>
                {{ plugin.manifest.version }}
              </p>
              <p class="truncate">
                <span class="text-toned">Source:</span>
                {{ plugin.source === 'bundled' ? 'Bundled' : 'Uploaded' }}
              </p>
              <p class="truncate">
                <span class="text-toned">Inputs:</span>
                {{ plugin.inputs.length }}
              </p>
            </div>
            <div class="mt-auto flex items-center justify-end gap-2" @click.stop>
              <UButton
                size="sm"
                color="neutral"
                variant="outline"
                :icon="plugin.state === 'disabled' ? 'i-lucide-power' : 'i-lucide-power-off'"
                :loading="isBusy(plugin, 'enable') || isBusy(plugin, 'disable')"
                :disabled="!!busy || loading"
                @click="toggleEnabled(plugin)"
              >
                {{ plugin.state === 'disabled' ? 'Enable' : 'Disable' }}
              </UButton>
              <UButton
                v-if="plugin.state !== 'disabled'"
                size="sm"
                color="neutral"
                variant="outline"
                icon="i-lucide-rotate-cw"
                :loading="isBusy(plugin, 'restart')"
                :disabled="!!busy || loading"
                @click="restartPlugin(plugin)"
              >
                Restart
              </UButton>
              <UButton
                v-if="plugin.state !== 'disabled'"
                size="sm"
                color="neutral"
                variant="outline"
                icon="i-lucide-square"
                :loading="isBusy(plugin, 'stop')"
                :disabled="!!busy || loading"
                @click="stopPlugin(plugin)"
              >
                Stop
              </UButton>
            </div>
          </article>
        </div>
      </section>
    </div>
    <div
      v-else-if="!lastError"
      class="mw-card flex flex-col items-center justify-center gap-1 py-10 text-center"
    >
      <UIcon name="i-lucide-puzzle" class="size-6 text-toned" />
      <p class="text-sm text-toned">No plugins loaded.</p>
    </div>
    <PluginDetailModal v-model:open="pluginModalOpen" :plugin="selectedPlugin" />
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import StatusBadge from '~/components/StatusBadge.vue';
import { useDialog } from '~/composables/useDialog';
import { usePlugins } from '~/composables/usePlugins';
import type { PluginKind, PluginRow } from '~/types/api';

const { confirmDialog } = useDialog();

const {
  items: plugins,
  isLoading: loading,
  lastError,
  reload: reloadPlugins,
  ensure: ensurePlugins,
  install: installPlugin,
  setEnabled,
  restart,
  stop,
} = usePlugins();
const selectedPlugin = ref<PluginRow | null>(null);
const pluginModalOpen = ref(false);
const pluginKinds: PluginKind[] = ['filter', 'inspector', 'transcode', 'action'];
const groupedPlugins = computed(
  () =>
    Object.fromEntries(
      pluginKinds.map((kind) => [
        kind,
        plugins.value.filter((plugin) => plugin.manifest.kind === kind),
      ]),
    ) as Record<PluginKind, PluginRow[]>,
);

const selectPlugin = (plugin: PluginRow): void => {
  selectedPlugin.value = plugin;
  pluginModalOpen.value = true;
};

watch(pluginModalOpen, (open) => {
  if (!open) selectedPlugin.value = null;
});

onMounted(() => {
  void ensurePlugins();
});

const uploading = ref(false);
const uploadInput = ref<HTMLInputElement | null>(null);

const onUploadFile = async (event: Event): Promise<void> => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;

  uploading.value = true;
  try {
    await installPlugin(file);
  } finally {
    uploading.value = false;
  }
};

type RowAction = 'enable' | 'disable' | 'restart' | 'stop';

const busy = ref<{ id: string; action: RowAction } | null>(null);

const isBusy = (plugin: PluginRow, action?: RowAction): boolean => {
  const current = busy.value;
  if (!current || current.id !== plugin.manifest.id) return false;
  return action === undefined || current.action === action;
};

const runRowAction = async (
  plugin: PluginRow,
  action: RowAction,
  call: () => Promise<void>,
): Promise<void> => {
  if (loading.value || busy.value) return;
  busy.value = { id: plugin.manifest.id, action };
  try {
    await call();
  } finally {
    busy.value = null;
  }
};

const toggleEnabled = async (plugin: PluginRow): Promise<void> => {
  const enable = plugin.state === 'disabled';
  if (!enable) {
    const { status } = await confirmDialog({
      title: `Disable plugin "${plugin.manifest.name}"`,
      message: 'Flows that use this plugin will fail while it is disabled.',
      confirmColor: 'warning',
      confirmIcon: 'i-lucide-power-off',
    });
    if (!status) return;
  }
  await runRowAction(plugin, enable ? 'enable' : 'disable', () => setEnabled(plugin, enable));
};

const restartPlugin = (plugin: PluginRow): Promise<void> =>
  runRowAction(plugin, 'restart', () => restart(plugin));

const stopPlugin = async (plugin: PluginRow): Promise<void> => {
  const { status } = await confirmDialog({
    title: `Stop plugin "${plugin.manifest.name}"`,
    message: 'The plugin stays stopped until you restart it or the server reloads plugins.',
    confirmColor: 'warning',
    confirmIcon: 'i-lucide-square',
  });
  if (!status) return;
  await runRowAction(plugin, 'stop', () => stop(plugin));
};

const kindColor = (kind: PluginKind): 'info' | 'neutral' | 'primary' | 'secondary' => {
  if (kind === 'filter') return 'info';
  if (kind === 'inspector') return 'neutral';
  if (kind === 'transcode') return 'primary';
  return 'secondary';
};
</script>
