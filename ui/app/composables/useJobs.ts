import { ref, shallowReadonly } from 'vue';
import { parse_api_error, request } from '~/utils';
import type { JobDetail, JobRow, JobStatus } from '~/types/api';
import { useEvents, type ServerEvent } from './useEvents';
import { applyJobProgress, applyQueuedJob, compareRows, mergeJobState } from './stateHelpers';
import { processingJobStatuses } from '~/utils/jobList';

export type JobFilters = {
  status: string;
  kind?: string;
  query?: string;
  limit: number;
  offset: number;
  sort_by?: 'id' | 'created_at' | 'updated_at' | 'priority' | 'status' | 'path';
  sort_dir?: 'asc' | 'desc';
};

export type JobAction = 'cancel' | 'retry' | 'bump';
export type BulkJobAction = JobAction | 'delete';
export type JobLogChunk = { text: string; complete: boolean; missing: boolean };
export type JobError = {
  detail?: string;
  status?: number;
  cause?: unknown;
};
export type RetryAccepted = {
  id: number;
  status: 'pending';
};
export type JobActionResult =
  | { ok: true; accepted?: RetryAccepted }
  | { ok: false; error: JobError };
export type JobRetryResult = { ok: true; accepted: RetryAccepted } | { ok: false; error: JobError };
export type BulkJobResult = {
  succeeded: number[];
  failed: { id: number; detail: string }[];
};
export type BulkRequestResult = {
  accepted: number[];
  failed: { id: number; detail: string }[];
};

const items = ref<JobRow[]>([]);
const total = ref(0);
const isLoading = ref(false);
const lastError = ref<JobError | null>(null);
let latestRequest = 0;
let activeFilters: JobFilters | null = null;
let snapshotQuery: string | null = null;
type ListLoad = { deleted: Set<number>; changed: boolean };
let listLoad: ListLoad | null = null;
const processingJobs = ref<JobRow[]>([]);
const processingTotal = ref(0);
const processingLoading = ref(false);
const processingError = ref<JobError | null>(null);
const PROCESSING_FILTERS: JobFilters = {
  status: processingJobStatuses.join(','),
  limit: 999,
  offset: 0,
  sort_by: 'id',
  sort_dir: 'asc',
};
let processingLoaded = false;
let processingLoad: ListLoad | null = null;
let eventsBound = false;
const bindEvents = (): void => {
  if (eventsBound) return;
  eventsBound = true;
  const events = useEvents();
  const markPendingLoadsChanged = (): void => {
    if (listLoad) listLoad.changed = true;
    if (processingLoad) processingLoad.changed = true;
  };
  events.onReconnect(markPendingLoadsChanged);
  events.on((event: ServerEvent) => {
    if (event.type === 'events.resync') {
      markPendingLoadsChanged();
    } else if (event.type === 'job.deleted') {
      processingLoad?.deleted.add(event.payload.job_id);
      drop(event.payload.job_id);
      updateProcessing(event.payload.job_id, null);
    } else if (event.type === 'job.progress') {
      patch(event.payload.job_id, {
        progress: event.payload.progress,
        updated_at: event.payload.updated_at,
      });
      updateProcessingProgress(
        event.payload.job_id,
        event.payload.progress,
        event.payload.updated_at,
      );
    } else if (event.type === 'job.status') {
      if (!('job' in event.payload)) {
        const id = event.payload.job_id;
        const row =
          items.value.find((job) => job.id === id) ??
          processingJobs.value.find((job) => job.id === id);
        if (row) {
          const queued = { ...row };
          applyQueuedJob(queued, event.payload);
          upsert(queued);
          updateProcessing(id, queued);
        } else markPendingLoadsChanged();
        return;
      }
      const { job } = event.payload;
      upsert({
        ...job,
        status: event.payload.status,
        priority: event.payload.priority,
        error: event.payload.error,
        warning: event.payload.warning,
        finished_at: event.payload.finished_at,
      });
      updateProcessing(job.id, job);
    }
  });
};
const load = async (filters: JobFilters): Promise<boolean> => {
  activeFilters = { ...filters };
  const requestId = ++latestRequest;
  const pending: ListLoad = { deleted: new Set(), changed: false };
  listLoad = pending;
  const params = new URLSearchParams({
    status: filters.status,
    limit: String(filters.limit),
    offset: String(filters.offset),
  });
  params.set('sort_by', filters.sort_by ?? 'created_at');
  params.set('sort_dir', filters.sort_dir ?? 'desc');
  if (filters.query) params.set('query', filters.query);
  if (filters.kind) params.set('kind', filters.kind);
  const query = params.toString();
  const preserve = snapshotQuery === query && items.value.length > 0;
  if (snapshotQuery !== query) {
    snapshotQuery = null;
    items.value = [];
    total.value = 0;
  }
  isLoading.value = true;
  lastError.value = null;
  try {
    const response = await request(`/api/jobs?${params}`);
    if (!response.ok) throw await actionError(response);
    const body = (await response.json()) as {
      items: JobRow[];
      pagination: { limit: number; offset: number; total: number };
    };
    for (const row of body.items) {
      if (row.status === 'pending')
        applyQueuedJob(row, {
          status: row.status,
          updated_at: row.updated_at,
        });
    }
    if (requestId === latestRequest) {
      if (pending.changed && preserve) return false;
      const rows = body.items.filter(
        (row) => !pending.deleted.has(row.id) && matches(row, filters),
      );
      items.value = sortRows(rows, filters).slice(0, filters.limit);
      total.value = body.pagination.total;
      snapshotQuery = query;
      return true;
    }
  } catch (error) {
    if (requestId === latestRequest) {
      lastError.value = error as JobError;
    }
  } finally {
    if (requestId === latestRequest) {
      isLoading.value = false;
      listLoad = null;
    }
  }
  return false;
};

