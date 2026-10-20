import type { JobRow, MediaFileRow } from '../types/api';
import { estimateCompletion } from './date';

export const progressPercent = (
  progress: Record<string, unknown> | null | undefined,
): number | null => {
  const percent = progress?.['percent'];
  return typeof percent === 'number' && Number.isFinite(percent)
    ? Math.min(100, Math.max(0, percent))
    : null;
};

export const estimatedJobCompletion = (
  job: Pick<JobRow, 'kind' | 'status' | 'started_at' | 'updated_at' | 'finished_at' | 'progress'>,
): Date | null => {
  if (job.status !== 'running' || job.finished_at !== null) return null;
  if (
    job.kind !== 'transcode' &&
    !(job.kind === 'healthcheck' && job.progress?.['stage'] === 'decode')
  )
    return null;
  return estimateCompletion(job.started_at, progressPercent(job.progress), job.updated_at);
};

export const fileRingProgress = (
  file: Pick<
    MediaFileRow,
    | 'active_job_status'
    | 'active_job_progress'
    | 'transcode_status'
    | 'cooldown_started_at'
    | 'cooldown_until'
  >,
  now = new Date(),
) => {
  if (
    !file.active_job_status &&
    cooldownRemaining('pending', file.cooldown_started_at, file.cooldown_until, now) === null
  )
    return null;
  const status = file.active_job_status ?? (file.transcode_status === 'pending' ? 'pending' : null);
  if (!status) return null;
  const cooldown = status === 'hold' || !file.active_job_status;
  return {
    status,
    percent: progressPercent(file.active_job_progress),
    cooldownStartedAt: cooldown ? file.cooldown_started_at : null,
    cooldownUntil: cooldown ? file.cooldown_until : null,
  };
};

export const progressStage = (
  progress: Record<string, unknown> | null | undefined,
): string | null => {
  switch (progress?.['tool']) {
    case 'waiting':
      return 'Waiting for source';
    case 'ffprobe':
      return 'Reading metadata';
    case 'mkvpropedit':
      return 'Calculating MKV statistics';
  }
  switch (progress?.['stage']) {
    case 'probe':
      return 'Reading metadata';
    case 'decode':
      return 'Decoding';
    default:
      return null;
  }
};

export const cooldownPercent = (
  startedAt: string | null | undefined,
  until: string | null | undefined,
  now: Date,
): number | null => {
  if (!startedAt || !until) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(until);
  const current = now.getTime();
  if (![start, end, current].every(Number.isFinite) || end <= start) return null;
  return Math.min(100, Math.max(0, ((current - start) / (end - start)) * 100));
};

export const cooldownColor = (percent: number | null): string => {
  if (percent === null || percent < 25) return 'stroke-(--ui-text-muted)';
  if (percent < 50) return 'stroke-info';
  if (percent < 75) return 'stroke-warning';
  return 'stroke-success';
};

export const cooldownRemaining = (
  status: string,
  startedAt: string | null | undefined,
  until: string | null | undefined,
  now: Date,
): number | null => {
  if (
    !until ||
    (status !== 'hold' && (status !== 'pending' || cooldownPercent(startedAt, until, now) === null))
  )
    return null;
  const end = Date.parse(until);
  if (!Number.isFinite(end)) return null;
  const remaining = Math.max(0, Math.ceil((end - now.getTime()) / 1000));
  return status === 'pending' && remaining === 0 ? null : remaining;
};

export const formatCountdown = (seconds: number): string => {
  const total = Math.floor(seconds);
  const pad = (value: number): string => String(value).padStart(2, '0');
  if (total >= 3600) return `${pad(Math.floor(total / 3600))}:${pad(Math.floor(total / 60) % 60)}`;
  if (total >= 60) return `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
  return pad(total);
};
