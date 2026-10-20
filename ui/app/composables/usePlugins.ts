import { ref, shallowReadonly } from 'vue';
import { parse_api_error, request } from '~/utils';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import type { PluginRow } from '~/types/api';

const items = ref<PluginRow[]>([]);
const isLoading = ref(false);
const lastError = ref<Error | null>(null);
let pending: Promise<void> | null = null;
let loaded = false;
let revision = 0;
const notify = useNotification();
const { messageFor } = useApiErrorMessage();

const fetchPlugins = async (): Promise<void> => {
  const version = revision;
  isLoading.value = true;
  lastError.value = null;
  try {
    const response = await request('/api/plugins');
    if (!response.ok) throw new Error(`Could not load plugins (HTTP ${response.status}).`);
    const rows = (await response.json()) as PluginRow[];
    if (version === revision) items.value = rows;
    loaded = true;
  } catch (error) {
    lastError.value = error instanceof Error ? error : new Error(String(error));
    loaded = false;
  } finally {
    isLoading.value = false;
  }
};

const ensure = (): Promise<void> => {
  if (loaded || pending) return pending ?? Promise.resolve();
  pending = fetchPlugins().finally(() => {
    pending = null;
  });
  return pending;
};

const reload = (): Promise<void> => {
  if (pending) return pending;
  pending = fetchPlugins().finally(() => {
    pending = null;
  });
  return pending;
};

const run = async (call: () => Promise<Response>, success: string): Promise<void> => {
  try {
    const response = await call();
    if (response.ok) {
      await saved((await response.json()) as PluginRow, success);
    } else {
      notify.error(messageFor(await parse_api_error(await response.json().catch(() => null))));
    }
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
  }
};

const saved = async (plugin: PluginRow, success: string): Promise<void> => {
  revision++;
  items.value = [...items.value.filter((item) => item.manifest.id !== plugin.manifest.id), plugin];
  notify.success(success);
  await pending;
  await reload();
  if (lastError.value)
    notify.warning('Plugin changed, but the list could not be refreshed. Try Refresh.');
};

const install = async (file: File): Promise<void> => {
  const body = new FormData();
  body.append('file', file);
  try {
    const response = await request('/api/plugins', { method: 'POST', body });
    if (response.ok) {
      const plugin = (await response.json()) as PluginRow;
      await saved(plugin, `Plugin "${plugin.manifest.name}" installed.`);
    } else {
      notify.error(messageFor(await parse_api_error(await response.json().catch(() => null))));
    }
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
  }
};

const setEnabled = (plugin: PluginRow, enabled: boolean): Promise<void> =>
  run(
    () =>
      request(`/api/plugins/${encodeURIComponent(plugin.manifest.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled }),
      }),
    `Plugin "${plugin.manifest.name}" ${enabled ? 'enabled' : 'disabled'}.`,
  );

const restart = (plugin: PluginRow): Promise<void> =>
  run(
    () =>
      request(`/api/plugins/${encodeURIComponent(plugin.manifest.id)}/restart`, { method: 'POST' }),
    `Plugin "${plugin.manifest.name}" restarted.`,
  );

const stop = (plugin: PluginRow): Promise<void> =>
  run(
    () =>
      request(`/api/plugins/${encodeURIComponent(plugin.manifest.id)}/stop`, { method: 'POST' }),
    `Plugin "${plugin.manifest.name}" stopped.`,
  );

export const usePlugins = () => ({
  items: shallowReadonly(items),
  isLoading: shallowReadonly(isLoading),
  lastError: shallowReadonly(lastError),
  ensure,
  reload,
  install,
  setEnabled,
  restart,
  stop,
});
