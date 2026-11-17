<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="plugins">
      <template #actions>
        <div class="flex flex-wrap items-center gap-2">
          <UButton
            size="sm"
            color="primary"
            variant="solid"
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
        <div class="mw-card divide-y divide-default">
          <article
            v-for="plugin in groupedPlugins[kind]"
            :key="plugin.manifest.id"
            class="w-full min-w-0 max-w-full"
          >
            <div
              class="grid min-w-0 gap-4 p-4 sm:p-5 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.5fr)_auto]"
            >
              <div class="min-w-0 space-y-2">
                <h3 class="min-w-0 font-semibold text-highlighted">
                  <button
                    type="button"
                    class="wrap-anywhere text-start hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                    @click="selectPlugin(plugin)"
                  >
                    {{ plugin.manifest.name }}
                  </button>
                </h3>
                <p class="wrap-anywhere text-xs text-toned">
                  {{ plugin.manifest.id }}
                </p>
                <StatusBadge :status="plugin.state" group="plugin" />
              </div>
              <div class="min-w-0 space-y-3">
                <p class="wrap-anywhere text-sm text-toned">
                  {{ plugin.manifest.description }}
                </p>
                <dl class="grid min-w-0 grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
                  <div class="min-w-0 space-y-1">
                    <dt class="flex items-center gap-1.5 text-xs text-muted">
                      <UIcon name="i-lucide-tag" class="size-3.5 shrink-0" aria-hidden="true" />
                      Version
                    </dt>
                    <dd class="wrap-anywhere">{{ plugin.manifest.version }}</dd>
                  </div>
                  <div class="min-w-0 space-y-1">
                    <dt class="flex items-center gap-1.5 text-xs text-muted">
                      <UIcon name="i-lucide-package" class="size-3.5 shrink-0" aria-hidden="true" />
                      Source
                    </dt>
                    <dd class="wrap-anywhere">
                      {{ plugin.source === 'bundled' ? 'Bundled' : 'Uploaded' }}
                    </dd>
                  </div>
                  <div class="min-w-0 space-y-1">
                    <dt class="flex items-center gap-1.5 text-xs text-muted">
                      <UIcon
                        name="i-lucide-sliders-horizontal"
                        class="size-3.5 shrink-0"
                        aria-hidden="true"
                      />
                      Inputs
                    </dt>
                    <dd class="wrap-anywhere">{{ plugin.inputs.length }}</dd>
                  </div>
                </dl>
              </div>
              <div class="flex items-center justify-end gap-1.5 self-start" @click.stop>
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
                <UDropdownMenu
                  v-if="plugin.state !== 'disabled'"
                  :items="pluginActions(plugin)"
                  :modal="false"
                  :content="{ align: 'end' }"
                >
                  <UButton
                    size="sm"
                    color="neutral"
                    variant="ghost"
                    icon="i-lucide-ellipsis"
                    square
                    :aria-label="`Actions for ${plugin.manifest.name}`"
                    :loading="isBusy(plugin, 'restart') || isBusy(plugin, 'stop')"
                    :disabled="!!busy || loading"
                  />
                </UDropdownMenu>
              </div>
            </div>
          </article>
        </div>
      </section>
    </div>
    <UEmpty v-else-if="!lastError" icon="i-lucide-puzzle" title="No plugins loaded." />
    <PluginDetailModal v-model:open="pluginModalOpen" :plugin="selectedPlugin" />
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import type { DropdownMenuItem } from '@nuxt/ui';
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

const pluginActions = (plugin: PluginRow): DropdownMenuItem[] => [
  {
    label: 'Restart',
    icon: 'i-lucide-rotate-cw',
    disabled: !!busy.value || loading.value,
    onSelect: () => restartPlugin(plugin),
  },
  {
    label: 'Stop',
    icon: 'i-lucide-square',
    disabled: !!busy.value || loading.value,
    onSelect: () => stopPlugin(plugin),
  },
];

const kindColor = (kind: PluginKind): 'info' | 'neutral' | 'primary' | 'secondary' => {
  if (kind === 'filter') return 'info';
  if (kind === 'inspector') return 'neutral';
  if (kind === 'transcode') return 'primary';
  return 'secondary';
};
</script>
