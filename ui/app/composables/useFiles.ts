import { ref, shallowReadonly } from 'vue';
import { parse_api_error, request } from '~/utils';
import { statusInfo } from '~/utils/status';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import type {
  BulkFileAction,
  BulkFileResult,
  FileJobAccepted,
  LibraryRow,
  MediaFileDetail,
  MediaFileRow,
} from '~/types/api';
import { useLibraries } from './useLibraries';
import { useDialog } from './useDialog';
import { useNotification } from './useNotification';
import { useEvents, type ServerEvent } from './useEvents';
import { useTranscodeFlow } from './useTranscodeFlow';
import {
  applyFileProgress,
  applyQueuedFileJob,
  newerJobTime,
  retainFileJobStatus,
  sortStateRows,
  type FileJobProgress,
} from './stateHelpers';

export type FileFilters = {
  query?: string;
  library_id?: string;
  transcode_status?: string;
  health_status?: string;
  limit: number;
  offset: number;
  sort_by?:
    | 'id'
    | 'path'
    | 'size'
    | 'duration_s'
    | 'bit_rate'
    | 'transcode_status'
    | 'health_status'
    | 'created_at'
    | 'updated_at';
  sort_dir?: 'asc' | 'desc';
};

const items = ref<MediaFileRow[]>([]);
const recentItems = ref<MediaFileRow[]>([]);
const recentLoading = ref(false);
const recentError = ref<Error | null>(null);
const total = ref(0);
const isLoading = ref(false);
const lastError = ref<Error | null>(null);
const actionBusy = ref(false);
const refreshingMetadata = ref<number | null>(null);
let requestId = 0;
let snapshotQuery: string | null = null;
let lastFilters: FileFilters = { limit: 50, offset: 0 };
let journal: {
  request: number;
  rows: Map<number, MediaFileRow>;
  drops: Set<number>;
  progress: Map<number, FileJobProgress>;
} | null = null;
let hasLoaded = false;
let recentHasLoaded = false;
let recentRequestId = 0;
let recentJournal: {
  rows: Map<number, MediaFileRow>;
  drops: Set<number>;
  progress: Map<number, FileJobProgress>;
} | null = null;
let eventsBound = false;
const matches = (file: MediaFileRow, filters: FileFilters): boolean =>
  (!filters.library_id || String(file.library_id) === filters.library_id) &&
  (!filters.transcode_status ||
    filters.transcode_status.split(',').includes(file.transcode_status)) &&
  (!filters.health_status || filters.health_status.split(',').includes(file.health_status));
