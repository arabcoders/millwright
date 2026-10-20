import {
  ACTIVE_JOB_STATUSES,
  type JobDetail,
  type JobRow,
  type MediaFileDetail,
  type MediaFileRow,
} from '~/types/api';
import type { JobStatusUpdate } from './useEvents';

export const compareRows = <T extends { id: number }>(
  a: T,
  b: T,
  av: string | number | null | undefined,
  bv: string | number | null | undefined,
  dir: 1 | -1,
): number => {
  if (av == null && bv == null) return (a.id - b.id) * dir;
  if (av == null) return 1;
  if (bv == null) return -1;
  const an =
    typeof av === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/.test(av)
      ? av.replace(
          /(?:\.(\d+))?Z$/,
          (_, fraction: string | undefined) => `.${(fraction ?? '').padEnd(6, '0')}Z`,
        )
      : av;
  const bn =
    typeof bv === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/.test(bv)
      ? bv.replace(
          /(?:\.(\d+))?Z$/,
          (_, fraction: string | undefined) => `.${(fraction ?? '').padEnd(6, '0')}Z`,
        )
      : bv;
  return ((an < bn ? -1 : an > bn ? 1 : a.id - b.id) as number) * dir;
};

export const sortStateRows = <T extends { id: number }>(
  rows: T[],
  value: (row: T) => string | number | null | undefined,
  dir: 1 | -1,
): T[] => [...rows].sort((a, b) => compareRows(a, b, value(a), value(b), dir));

type JobState = JobRow | JobDetail;
// Catalogue timestamps do not advance when pending jobs are cancelled.
type FileState = (MediaFileRow | MediaFileDetail) & { jobStatusUpdatedAt?: string };

export const retainFileJobStatus = (file: FileState, source: FileState | JobRow): void => {
  const time = 'status' in source ? source.updated_at : source.jobStatusUpdatedAt;
  if (time && (!file.jobStatusUpdatedAt || newerJobTime(time, file.jobStatusUpdatedAt)))
    file.jobStatusUpdatedAt = time;
};

export const applyQueuedJob = (
  job: JobState,
  update: Pick<JobStatusUpdate, 'status' | 'updated_at'>,
): void => {
  if (newerJobTime(job.updated_at, update.updated_at)) return;
  Object.assign(job, {
    status: update.status,
    updated_at: update.updated_at,
    progress: null,
    started_at: null,
    finished_at: null,
    error: null,
    warning: null,
    node_id: null,
    node_name: null,
    hold_until: null,
    queue_state: undefined,
    queue_reason: undefined,
  });
};
export const applyQueuedFileJob = (file: FileState, update: JobStatusUpdate): boolean => {
  if (file.id !== update.media_file_id) return false;
  if (file.jobStatusUpdatedAt && !newerJobTime(update.updated_at, file.jobStatusUpdatedAt))
    return false;
  if (
    file.active_job_updated_at !== null &&
    newerJobTime(file.active_job_updated_at, update.updated_at)
  )
    return false;
  const job = 'jobs' in file ? file.jobs.find((job) => job.id === update.job_id) : undefined;
  if (job) {
    job.status = update.status;
    job.error = null;
  }
  file.active_job_id = update.job_id;
  file.active_job_kind = update.kind;
  file.active_job_status = update.status;
  file.active_job_progress = null;
  file.active_job_updated_at = update.updated_at;
  if (file.active_job_kind === 'transcode') file.transcode_status = update.status;
  return true;
};
type JobProgress = Pick<JobRow, 'progress' | 'updated_at'>;
const latestAttempt = (job: JobState) =>
  'latest_attempt' in job ? job.latest_attempt : job.attempts.at(-1);
const attemptCount = (job: JobState): number =>
  'latest_attempt' in job ? job.attempts : job.attempt_count;
const normalizeTime = (value: string): string =>
  value.replace(
    /(?:\.(\d+))?Z$/,
    (_, fraction: string | undefined) => `.${(fraction ?? '').padEnd(6, '0')}Z`,
  );
export const newerJobTime = (incoming: string, current: string): boolean =>
  normalizeTime(incoming) > normalizeTime(current);

export type FileJobProgress = {
  job_id: number;
  progress: Record<string, unknown> | null;
  updated_at: string;
};

export const applyFileProgress = (file: MediaFileRow, update: FileJobProgress): boolean => {
  if (
    file.active_job_id !== update.job_id ||
    !ACTIVE_JOB_STATUSES.some((status) => status === file.active_job_status) ||
    file.active_job_updated_at === null ||
    !newerJobTime(update.updated_at, file.active_job_updated_at)
  )
    return false;
  file.active_job_progress = update.progress;
  file.active_job_updated_at = update.updated_at;
  return true;
};

export const applyJobProgress = (current: JobState, update: JobProgress | undefined): boolean => {
  if (
    !update ||
    !ACTIVE_JOB_STATUSES.includes(current.status) ||
    !newerJobTime(update.updated_at, current.updated_at)
  )
    return false;
  const attempt = latestAttempt(current);
  if (attempt && !ACTIVE_JOB_STATUSES.some((status) => status === attempt.status)) return false;
  const id = update.progress?.['attempt_id'];
  if (typeof id === 'number' && id !== attempt?.id) return false;
  current.progress = update.progress;
  current.updated_at = update.updated_at;
  return true;
};

export const mergeJobState = <T extends JobState>(
  current: JobState | undefined,
  incoming: T,
): T | null => {
  const sameAttempt =
    current &&
    attemptCount(current) === attemptCount(incoming) &&
    latestAttempt(current)?.id === latestAttempt(incoming)?.id;
  if (current && attemptCount(incoming) < attemptCount(current)) return null;
  if (current && sameAttempt) {
    const active = ACTIVE_JOB_STATUSES.includes(current.status);
    const nextActive = ACTIVE_JOB_STATUSES.includes(incoming.status);
    // A terminal status takes precedence over the last progress timestamp.
    if ((!active || nextActive) && newerJobTime(current.updated_at, incoming.updated_at))
      return null;
    if (!active && nextActive && !newerJobTime(incoming.updated_at, current.updated_at))
      return null;
  }
  const merged = { ...incoming };
  if (merged.status === 'pending')
    applyQueuedJob(merged, { status: merged.status, updated_at: merged.updated_at });
  return merged;
};

export const applyJobDetail = (current: JobDetail, incoming: JobRow): boolean => {
  const row = mergeJobState(current, incoming);
  if (!row) return false;
  const { attempts, latest_attempt: latest, ...fields } = row;
  Object.assign(current, fields, {
    attempt_count: attempts,
    file: row.file ? { ...current.file, ...row.file } : current.file,
  });
  if (latest) {
    const index = current.attempts.findIndex((attempt) => attempt.id === latest.id);
    const saved = current.attempts[index];
    if (saved)
      current.attempts[index] = {
        ...saved,
        ...latest,
        input_probe: saved.input_probe ?? latest.input_probe,
        output_probe: saved.output_probe ?? latest.output_probe,
      };
    else current.attempts = [...current.attempts, { ...latest, log_data: null }];
  }
  return true;
};
