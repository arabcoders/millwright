import { ref, shallowReadonly } from 'vue';
import { ApiError, parse_api_error, request } from '~/utils';

export type Target = { name: string; url: string; created_at: string; updated_at: string };

const items = ref<Target[]>([]);
const isLoading = ref(false);
const lastError = ref<Error | null>(null);
let inFlight: Promise<void> | null = null;
let loaded = false;
let revision = 0;

const fetchItems = async (): Promise<void> => {
  const version = revision;
  isLoading.value = true;
  lastError.value = null;
  try {
    const response = await request('/api/notifications');
    if (!response.ok)
      throw new ApiError(await parse_api_error(await response.json().catch(() => null)), {
        status: response.status,
      });
    const rows = (await response.json()) as Target[];
    if (version === revision) items.value = rows;
    loaded = true;
  } catch (error) {
    lastError.value = error instanceof Error ? error : null;
    loaded = false;
    throw error;
  } finally {
    isLoading.value = false;
  }
};

const reload = (): Promise<void> => (inFlight ??= fetchItems().finally(() => (inFlight = null)));
const ensure = (): Promise<void> => (loaded ? Promise.resolve() : reload());

export type MutationResult = {
  ok: boolean;
  status: number;
  detail?: string;
  refreshError?: string;
};

const mutate = async (
  path: string,
  options: RequestInit,
  name?: string,
): Promise<MutationResult> => {
  const response = await request(path, options);
  if (!response.ok)
    return {
      ok: false,
      status: response.status,
      detail: await parse_api_error(await response.json().catch(() => null)),
    };
  if (options.method === 'POST') return { ok: true, status: response.status };
  revision++;
  if (options.method === 'DELETE') items.value = items.value.filter((item) => item.name !== name);
  else {
    const row = (await response.json()) as Target;
    items.value = [...items.value.filter((item) => item.name !== row.name), row].sort((a, b) =>
      a.name.localeCompare(b.name),
    );
  }
  try {
    await inFlight?.catch(() => {});
    await reload();
  } catch {
    return {
      ok: true,
      status: response.status,
      refreshError:
        'Notification target changed, but the list could not be refreshed. Try Refresh.',
    };
  }
  return { ok: true, status: response.status };
};

const save = async (name: string, url: string, editing: Target | null): Promise<MutationResult> => {
  try {
    await inFlight?.catch(() => {});
    await reload();
  } catch {
    return {
      ok: false,
      status: 0,
      detail: 'Could not check existing notification targets.',
    };
  }
  const current = items.value.find((item) => item.name === name);
  if (editing && (!current || current.updated_at !== editing.updated_at))
    return {
      ok: false,
      status: 409,
      detail: 'This notification target changed or was deleted after you opened it.',
    };
  if (!editing && current)
    return {
      ok: false,
      status: 409,
      detail: `A notification target named "${name}" already exists.`,
    };
  return mutate(`/api/notifications/${encodeURIComponent(name)}`, {
    method: 'PUT',
    body: JSON.stringify({ url }),
  });
};

const remove = (name: string): Promise<MutationResult> =>
  mutate(`/api/notifications/${encodeURIComponent(name)}`, { method: 'DELETE' }, name);

const sendTest = (path: string): Promise<MutationResult> => mutate(path, { method: 'POST' });

const test = (name: string): Promise<MutationResult> =>
  sendTest(`/api/notifications/${encodeURIComponent(name)}/test`);

export const useNotificationTargets = () => ({
  items: shallowReadonly(items),
  isLoading: shallowReadonly(isLoading),
  lastError: shallowReadonly(lastError),
  ensure,
  reload,
  save,
  test,
  remove,
});
