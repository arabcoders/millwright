import { ref, shallowReadonly } from 'vue';
import { parse_api_error, request } from '~/utils';
import type { TrashEntry } from '~/types/api';

export type TrashFilters = {
  query?: string;
  library_id?: string;
  limit: number;
  offset: number;
  sort_by?: 'trashed_at' | 'purge_at' | 'size_bytes' | 'original_path';
  sort_dir?: 'asc' | 'desc';
};

const items = ref<TrashEntry[]>([]);
const total = ref(0);
const totalSizeBytes = ref(0);
const isLoading = ref(false);
const lastError = ref<Error | null>(null);
let requestId = 0;
let lastFilters: TrashFilters | null = null;

const load = async (filters: TrashFilters): Promise<void> => {
  const currentRequest = ++requestId;
  lastFilters = { ...filters };
  const params = new URLSearchParams({
    limit: String(filters.limit),
    offset: String(filters.offset),
  });
  if (filters.query) params.set('query', filters.query);
  if (filters.library_id) params.set('library_id', filters.library_id);
  if (filters.sort_by) params.set('sort_by', filters.sort_by);
  if (filters.sort_dir) params.set('sort_dir', filters.sort_dir);
  isLoading.value = true;
  lastError.value = null;
  try {
    const response = await request(`/api/trash?${params}`);
    if (!response.ok)
      throw new Error(await parse_api_error(await response.json().catch(() => null)));
    const payload = (await response.json()) as {
      items: TrashEntry[];
      pagination: { total: number };
      total_size_bytes: number;
    };
    if (currentRequest === requestId) {
      items.value = payload.items;
      total.value = payload.pagination.total;
      totalSizeBytes.value = payload.total_size_bytes;
    }
  } catch (error) {
    if (currentRequest === requestId)
      lastError.value = error instanceof Error ? error : new Error(String(error));
    throw error;
  } finally {
    if (currentRequest === requestId) isLoading.value = false;
  }
};

type OperationResult =
  | { ok: true; refreshError?: string; offset?: number }
  | { ok: false; error: string };
type EmptyResult =
  | {
      deleted: number;
      deleted_size_bytes: number;
      failed: string[];
      refreshError?: string;
      offset?: number;
    }
  | { ok: false; error: string };

const removeItem = (entryId: string, sizeBytes: number): void => {
  items.value = items.value.filter((entry) => entry.id !== entryId);
  total.value = Math.max(0, total.value - 1);
  totalSizeBytes.value = Math.max(0, totalSizeBytes.value - sizeBytes);
};

const restore = async (
  entryId: string,
  options: { replace_existing: boolean; move_to_trash: boolean; sync_to_disk: boolean },
  filters: TrashFilters,
): Promise<OperationResult> => {
  try {
    const response = await request(`/api/trash/${entryId}/restore`, {
      method: 'POST',
      body: JSON.stringify(options),
    });
    if (!response.ok)
      return { ok: false, error: await parse_api_error(await response.json().catch(() => null)) };
    try {
      await load(filters);
      const offset = total.value
        ? Math.min(filters.offset, Math.floor((total.value - 1) / filters.limit) * filters.limit)
        : 0;
      if (offset !== filters.offset) await load({ ...filters, offset });
      return { ok: true, offset };
    } catch {
      return {
        ok: true,
        refreshError: 'File restored, but trash could not be refreshed. Try Refresh.',
      };
    }
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
};

const expunge = async (entryId: string): Promise<OperationResult> => {
  try {
    const response = await request(`/api/trash/${entryId}`, { method: 'DELETE' });
    if (!response.ok)
      return { ok: false, error: await parse_api_error(await response.json().catch(() => null)) };
    const entry = items.value.find((item) => item.id === entryId);
    if (entry) removeItem(entryId, entry.size_bytes);
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
};

const empty = async (): Promise<EmptyResult> => {
  const snapshot = items.value;
  const initialRequest = requestId;
  try {
    const response = await request('/api/trash', { method: 'DELETE' });
    if (!response.ok)
      return { ok: false, error: await parse_api_error(await response.json().catch(() => null)) };
    const outcome = (await response.json()) as {
      deleted: number;
      deleted_size_bytes: number;
      failed: string[];
    };
    if (!outcome.failed.length && requestId === initialRequest && items.value === snapshot) {
      requestId += 1;
      isLoading.value = false;
      lastError.value = null;
      items.value = [];
      total.value = 0;
      totalSizeBytes.value = 0;
      if (lastFilters) lastFilters = { ...lastFilters, offset: 0 };
      return { ...outcome, offset: 0 };
    } else {
      const failed = new Set(outcome.failed);
      for (const entry of snapshot.filter((entry) => !failed.has(entry.id))) {
        const current = items.value.find((item) => item.id === entry.id);
        if (current) removeItem(current.id, current.size_bytes);
      }
      if (lastFilters) {
        const filters = lastFilters;
        const refreshId = requestId + 1;
        try {
          await load(filters);
          if (requestId !== refreshId) return outcome;
          const offset = total.value
            ? Math.min(
                filters.offset,
                Math.floor((total.value - 1) / filters.limit) * filters.limit,
              )
            : 0;
          if (offset !== filters.offset) {
            await load({ ...filters, offset });
            if (requestId === refreshId + 1) return { ...outcome, offset };
          }
        } catch {
          return {
            ...outcome,
            refreshError: 'Trash could not be refreshed after deletion. Try Refresh.',
          };
        }
      }
    }
    return outcome;
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
};

export const useTrash = () => ({
  items: shallowReadonly(items),
  total: shallowReadonly(total),
  totalSizeBytes: shallowReadonly(totalSizeBytes),
  isLoading: shallowReadonly(isLoading),
  lastError: shallowReadonly(lastError),
  load,
  restore,
  expunge,
  empty,
});