const loadProcessing = async (): Promise<void> => {
  if (processingLoading.value) return;
  processingLoading.value = true;
  processingLoaded = true;
  processingError.value = null;
  const pending: ListLoad = { deleted: new Set(), changed: false };
  const preserve = processingJobs.value.length > 0;
  processingLoad = pending;
  try {
    const response = await request(
      `/api/jobs?status=${PROCESSING_FILTERS.status}&limit=${PROCESSING_FILTERS.limit}&sort_by=${PROCESSING_FILTERS.sort_by}&sort_dir=${PROCESSING_FILTERS.sort_dir}`,
    );
    if (!response.ok) throw await actionError(response);
    const body = (await response.json()) as {
      items: JobRow[];
      pagination: { total: number };
    };
    if (pending.changed && (preserve || processingJobs.value.length > 0)) return;
    const rows = body.items.filter(
      (row) => !pending.deleted.has(row.id) && matches(row, PROCESSING_FILTERS),
    );
    processingJobs.value = sortRows(rows, PROCESSING_FILTERS).slice(0, PROCESSING_FILTERS.limit);
    processingTotal.value = body.pagination.total;
  } catch (error) {
    processingError.value = error as JobError;
  } finally {
    processingLoad = null;
    processingLoading.value = false;
  }
};

const updateProcessing = (id: number, job: JobRow | null): void => {
  if (!processingLoaded || (job && processingLoad?.deleted.has(id))) return;
  if (processingLoad && job) processingLoad.changed = true;
  const current = processingJobs.value.find((row) => row.id === id);
  const merged = job
    ? mergeJobState(current ?? items.value.find((row) => row.id === id), job)
    : null;
  if (job && !merged) {
    return;
  }
  const next = processingJobs.value.filter((row) => row.id !== id);
  if (merged && matches(merged, PROCESSING_FILTERS)) {
    if (!current) {
      if (processingTotal.value === processingJobs.value.length) processingTotal.value++;
    }
    next.push(merged);
  } else {
    if (current) {
      processingTotal.value = Math.max(0, processingTotal.value - 1);
    }
  }
  processingJobs.value = sortRows(next, PROCESSING_FILTERS).slice(0, PROCESSING_FILTERS.limit);
};