const sortRows = (rows: MediaFileRow[], filters: FileFilters): MediaFileRow[] => {
  const key = filters.sort_by ?? 'id';
  const dir = filters.sort_dir === 'asc' ? 1 : -1;
  return sortStateRows(rows, (row) => row[key], dir as 1 | -1);
};
const bindEvents = (): void => {
  if (eventsBound) return;
  eventsBound = true;
  const events = useEvents();
  events.on((event: ServerEvent) => {
    if (event.type === 'file.status') {
      const payload = event.payload;
      if (payload.file) {
        const row = payload.file;
        const previous =
          recentItems.value.find((file) => file.id === row.id) ?? recentJournal?.rows.get(row.id);
        if (previous) retainFileJobStatus(row, previous);
        if (previous && previous.active_job_id !== null && previous.active_job_updated_at !== null)
          applyFileProgress(row, {
            job_id: previous.active_job_id,
            progress: previous.active_job_progress,
            updated_at: previous.active_job_updated_at,
          });
        const rows = recentJournal?.rows;
        rows?.set(row.id, row);
        recentJournal?.drops.delete(row.id);
        if (recentHasLoaded) {
          const merged = recentItems.value.filter((item) => item.id !== row.id);
          merged.push(row);
          recentItems.value = sortStateRows(merged, (item) => item.created_at, -1).slice(0, 6);
        }
      } else if (
        payload.media_file_id !== undefined &&
        (payload.action === 'deleted' || payload.action === 'pruned')
      ) {
        const id = payload.media_file_id;
        drop(id);
        recentJournal?.rows.delete(id);
        recentJournal?.drops.add(id);
        recentItems.value = recentItems.value.filter((file) => file.id !== id);
      }
      if (payload.file) upsert(payload.file);
    } else if (event.type === 'job.status') {
      for (const row of [...items.value, ...(journal?.rows.values() ?? [])]) {
        const updated = { ...row };
        if ('job' in event.payload) {
          if (row.id !== event.payload.job.media_file_id) continue;
          retainFileJobStatus(updated, event.payload.job);
        } else if (!applyQueuedFileJob(updated, event.payload)) continue;
        upsert(updated);
      }
      for (const row of [...recentItems.value, ...(recentJournal?.rows.values() ?? [])]) {
        if ('job' in event.payload) {
          if (row.id !== event.payload.job.media_file_id) continue;
          retainFileJobStatus(row, event.payload.job);
        } else if (!applyQueuedFileJob(row, event.payload)) continue;
        recentJournal?.rows.set(row.id, { ...row });
      }
    } else if (event.type === 'job.progress') {
      const update = event.payload;
      if (journal) {
        const previous = journal.progress.get(update.job_id);
        if (!previous || newerJobTime(update.updated_at, previous.updated_at))
          journal.progress.set(update.job_id, update);
      }
      if (recentJournal) {
        const previous = recentJournal.progress.get(update.job_id);
        if (!previous || newerJobTime(update.updated_at, previous.updated_at))
          recentJournal.progress.set(update.job_id, update);
      }
      for (const file of items.value) applyFileProgress(file, update);
      for (const file of recentItems.value) applyFileProgress(file, update);
    }
  });
};

const loadRecent = async (): Promise<void> => {
  recentHasLoaded = true;
  const currentRequest = ++recentRequestId;
  const journal = {
    rows: new Map<number, MediaFileRow>(),
    drops: new Set<number>(),
    progress: new Map<number, FileJobProgress>(),
  };
  recentJournal = journal;
  recentLoading.value = true;
  recentError.value = null;
  try {
    const response = await request('/api/files?limit=6&offset=0&sort_by=created_at&sort_dir=desc');
    if (!response.ok) throw new Error(`Could not load recent files (HTTP ${response.status}).`);
    const body = (await response.json()) as { items: MediaFileRow[] };
    if (currentRequest === recentRequestId) {
      for (const row of body.items) {
        const previous = recentItems.value.find((file) => file.id === row.id);
        if (previous) retainFileJobStatus(row, previous);
      }
      const rows = body.items.filter((file) => !journal.drops.has(file.id));
      for (const file of journal.rows.values()) {
        const index = rows.findIndex((item) => item.id === file.id);
        if (index >= 0) rows[index] = file;
        else rows.push(file);
      }
      for (const file of rows) {
        if (file.active_job_id === null) continue;
        const update = journal.progress.get(file.active_job_id);
        if (update) applyFileProgress(file, update);
      }
      recentItems.value = sortStateRows(rows, (file) => file.created_at, -1).slice(0, 6);
      if (recentJournal === journal) recentJournal = null;
    }
  } catch (error) {
    if (currentRequest === recentRequestId) {
      recentError.value = error instanceof Error ? error : new Error(String(error));
      if (recentJournal === journal) recentJournal = null;
    }
    throw error;
  } finally {
    if (currentRequest === recentRequestId) recentLoading.value = false;
  }
};

