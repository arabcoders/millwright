import { ref, shallowReadonly } from 'vue';
import { parse_api_error, request } from '~/utils';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import type { NodeChanges, NodeRow, NodeUsage } from '~/types/api';

const items = ref<NodeRow[]>([]);
const isLoading = ref(false);
const lastError = ref<Error | null>(null);
let pending: Promise<void> | null = null;
let loaded = false;

const fetchNodes = async (): Promise<void> => {
  isLoading.value = true;
  lastError.value = null;
  try {
    const response = await request('/api/nodes');
    if (!response.ok) throw new Error(`Failed to load nodes (${response.status})`);
    items.value = (await response.json()) as NodeRow[];
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
  pending = fetchNodes().finally(() => {
    pending = null;
  });
  return pending;
};

const reload = (): Promise<void> => {
  if (pending) return pending;
  pending = fetchNodes().finally(() => {
    pending = null;
  });
  return pending;
};

const upsert = (row: NodeRow): void => {
  const index = items.value.findIndex((item) => item.id === row.id);
  if (index === -1) items.value.push(row);
  else items.value[index] = row;
};

const remove = (id: number): void => {
  items.value = items.value.filter((item) => item.id !== id);
};

const update = async (id: number, changes: NodeChanges): Promise<NodeRow> => {
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  try {
    const response = await request(`/api/nodes/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(changes),
    });
    if (!response.ok)
      throw new Error(await parse_api_error(await response.json().catch(() => null)));
    const row = (await response.json()) as NodeRow;
    upsert(row);
    notify.success(
      'paused' in changes
        ? `${row.display_name} ${row.paused ? 'paused' : 'resumed'}.`
        : `Saved changes to ${row.display_name}.`,
    );
    return row;
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
    throw error;
  }
};

const deleteNode = async (id: number): Promise<void> => {
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  try {
    const node = items.value.find((item) => item.id === id);
    const response = await request(`/api/nodes/${id}`, { method: 'DELETE' });
    if (!response.ok)
      throw new Error(await parse_api_error(await response.json().catch(() => null)));
    remove(id);
    if (node) notify.success(`Removed node ${node.display_name}`);
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
    throw error;
  }
};

const setStatus = (
  name: string,
  state: string,
  active_jobs: number,
  active_job_ids: number[],
  active_transcodes: number,
  active_healthchecks: number,
  active_mkv_stats: number,
): void => {
  const node = items.value.find((item) => item.name === name);
  if (node) {
    if (state === 'online' && node.state !== 'online') node.usage = null;
    node.state = state;
    node.active_jobs = active_jobs;
    node.active_job_ids = active_job_ids;
    node.active_transcodes = active_transcodes;
    node.active_healthchecks = active_healthchecks;
    node.active_mkv_stats = active_mkv_stats;
  }
};

const setUsage = (name: string, usage: NodeUsage, last_seen_at: string | null): void => {
  const node = items.value.find((item) => item.name === name);
  if (node) {
    node.usage = usage;
    node.last_seen_at = last_seen_at;
  }
};

export const useNodes = () => ({
  items: shallowReadonly(items),
  isLoading: shallowReadonly(isLoading),
  lastError: shallowReadonly(lastError),
  ensure,
  reload,
  upsert,
  remove,
  update,
  deleteNode,
  setStatus,
  setUsage,
});