const updateProcessingProgress = (
  id: number,
  progress: JobRow['progress'],
  updated_at: string,
): void => {
  const row = processingJobs.value.find((item) => item.id === id);
  if (row && applyJobProgress(row, { progress, updated_at })) {
    processingJobs.value = sortRows(processingJobs.value, PROCESSING_FILTERS);
  }
  if (processingLoad) processingLoad.changed = true;
};

const upsert = (job: JobRow): void => {
  if (listLoad?.deleted.has(job.id)) return;
  if (listLoad) listLoad.changed = true;
  const index = items.value.findIndex((item) => item.id === job.id);
  const merged = mergeJobState(
    items.value[index] ?? processingJobs.value.find((row) => row.id === job.id),
    job,
  );
  if (!merged) {
    return;
  }
  job = merged;
  if (!activeFilters) return;
  if (!matches(job, activeFilters)) {
    if (index >= 0) {
      items.value.splice(index, 1);
      total.value = Math.max(0, total.value - 1);
    }
    return;
  }
  if (activeFilters.query && index >= 0 && items.value[index]?.file?.path !== job.file?.path) {
    items.value = items.value.filter((item) => item.id !== job.id);
    return;
  }
  if (activeFilters.query && index < 0) {
    return;
  }
  if (index >= 0) {
    items.value[index] = job;
    items.value = sortRows(items.value, activeFilters);
  } else {
    const complete = activeFilters.offset === 0 && total.value === items.value.length;
    if (complete) total.value++;
    if (activeFilters.offset === 0) {
      items.value.push(job);
      items.value = sortRows(items.value, activeFilters).slice(0, activeFilters.limit);
    }
  }
};
const patch = (id: number, changes: Partial<JobRow>): void => {
  if (listLoad?.deleted.has(id)) return;
  if (listLoad) listLoad.changed = true;
  const row = items.value.find((item) => item.id === id);
  if (!('progress' in changes) || changes.updated_at === undefined) {
    if (row) upsert({ ...row, ...changes });
    return;
  }
  const update = {
    progress: changes.progress,
    updated_at: changes.updated_at,
  };
  if (row) {
    if (!applyJobProgress(row, update)) return;
    if (activeFilters) items.value = sortRows(items.value, activeFilters);
  }
};
const drop = (id: number): void => {
  listLoad?.deleted.add(id);
  const before = items.value.length;
  items.value = items.value.filter((job) => job.id !== id);
  if (before !== items.value.length) total.value = Math.max(0, total.value - 1);
};

const statusSet = (status: string): Set<JobStatus> => new Set(status.split(',') as JobStatus[]);
const matches = (job: JobRow, filters: JobFilters): boolean =>
  (!filters.status || statusSet(filters.status).has(job.status)) &&
  (!filters.kind || job.kind === filters.kind);
const sortRows = (rows: JobRow[], filters: JobFilters): JobRow[] => {
  const key = filters.sort_by ?? 'created_at';
  const dir = filters.sort_dir === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const av = key === 'path' ? a.file?.path : a[key];
    const bv = key === 'path' ? b.file?.path : b[key];
    return compareRows(
      a,
      b,
      av as string | number | null | undefined,
      bv as string | number | null | undefined,
      dir as 1 | -1,
    );
  });
};

const actionError = async (response: Response): Promise<JobError> => {
  let detail: string | null = null;
  try {
    detail = await parse_api_error(await response.json());
  } catch {}
  return {
    status: response.status,
    ...(detail ? { detail } : {}),
  };
};