const load = async (filters: FileFilters): Promise<void> => {
  lastFilters = { ...filters };
  hasLoaded = true;
  const currentRequest = ++requestId;
  const requestJournal = {
    request: currentRequest,
    rows: new Map<number, MediaFileRow>(),
    drops: new Set<number>(),
    progress: new Map<number, FileJobProgress>(),
  };
  journal = requestJournal;
  const params = new URLSearchParams({
    limit: String(filters.limit),
    offset: String(filters.offset),
  });
  params.set('sort_by', filters.sort_by ?? 'id');
  params.set('sort_dir', filters.sort_dir ?? 'desc');
  for (const key of ['query', 'library_id', 'transcode_status', 'health_status'] as const) {
    if (filters[key]) params.set(key, filters[key]);
  }
  const query = params.toString();
  if (snapshotQuery !== query) {
    snapshotQuery = null;
    items.value = [];
    total.value = 0;
  }
  isLoading.value = true;
  lastError.value = null;
  try {
    const response = await request(`/api/files?${params}`);
    if (!response.ok) throw new Error(`Could not load files (HTTP ${response.status}).`);
    const body = (await response.json()) as {
      items: MediaFileRow[];
      pagination: { limit: number; offset: number; total: number };
    };
    if (currentRequest === requestId) {
      for (const row of body.items) {
        const previous = items.value.find((file) => file.id === row.id);
        if (previous) retainFileJobStatus(row, previous);
      }
      const rows = body.items.filter((file) => !requestJournal.drops.has(file.id));
      for (const file of requestJournal.rows.values()) {
        const index = rows.findIndex((item) => item.id === file.id);
        if (index >= 0) {
          if (filters.query && rows[index]?.path !== file.path) rows.splice(index, 1);
          else rows[index] = file;
        } else if (!filters.query && matches(file, filters) && filters.offset === 0)
          rows.push(file);
      }
      for (const file of rows) {
        if (file.active_job_id === null) continue;
        const update = requestJournal.progress.get(file.active_job_id);
        if (update) applyFileProgress(file, update);
      }
      items.value = sortRows(
        rows.filter((file) => matches(file, filters)),
        filters,
      ).slice(0, filters.limit);
      const removedFromPage = body.items.reduce(
        (count, file) => count + (requestJournal.drops.has(file.id) ? 1 : 0),
        0,
      );
      total.value = Math.max(0, body.pagination.total - removedFromPage);
      snapshotQuery = query;
      if (journal === requestJournal) journal = null;
    }
  } catch (error) {
    if (currentRequest === requestId) {
      if (journal === requestJournal) journal = null;
      lastError.value = error instanceof Error ? error : new Error(String(error));
    }
    throw error;
  } finally {
    if (currentRequest === requestId) {
      isLoading.value = false;
    }
  }
};
const upsert = (file: MediaFileRow): void => {
  if (!hasLoaded) return;
  const index = items.value.findIndex((item) => item.id === file.id);
  const previous = items.value[index] ?? journal?.rows.get(file.id);
  if (previous) retainFileJobStatus(file, previous);
  if (previous && previous.active_job_id !== null && previous.active_job_updated_at !== null) {
    applyFileProgress(file, {
      job_id: previous.active_job_id,
      progress: previous.active_job_progress,
      updated_at: previous.active_job_updated_at,
    });
  }
  if (journal && journal.request === requestId) {
    journal.rows.set(file.id, file);
    journal.drops.delete(file.id);
  }
  if (index >= 0) {
    const previous = items.value[index]!;
    if (!matches(file, lastFilters)) {
      drop(file.id);
      return;
    }
    if (lastFilters.query && previous.path !== file.path) {
      items.value = items.value.filter((item) => item.id !== file.id);
      return;
    }
    items.value[index] = file;
    items.value = sortRows(items.value, lastFilters);
    return;
  }
  if (matches(file, lastFilters)) {
    const complete =
      !lastFilters.query && lastFilters.offset === 0 && total.value === items.value.length;
    if (complete) total.value++;
    if (!lastFilters.query && lastFilters.offset === 0) {
      const sorted = sortRows([...items.value, file], lastFilters);
      if (sorted.indexOf(file) < lastFilters.limit)
        items.value = sorted.slice(0, lastFilters.limit);
    }
  }
};
const patch = (id: number, changes: Partial<MediaFileRow>): void => {
  const row = items.value.find((item) => item.id === id);
  if (row) {
    upsert({ ...row, ...changes });
  }
};
const drop = (id: number): void => {
  if (journal && journal.request === requestId) {
    journal.drops.add(id);
    journal.rows.delete(id);
  }
  const before = items.value.length;
  items.value = items.value.filter((file) => file.id !== id);
  if (before !== items.value.length) {
    total.value = Math.max(0, total.value - 1);
  }
};

