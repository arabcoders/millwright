import { ref, shallowReadonly } from 'vue';
import { parse_api_error, request } from '~/utils';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import { useEvents } from '~/composables/useEvents';
import type { LibraryBody, LibraryRow, LibraryProgress } from '~/types/api';

const items = ref<LibraryRow[]>([]);
const isLoading = ref(false);
const lastError = ref<Error | null>(null);
let loaded = false;
let inFlight: Promise<void> | null = null;
let eventsBound = false;

const fetchLibraries = async (): Promise<void> => {
  isLoading.value = true;
  lastError.value = null;
  try {
    const response = await request('/api/libraries');
    if (!response.ok) throw new Error(`Could not load libraries (HTTP ${response.status}).`);
    items.value = (await response.json()) as LibraryRow[];
    loaded = true;
  } catch (error) {
    lastError.value = error instanceof Error ? error : new Error(String(error));
    loaded = false;
  } finally {
    isLoading.value = false;
    inFlight = null;
  }
};

const ensure = (): Promise<void> => {
  if (loaded) return Promise.resolve();
  return (inFlight ??= fetchLibraries());
};

export const applyLibraryDeletionEvent = (payload: {
  library_id: number;
  status: 'deleting' | 'failed' | 'deleted';
  error: string | null;
}): void => {
  const index = items.value.findIndex((library) => library.id === payload.library_id);
  if (index < 0) return;
  if (payload.status === 'deleted') {
    items.value.splice(index, 1);
    return;
  }
  const library = items.value[index];
  if (!library) return;
  items.value[index] = {
    ...library,
    deleted: true,
    suspended: true,
    delete_error: payload.status === 'failed' ? payload.error : null,
  };
};

const reload = (): Promise<void> => inFlight ?? (inFlight = fetchLibraries());

