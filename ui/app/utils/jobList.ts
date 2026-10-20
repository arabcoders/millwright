import { ACTIVE_JOB_STATUSES, type JobRow, type JobStatus } from '../types/api';
import type { BulkJobAction } from '../composables/useJobs';
import type { LocationQueryRaw } from 'vue-router';
import { LIST_PAGE_SIZES } from './listPagination';
import { parseStatuses, statusInfo, statusOptions } from './status';

export const JOB_FILTER_ALL = '__all__';
export const jobStatusOptions = statusOptions('job').slice(1);
export const processingJobStatuses: JobStatus[] = ['claimed', 'running', 'verifying', 'finalizing'];
export const dashboardJobStatusOptions = jobStatusOptions.filter(
  (option) => !processingJobStatuses.some((status) => status === option.value),
);
export const jobKindOptions = [
  { label: 'All job types', value: JOB_FILTER_ALL },
  { label: statusInfo('transcode', 'kind').label, value: 'transcode' },
  { label: statusInfo('healthcheck', 'kind').label, value: 'healthcheck' },
  { label: statusInfo('probe', 'kind').label, value: 'probe' },
  { label: statusInfo('mkv_stats', 'kind').label, value: 'mkv_stats' },
];
export const jobSortOptions = [
  { label: 'Job ID', value: 'id' },
  { label: 'Created', value: 'created_at' },
  { label: 'Updated', value: 'updated_at' },
  { label: 'Priority', value: 'priority' },
  { label: 'Status', value: 'status' },
  { label: 'File', value: 'path' },
];
export const jobQueryKeys = ['status', 'query', 'kind', 'offset', 'limit', 'sort_by', 'sort_dir'];

const queryValue = (value: unknown): string | undefined =>
  typeof value === 'string' ? value : undefined;

export const jobRouteFilters = (
  source: Record<string, unknown>,
  statusesAllowed = jobStatusOptions,
): Record<string, string> => {
  const values: Record<string, string> = {};
  const status = queryValue(source.status);
  const search = queryValue(source.query)?.trim();
  const kind = queryValue(source.kind);
  const offset = queryValue(source.offset);
  const limit = queryValue(source.limit);
  const sort = queryValue(source.sort_by);
  const direction = queryValue(source.sort_dir);
  const statuses = parseStatuses(status, statusesAllowed);
  if (statuses.length) values.status = statuses.join(',');
  if (search) values.query = search;
  if (kind && jobKindOptions.slice(1).some((option) => option.value === kind)) values.kind = kind;
  if (offset && /^\d+$/.test(offset)) {
    const normalized = offset.replace(/^0+(?=\d)/, '');
    if (normalized !== '0') values.offset = normalized;
  }
  if (limit && LIST_PAGE_SIZES.some((size) => String(size) === limit) && limit !== '50')
    values.limit = limit;
  if (sort && jobSortOptions.some((option) => option.value === sort)) values.sort_by = sort;
  if (direction === 'asc' || direction === 'desc') values.sort_dir = direction;
  return values;
};

export const updateJobRoute = (
  source: LocationQueryRaw,
  changes: Record<string, string>,
  statusesAllowed = jobStatusOptions,
): LocationQueryRaw => {
  const next = { ...source, ...changes };
  if (jobQueryKeys.some((key) => key !== 'offset' && key in changes)) delete next.offset;
  return {
    ...Object.fromEntries(Object.entries(next).filter(([key]) => !jobQueryKeys.includes(key))),
    ...jobRouteFilters(next, statusesAllowed),
  };
};

export const canJobAction = (status: JobStatus, action: BulkJobAction): boolean => {
  switch (action) {
    case 'bump':
      return status === 'staging' || status === 'queued';
    case 'cancel':
      return ACTIVE_JOB_STATUSES.includes(status);
    case 'retry':
      return status === 'succeeded' || status === 'failed' || status === 'cancelled';
    case 'delete':
      return !['pending', 'claimed', 'running', 'verifying', 'finalizing'].includes(status);
  }
};

export const formatReason = (reason: string): string => {
  const groups = new Map<string, { line: number; streams: string[] }>();
  const lines: string[] = [];
  const pattern =
    /\b(Drop|Transcode|Retag) (\w+) stream (\d+) \(([^,]+), ([^)]+)\): (.*?)\.(?=\s|$)/g;
  let cursor = 0;
  for (const match of reason.matchAll(pattern)) {
    const [, action, type, index, language, codec, detail] = match;
    const recorded = reason.slice(cursor, match.index).trim();
    if (recorded) lines.push(recorded);
    const verb =
      action === 'Drop' ? 'Dropping' : action === 'Transcode' ? 'Transcoding' : 'Retagging';
    const key = JSON.stringify([action, type, codec, detail]);
    let group = groups.get(key);
    if (!group) {
      group = { line: lines.length, streams: [] };
      groups.set(key, group);
      lines.push('');
    }
    group.streams.push(`${index}:${language}`);
    lines[group.line] =
      group.streams.length === 1
        ? `${verb} ${type} stream (${index}, ${language}, ${codec}): ${detail}.`
        : `${verb} ${type} streams (${group.streams.join(', ')}) [${codec}]: ${detail}.`;
    cursor = match.index + match[0].length;
  }
  const recorded = reason.slice(cursor).trim();
  if (recorded) lines.push(recorded);
  return cursor ? lines.join('\n') : reason;
};

export const jobReason = (job: Pick<JobRow, 'kind' | 'status' | 'decision_log'>): string | null => {
  if (job.kind !== 'transcode' || job.status === 'pending') return null;
  const reasons = job.decision_log
    ?.filter((entry) => entry.outcome === 'plan' && entry.reason)
    .map((entry) => entry.reason);
  return reasons?.join('\n') || null;
};

export const jobActionIds = (
  jobs: ReadonlyArray<Pick<JobRow, 'id' | 'status'>>,
  selected: ReadonlySet<number>,
  action: BulkJobAction,
): number[] =>
  jobs
    .filter((job) => selected.has(job.id) && canJobAction(job.status, action))
    .map((job) => job.id);

export const remainingJobSelection = (
  selected: ReadonlySet<number>,
  succeeded: readonly number[],
): Set<number> => {
  const completed = new Set(succeeded);
  return new Set([...selected].filter((id) => !completed.has(id)));
};