type SelectedFile = Pick<MediaFileRow, 'id' | 'library_id' | 'path' | 'active_job_id'>;

export const canSkipCooldown = (
  file: Pick<
    MediaFileRow,
    'cooldown_until' | 'active_job_id' | 'active_job_status' | 'transcode_status' | 'health_status'
  >,
  now = Date.now(),
): boolean =>
  file.cooldown_until !== null &&
  Date.parse(file.cooldown_until) > now &&
  (file.active_job_id !== null
    ? file.active_job_status === 'hold'
    : file.transcode_status === 'pending' || file.health_status === 'pending');

export const notifyJobRequestResult = (
  result: Extract<ServerEvent, { type: 'job.request_result' }>['payload'],
): void => {
  const notify = useNotification();
  const kind = statusInfo(result.kind, 'kind').label;
  const reason = result.reason || result.decision?.reason;
  const detail = reason ? `: ${reason}` : '.';
  const subject = `file #${result.file_id} (job #${result.job_id})`;
  switch (result.outcome) {
    case 'queued':
      return;
    case 'not_queued':
      notify.info(`${kind} not queued for ${subject}${detail}`);
      return;
    case 'failed':
      notify.error(`${kind} request failed for ${subject}${detail}`);
      return;
    case 'cancelled':
      notify.info(`${kind} request cancelled for ${subject}${detail}`);
  }
};

export const refreshMetadataPrerequisite = (
  file: SelectedFile,
  libraries: readonly LibraryRow[],
  scannerPaused: boolean,
): string | null => {
  if (file.active_job_id !== null) return 'File already has an active job';
  const library = libraries.find((library) => library.id === file.library_id);
  if (!library) return 'Library settings unavailable';
  if (library.suspended) return 'Resume the library before refreshing metadata';
  if (scannerPaused) return 'Resume activity from the header before refreshing metadata';
  return null;
};

export const transcodePrerequisite = (
  file: SelectedFile,
  libraries: readonly LibraryRow[],
): string | null => {
  const library = libraries.find((library) => library.id === file.library_id);
  if (!library) return 'Library settings unavailable';
  if (library.suspended) return 'Library is paused';
  if (library.flow_id == null) return 'Library has no flow';
  if (file.active_job_id !== null) return 'File already has an active job';
  return null;
};

export const manualTranscodePrerequisite = (
  file: SelectedFile,
  libraries: readonly LibraryRow[],
): string | null => {
  if (file.active_job_id !== null) return 'File already has an active job';
  const library = libraries.find((library) => library.id === file.library_id);
  if (!library) return 'Library settings unavailable';
  if (library.suspended) return 'Library is paused';
  return null;
};

export const transcodeWarnings = (
  files: SelectedFile[],
  libraries: readonly LibraryRow[],
): string[] => {
  const counts = new Map<string, number>();
  for (const file of files) {
    const reason = transcodePrerequisite(file, libraries);
    if (reason) counts.set(reason, (counts.get(reason) ?? 0) + 1);
  }
  return [...counts].map(([reason, count]) => {
    const subject = `${count} file${count === 1 ? '' : 's'}`;
    if (reason === 'Library has no flow')
      return `${subject} can't transcode because ${count === 1 ? 'its' : 'their'} library has no assigned flow.`;
    if (reason === 'Library is paused')
      return `${subject} can't transcode because ${count === 1 ? 'its' : 'their'} library is paused.`;
    if (reason === 'File already has an active job')
      return `${subject} ${count === 1 ? 'already has an active job' : 'already have active jobs'}.`;
    return `Library settings are unavailable for ${subject}.`;
  });
};