const save = async (payload: LibraryBody, editing: LibraryRow | null): Promise<string | null> => {
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  try {
    if (editing) {
      let current: LibraryRow;
      try {
        const response = await request(`/api/libraries/${editing.id}`);
        if (!response.ok) throw new Error('Library refresh failed.');
        current = (await response.json()) as LibraryRow;
      } catch {
        throw new Error(
          'Could not check the current library settings. Your draft is still available. Check the connection and try Save again.',
        );
      }
      if (current.updated_at !== editing.updated_at)
        throw new Error(
          'This library changed after you opened it. Your draft is still available. Copy your changes, then close and reopen the editor to review the current settings.',
        );
    }
    const body = editing
      ? Object.fromEntries(
          Object.entries(payload).filter(
            ([key, value]) =>
              JSON.stringify(value) !== JSON.stringify(editing[key as keyof LibraryBody]),
          ),
        )
      : payload;
    const response = await request(editing ? `/api/libraries/${editing.id}` : '/api/libraries', {
      method: editing ? 'PATCH' : 'POST',
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const detail = messageFor(await parse_api_error(await response.json().catch(() => null)));
      notify.error(detail);
      return detail;
    }
    notify.success(
      editing ? `Library "${payload.name}" saved.` : `Library "${payload.name}" created.`,
    );
    await reload();
    return null;
  } catch (error) {
    const detail = messageFor(error instanceof Error ? error.message : undefined);
    notify.error(detail);
    return detail;
  }
};

const scan = async (library: Pick<LibraryRow, 'id' | 'name'>): Promise<void> => {
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  try {
    const response = await request(`/api/libraries/${library.id}/scan`, { method: 'POST' });
    if (response.ok) {
      const acknowledgement = (await response.json()) as {
        status: 'queued' | 'already_queued' | 'already_running';
      };
      notify.success(
        acknowledgement.status === 'queued'
          ? `Scan of "${library.name}" queued.`
          : `Scan of "${library.name}" is already ${acknowledgement.status === 'already_running' ? 'running' : 'queued'}.`,
      );
    } else {
      notify.error(messageFor(await parse_api_error(await response.json().catch(() => null))));
    }
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
  }
};

const scanMany = async (libraryIds: number[]): Promise<void> => {
  if (!libraryIds.length) return;
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  try {
    const response = await request('/api/libraries/scan', {
      method: 'POST',
      body: JSON.stringify({ library_ids: libraryIds }),
    });
    if (!response.ok)
      throw new Error(
        (await parse_api_error(await response.json().catch(() => null))) ??
          'Could not queue scans.',
      );
    const { outcomes } = (await response.json()) as {
      outcomes: Array<{ library_id: number; status: string; detail?: string }>;
    };
    const queued = outcomes.filter((item) => item.status === 'queued').length;
    const existing = outcomes.filter((item) =>
      ['already_queued', 'already_running'].includes(item.status),
    ).length;
    const skipped = outcomes.filter((item) => item.status === 'suspended').length;
    const failed = outcomes.filter((item) => ['missing', 'stopped'].includes(item.status));
    const summary = `${queued} queued, ${existing} already queued or running, ${skipped} suspended, ${failed.length} failed.`;
    if (failed.length) notify.error(summary);
    else if (skipped) notify.info(summary);
    else notify.success(summary);
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
  }
};

const cancelScan = async (
  library: Pick<LibraryRow, 'id' | 'name'>,
): Promise<LibraryProgress | null> => {
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  try {
    const response = await request(`/api/libraries/${library.id}/scan/cancel`, {
      method: 'POST',
    });
    if (!response.ok)
      throw new Error(
        (await parse_api_error(await response.json().catch(() => null))) ??
          'Could not cancel scan.',
      );
    const result = (await response.json()) as { status: string; progress: LibraryProgress };
    notify.success(
      result.status === 'not_running'
        ? `No scan to cancel for "${library.name}".`
        : `Cancelling scan for "${library.name}". Slow storage can delay cancellation.`,
    );
    return result.progress;
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
    return null;
  }
};

const setSuspended = async (library: LibraryRow, suspended: boolean): Promise<void> => {
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  try {
    if (!suspended) {
      let current: LibraryRow;
      try {
        const response = await request(`/api/libraries/${library.id}`);
        if (!response.ok) throw new Error('Library refresh failed.');
        current = (await response.json()) as LibraryRow;
      } catch {
        throw new Error(
          'Could not check the current library settings. Refresh the library list and review the settings before resuming.',
        );
      }
      if (current.updated_at !== library.updated_at)
        throw new Error(
          'This library changed since you reviewed it. Refresh the library list and review the current settings before resuming.',
        );
    }
    const response = await request(`/api/libraries/${library.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ suspended }),
    });
    if (!response.ok)
      throw new Error(
        (await parse_api_error(await response.json().catch(() => null))) ??
          `Could not ${suspended ? 'pause' : 'resume'} library.`,
      );
    await reload();
    notify.success(`Library "${library.name}" ${suspended ? 'paused' : 'resumed'}.`);
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
  }
};

const deleteLibrary = async (library: Pick<LibraryRow, 'id' | 'name'>): Promise<boolean> => {
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  let receivedEvent = false;
  let receivedTerminal = false;
  const stop = useEvents().on((event) => {
    if (event.type === 'library.deletion' && event.payload.library_id === library.id) {
      receivedEvent = true;
      receivedTerminal = event.payload.status !== 'deleting';
    }
  });
  try {
    const response = await request(`/api/libraries/${library.id}`, { method: 'DELETE' });
    if (!response.ok)
      throw new Error(
        (await parse_api_error(await response.json().catch(() => null))) ??
          'Could not delete library.',
      );
    const index = items.value.findIndex((item) => item.id === library.id);
    if (index >= 0 && !receivedEvent) {
      const current = items.value[index];
      if (!current) return true;
      items.value[index] = { ...current, deleted: true, suspended: true, delete_error: null };
    }
    if (!receivedTerminal) notify.info(`Deletion of "${library.name}" started.`);
    return true;
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
    return false;
  } finally {
    stop();
  }
};

export const useLibraries = () => {
  if (import.meta.client && !eventsBound) {
    eventsBound = true;
    useEvents().on((event) => {
      if (event.type === 'library.deletion') applyLibraryDeletionEvent(event.payload);
    });
  }
  return {
    items: shallowReadonly(items),
    isLoading: shallowReadonly(isLoading),
    lastError: shallowReadonly(lastError),
    ensure,
    reload,
    save,
    scan,
    scanMany,
    cancelScan,
    setSuspended,
    deleteLibrary,
  };
};
