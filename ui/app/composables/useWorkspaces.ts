import { ref, shallowReadonly } from 'vue';
import { parse_api_error, request } from '~/utils';
import type {
  WorkspaceDetail,
  WorkspaceList,
  WorkspacePruneResult,
  WorkspaceSnapshot,
} from '~/types/api';

export type WorkspaceFilters = {
  query?: string;
  eligibility?: 'all' | 'unused' | 'protected';
  sort_by?:
    | 'workspace_id'
    | 'filename'
    | 'status'
    | 'node_name'
    | 'size_bytes'
    | 'last_activity_at';
  sort_dir?: 'asc' | 'desc';
  limit: number;
  offset: number;
};

const snapshot = ref<WorkspaceList | null>(null);
const summary = ref<WorkspaceSnapshot | null>(null);
const isLoading = ref(false);
const lastError = ref<string | null>(null);
let requestId = 0;
let lastFilters: WorkspaceFilters = { limit: 50, offset: 0 };

const applySummary = (next: WorkspaceSnapshot): void => {
  if (summary.value && next.sampled_at < summary.value.sampled_at) return;
  summary.value = next;
  if (next.error) lastError.value = next.error;
};

const load = async (filters: WorkspaceFilters = lastFilters): Promise<void> => {
  lastFilters = { ...filters };
  const id = ++requestId;
  isLoading.value = true;
  try {
    const params = new URLSearchParams({
      limit: String(filters.limit),
      offset: String(filters.offset),
    });
    for (const key of ['query', 'eligibility', 'sort_by', 'sort_dir'] as const) {
      const value = filters[key];
      if (value) params.set(key, value);
    }
    const response = await request(`/api/workspaces?${params}`);
    if (!response.ok)
      throw new Error(await parse_api_error(await response.json().catch(() => null)));
    const next = (await response.json()) as WorkspaceList;
    if (id === requestId) {
      snapshot.value = next;
      applySummary(next);
      lastError.value = summary.value?.error ?? next.error;
    }
  } catch (error) {
    if (id === requestId) {
      lastError.value = error instanceof Error ? error.message : String(error);
    }
  } finally {
    if (id === requestId) isLoading.value = false;
  }
};

const detail = async (id: number): Promise<WorkspaceDetail> => {
  const response = await request(`/api/workspaces/${id}`);
  if (!response.ok) throw new Error(await parse_api_error(await response.json().catch(() => null)));
  return (await response.json()) as WorkspaceDetail;
};

const prune = async (
  selection: { job_ids: number[] } | { all_unused: true },
): Promise<WorkspacePruneResult> => {
  const response = await request('/api/workspaces/prune', {
    method: 'POST',
    body: JSON.stringify(selection),
  });
  if (!response.ok) throw new Error(await parse_api_error(await response.json().catch(() => null)));
  const result = (await response.json()) as WorkspacePruneResult;
  applySummary(result.snapshot);
  return result;
};

export const useWorkspaces = () => ({
  snapshot: shallowReadonly(snapshot),
  summary: shallowReadonly(summary),
  isLoading: shallowReadonly(isLoading),
  lastError: shallowReadonly(lastError),
  load,
  detail,
  prune,
});