const currentLibraries = async (files: SelectedFile[]): Promise<readonly LibraryRow[]> => {
  const libraries = useLibraries();
  await libraries.reload();
  if (
    libraries.lastError.value ||
    files.some(
      (file) =>
        !libraries.items.value.some(
          (library) =>
            library.id === file.library_id && typeof library.healthcheck_enabled === 'boolean',
        ),
    )
  )
    throw new Error('Could not check the current library settings. Refresh and try again.');
  return libraries.items.value;
};

const healthWarning = (files: SelectedFile[], libraries: readonly LibraryRow[]): string => {
  const affected = libraries.filter(
    (library) =>
      !library.healthcheck_enabled && files.some((file) => file.library_id === library.id),
  );
  if (!affected.length) return '';
  const names = affected
    .map((library) => {
      const count = files.filter((file) => file.library_id === library.id).length;
      return `${library.name} (${count} file${count === 1 ? '' : 's'})`;
    })
    .join(', ');
  return `Automatic health checks are disabled for ${names}.`;
};

const queueHealthCheck = async (file: SelectedFile): Promise<boolean> => {
  if (actionBusy.value) return false;
  actionBusy.value = true;
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  const { confirmDialog } = useDialog();
  try {
    const warning = healthWarning([file], await currentLibraries([file]));
    if (
      warning &&
      !(
        await confirmDialog({
          title: 'Queue manual health check?',
          message: warning,
          confirmText: 'Run',
          confirmColor: 'success',
        })
      ).status
    )
      return false;
    const response = await request(`/api/files/${file.id}/healthcheck`, {
      method: 'POST',
    });
    if (!response.ok) {
      notify.error(messageFor(await parse_api_error(await response.json().catch(() => null))));
      return false;
    }
    const accepted = (await response.json()) as FileJobAccepted;
    notify.success(
      `Health check ${accepted.status === 'queued' ? 'queued' : 'requested'} (job #${accepted.job_id}).`,
    );
    return true;
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
    return false;
  } finally {
    actionBusy.value = false;
  }
};

const refreshMetadata = async (file: Pick<MediaFileRow, 'id'>): Promise<boolean> => {
  if (actionBusy.value) return false;
  actionBusy.value = true;
  refreshingMetadata.value = file.id;
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  try {
    const response = await request(`/api/files/${file.id}/probe`, {
      method: 'POST',
    });
    if (!response.ok) {
      notify.error(messageFor(await parse_api_error(await response.json().catch(() => null))));
      return false;
    }
    const accepted = (await response.json()) as FileJobAccepted;
    notify.success(`Metadata refresh queued (job #${accepted.job_id}).`);
    return true;
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
    return false;
  } finally {
    refreshingMetadata.value = null;
    actionBusy.value = false;
  }
};

const skipCooldown = async (file: Pick<MediaFileRow, 'id'>): Promise<boolean> => {
  if (actionBusy.value) return false;
  actionBusy.value = true;
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  try {
    const response = await request(`/api/files/${file.id}/skip-cooldown`, {
      method: 'POST',
    });
    if (!response.ok) {
      notify.error(messageFor(await parse_api_error(await response.json().catch(() => null))));
      return false;
    }
    notify.success(`Cooldown skipped for file #${file.id}.`);
    return true;
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
    return false;
  } finally {
    actionBusy.value = false;
  }
};