const get = async (id: number, events = false): Promise<JobDetail> => {
  const response = await request(`/api/jobs/${id}${events ? '?events=1' : ''}`);
  if (!response.ok) throw await actionError(response);
  const detail = (await response.json()) as JobDetail;
  if (detail.status === 'pending')
    applyQueuedJob(detail, {
      status: detail.status,
      updated_at: detail.updated_at,
    });
  return detail;
};

const attemptLog = async (
  jobId: number,
  attemptId: number,
  complete: boolean,
): Promise<JobLogChunk> => {
  const response = await request(`/api/jobs/${jobId}/attempts/${attemptId}/log`, {
    headers: complete ? {} : { Range: 'bytes=-262144' },
  });
  if (response.status === 404) return { text: '', complete: false, missing: true };
  if (!response.ok) throw await actionError(response);
  const range = response.headers.get('Content-Range');
  return {
    text: await response.text(),
    complete: response.status === 200 && !range,
    missing: false,
  };
};

const downloadLog = async (jobId: number, attemptId: number): Promise<Blob> => {
  const response = await request(`/api/jobs/${jobId}/attempts/${attemptId}/log`, {
    headers: { Accept: 'text/plain' },
  });
  if (!response.ok) throw await actionError(response);
  return response.blob();
};

function runAction(id: number, action: 'retry'): Promise<JobRetryResult>;
function runAction(id: number, action: JobAction): Promise<JobActionResult>;
async function runAction(id: number, action: JobAction): Promise<JobActionResult> {
  try {
    const response = await request(`/api/jobs/${id}/${action}`, {
      method: 'POST',
      ...(action === 'bump' ? { body: JSON.stringify({}) } : {}),
    });
    if (!response.ok) throw await actionError(response);
    const result: JobActionResult = {
      ok: true,
      ...(action === 'retry' ? { accepted: (await response.json()) as RetryAccepted } : {}),
    };
    if (action !== 'retry' && activeFilters) await load(activeFilters);
    return result;
  } catch (error) {
    return { ok: false, error: error as JobError };
  }
}

const remove = async (id: number): Promise<JobActionResult> => {
  try {
    const response = await request(`/api/jobs/${id}`, { method: 'DELETE' });
    if (!response.ok) throw await actionError(response);
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error as JobError };
  }
};

function runBulk(
  action: 'retry' | 'delete',
  ids: number[],
): Promise<BulkRequestResult | { error: JobError }>;
function runBulk(
  action: Exclude<BulkJobAction, 'retry' | 'delete'>,
  ids: number[],
): Promise<BulkJobResult | { error: JobError }>;
function runBulk(
  action: BulkJobAction,
  ids: number[],
): Promise<BulkJobResult | BulkRequestResult | { error: JobError }>;
async function runBulk(
  action: BulkJobAction,
  ids: number[],
): Promise<BulkJobResult | BulkRequestResult | { error: JobError }> {
  try {
    const response = await request('/api/jobs/bulk', {
      method: 'POST',
      body: JSON.stringify({
        action,
        job_ids: ids,
      }),
    });
    if (!response.ok) throw await actionError(response);
    if (action === 'retry' || action === 'delete')
      return (await response.json()) as BulkRequestResult;
    const result = (await response.json()) as BulkJobResult;
    if (result.succeeded.length && activeFilters) await load(activeFilters);
    return result;
  } catch (error) {
    return { error: error as JobError };
  }
}

export const useJobs = () => {
  if (import.meta.client) bindEvents();
  return {
    items: shallowReadonly(items),
    total: shallowReadonly(total),
    isLoading: shallowReadonly(isLoading),
    lastError: shallowReadonly(lastError),
    load,
    processingJobs: shallowReadonly(processingJobs),
    processingTotal: shallowReadonly(processingTotal),
    processingLoading: shallowReadonly(processingLoading),
    processingError: shallowReadonly(processingError),
    loadProcessing,
    upsert,
    patch,
    drop,
    runAction,
    remove,
    runBulk,
    get,
    attemptLog,
    downloadLog,
  };
};