const queueBulk = async (
  action: BulkFileAction,
  files: SelectedFile[],
): Promise<BulkFileResult<{ file_id: number }> | null> => {
  if (actionBusy.value || !files.length) return null;
  actionBusy.value = true;
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  const { confirmDialog } = useDialog();
  try {
    const libraries = action === 'skip_cooldown' ? [] : await currentLibraries(files);
    const warning = action === 'healthcheck' ? healthWarning(files, libraries) : '';
    const prerequisites =
      action === 'transcode' ? transcodeWarnings(files, libraries).join(' ') : '';
    const subject = `${files.length} file${files.length === 1 ? '' : 's'}`;
    const label = {
      healthcheck: 'Health check',
      transcode: 'Transcode',
      skip_cooldown: 'Skip cooldown for',
    }[action];
    const confirmed = await confirmDialog({
      title: `${label} ${subject}?`,
      message:
        action === 'healthcheck'
          ? warning || 'Full decoding uses CPU and storage I/O.'
          : action === 'skip_cooldown'
            ? 'Expires their remaining cooldowns. Normal scheduling rules still apply.'
            : `Uses each file's library flow.${prerequisites ? ` ${prerequisites}` : ''}`,
      confirmText: action === 'skip_cooldown' ? 'Skip cooldown' : label,
      confirmColor: 'primary',
    });
    if (!confirmed.status) return null;
    const response = await request('/api/files/bulk', {
      method: 'POST',
      body: JSON.stringify({ action, file_ids: files.map((file) => file.id) }),
    });
    if (!response.ok)
      throw new Error(
        (await parse_api_error(await response.json().catch(() => null))) ??
          'Could not apply the selected file action.',
      );
    const result = (await response.json()) as BulkFileResult<{ file_id: number }>;
    const count = result.accepted.length;
    const kind = action === 'healthcheck' ? 'health check' : 'transcode';
    const summary =
      action === 'skip_cooldown'
        ? `${count} cooldowns skipped, ${result.failed.length} failed.`
        : `${count} ${kind} request${count === 1 ? '' : 's'} accepted, ${result.failed.length} rejected.`;
    const details = result.failed
      .slice(0, 3)
      .map((item) => `#${item.file_id}: ${item.detail}`)
      .join('. ');
    const message = [summary, details].filter(Boolean).join(' ');
    if (result.failed.length) notify.error(message);
    else notify.success(message);
    return result;
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
    return null;
  } finally {
    actionBusy.value = false;
  }
};

const loadDetail = async (fileId: number): Promise<MediaFileDetail> => {
  const response = await request(`/api/files/${fileId}`);
  if (!response.ok) throw new Error(`Could not load file (HTTP ${response.status}).`);
  return (await response.json()) as MediaFileDetail;
};

export const useFiles = () => {
  if (import.meta.client) bindEvents();
  const { pick } = useTranscodeFlow();
  const queueTranscode = async (file: MediaFileRow): Promise<boolean> => {
    if (actionBusy.value) return false;
    actionBusy.value = true;
    const notify = useNotification();
    const { messageFor } = useApiErrorMessage();
    try {
      const reason = manualTranscodePrerequisite(file, useLibraries().items.value);
      if (reason) {
        notify.error(reason);
        return false;
      }
      const flow = await pick(file);
      if (!flow) return false;
      const response = await request(`/api/files/${file.id}/transcode`, {
        method: 'POST',
        body: JSON.stringify({ flow_id: flow.id, expected_version: flow.version }),
      });
      if (!response.ok) {
        notify.error(messageFor(await parse_api_error(await response.json().catch(() => null))));
        return false;
      }
      const accepted = (await response.json()) as FileJobAccepted;
      notify.success(`Transcode requested (job #${accepted.job_id}).`);
      return true;
    } catch (error) {
      notify.error(messageFor(error instanceof Error ? error.message : undefined));
      return false;
    } finally {
      actionBusy.value = false;
    }
  };
  return {
    items: shallowReadonly(items),
    recentItems: shallowReadonly(recentItems),
    recentLoading: shallowReadonly(recentLoading),
    recentError: shallowReadonly(recentError),
    total: shallowReadonly(total),
    isLoading: shallowReadonly(isLoading),
    lastError: shallowReadonly(lastError),
    actionBusy: shallowReadonly(actionBusy),
    refreshingMetadata: shallowReadonly(refreshingMetadata),
    load,
    loadRecent,
    loadDetail,
    upsert,
    patch,
    drop,
    queueHealthCheck,
    refreshMetadata,
    skipCooldown,
    queueTranscode,
    queueBulk,
  };
};
