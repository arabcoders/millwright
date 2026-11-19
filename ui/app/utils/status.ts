import type { JobRow, MediaFileRow } from '../types/api';

export type StatusColor = 'success' | 'error' | 'neutral' | 'warning' | 'info';

export type StatusInfo = { label: string; color: StatusColor; icon: string; active?: boolean };

const values: Record<string, StatusInfo> = {};

const add = (
  group: string,
  entries: Record<string, Omit<StatusInfo, 'label'> & { label?: string }>,
) => {
  for (const [value, info] of Object.entries(entries)) {
    values[`${group}:${value}`] = {
      ...info,
      label: info.label ?? value.replaceAll('_', ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
    };
  }
};

add('job', {
  pending: { color: 'neutral', icon: 'i-lucide-hourglass', active: true },
  staging: { label: 'Preparing', color: 'info', icon: 'i-lucide-hourglass', active: true },
  hold: { label: 'On hold', color: 'neutral', icon: 'i-lucide-clock-alert', active: true },
  queued: { color: 'neutral', icon: 'i-lucide-hourglass', active: true },
  claimed: { label: 'Assigned', color: 'info', icon: 'i-lucide-inbox', active: true },
  running: { color: 'info', icon: 'i-lucide-loader-circle', active: true },
  verifying: { color: 'info', icon: 'i-lucide-search-check', active: true },
  finalizing: { color: 'info', icon: 'i-lucide-settings-2', active: true },
  succeeded: { color: 'success', icon: 'i-lucide-circle-check' },
  skipped: { label: 'Not needed', color: 'neutral', icon: 'i-lucide-skip-forward' },
  failed: { color: 'error', icon: 'i-lucide-circle-x' },
  cancelled: { color: 'neutral', icon: 'i-lucide-ban' },
});
add('discovery', {
  paused: { color: 'neutral', icon: 'i-lucide-pause' },
  cancelling: { color: 'warning', icon: 'i-lucide-hourglass', active: true },
  cancelled: { color: 'neutral', icon: 'i-lucide-ban' },
  idle: { color: 'neutral', icon: 'i-lucide-circle-minus' },
  queued: { color: 'warning', icon: 'i-lucide-hourglass', active: true },
  scanning: { color: 'info', icon: 'i-lucide-radar', active: true },
  completed: { color: 'success', icon: 'i-lucide-circle-check' },
  failed: { color: 'error', icon: 'i-lucide-circle-x' },
});
add('probing', {
  paused: { color: 'neutral', icon: 'i-lucide-pause' },
  idle: { color: 'neutral', icon: 'i-lucide-circle-minus' },
  queued: { label: 'Pending', color: 'warning', icon: 'i-lucide-hourglass', active: true },
  scanning: { label: 'Active', color: 'info', icon: 'i-lucide-loader-circle', active: true },
  completed: { color: 'success', icon: 'i-lucide-circle-check' },
  failed: { color: 'error', icon: 'i-lucide-circle-x' },
});
add('media', {
  pending: { label: 'Pending', color: 'neutral', icon: 'i-lucide-clock' },
  failed: { label: 'Transcode failed', color: 'error', icon: 'i-lucide-circle-x' },
  cancelled: { label: 'Transcode cancelled', color: 'neutral', icon: 'i-lucide-ban' },
  rejected: { label: 'Flow rejected', color: 'warning', icon: 'i-lucide-ban' },
  ...Object.fromEntries(
    Object.entries(values)
      .filter(([key, info]) => key.startsWith('job:') && key !== 'job:pending' && info.active)
      .map(([key, info]) => [key.slice(4), info]),
  ),
  not_needed: { label: 'Transcode not needed', color: 'neutral', icon: 'i-lucide-skip-forward' },
  transcoded: { color: 'success', icon: 'i-lucide-circle-check' },
});
add('health', {
  pending: { label: 'Not checked', color: 'neutral', icon: 'i-lucide-circle-minus' },
  healthy: { color: 'success', icon: 'i-lucide-heart-pulse' },
  corrupt: { label: 'Issues found', color: 'error', icon: 'i-lucide-triangle-alert' },
  probe_failed: { label: 'Check failed', color: 'error', icon: 'i-lucide-circle-x' },
});
add('plugin', {
  ready: { color: 'success', icon: 'i-lucide-circle-check' },
  stopped: { color: 'neutral', icon: 'i-lucide-square' },
  disabled: { color: 'neutral', icon: 'i-lucide-ban' },
});
add('flow', {
  matched: { color: 'success', icon: 'i-lucide-check' },
  deferred: { color: 'info', icon: 'i-lucide-clock-arrow-down' },
  skipped: { color: 'neutral', icon: 'i-lucide-skip-forward' },
  vetoed: { color: 'warning', icon: 'i-lucide-ban' },
  error: { color: 'error', icon: 'i-lucide-circle-x' },
  unavailable: { color: 'warning', icon: 'i-lucide-circle-alert' },
  not_evaluated: { label: 'Not evaluated', color: 'neutral', icon: 'i-lucide-circle-minus' },
});
add('dry_run', {
  preparing: { color: 'warning', icon: 'i-lucide-hourglass', active: true },
  running: { color: 'info', icon: 'i-lucide-loader-circle', active: true },
  cancelling: { color: 'warning', icon: 'i-lucide-loader-circle', active: true },
  completed: { color: 'success', icon: 'i-lucide-circle-check' },
  cancelled: { color: 'neutral', icon: 'i-lucide-ban' },
  failed: { color: 'error', icon: 'i-lucide-circle-x' },
});
add('kind', {
  transcode: { color: 'neutral', icon: 'i-lucide-clapperboard' },
  healthcheck: { label: 'Health check', color: 'neutral', icon: 'i-lucide-heart-pulse' },
  probe: { label: 'Metadata probe', color: 'neutral', icon: 'i-lucide-search' },
  mkv_stats: {
    label: 'MKV statistics',
    color: 'neutral',
    icon: 'i-lucide-chart-no-axes-combined',
  },
});
add('outcome', {
  match: { color: 'success', icon: 'i-lucide-check' },
  veto: { color: 'warning', icon: 'i-lucide-ban' },
  skip: { color: 'neutral', icon: 'i-lucide-skip-forward' },
  plan: { label: 'Convert', color: 'info', icon: 'i-lucide-list-check' },
  actions: { color: 'info', icon: 'i-lucide-list' },
  deferred: { color: 'info', icon: 'i-lucide-clock-arrow-down' },
  true: { label: 'Matched', color: 'success', icon: 'i-lucide-check' },
  false: { label: 'Not matched', color: 'error', icon: 'i-lucide-x' },
  done: { color: 'success', icon: 'i-lucide-circle-check' },
  next: { color: 'info', icon: 'i-lucide-arrow-right' },
  error: { color: 'error', icon: 'i-lucide-circle-x' },
});
add('action', {
  ok: { color: 'success', icon: 'i-lucide-check' },
  failed: { color: 'error', icon: 'i-lucide-circle-x' },
});
add('notification', {
  pending: { color: 'warning', icon: 'i-lucide-hourglass', active: true },
  sent: { color: 'success', icon: 'i-lucide-check' },
  failed: { color: 'error', icon: 'i-lucide-circle-x' },
});
add('stream', {
  copy: { color: 'success', icon: 'i-lucide-copy' },
  transcode: { color: 'info', icon: 'i-lucide-clapperboard' },
  drop: { color: 'warning', icon: 'i-lucide-circle-minus' },
  retag: { color: 'info', icon: 'i-lucide-tag' },
});
add('node', {
  online: { color: 'success', icon: 'i-lucide-server' },
  offline: { color: 'neutral', icon: 'i-lucide-server-off' },
  paused: { label: 'Paused', color: 'warning', icon: 'i-lucide-pause' },
  busy: { color: 'info', icon: 'i-lucide-loader-circle', active: true },
});

const unknown: StatusInfo = { label: 'Unknown', color: 'neutral', icon: 'i-lucide-circle-help' };
export const statusInfo = (value: string, group = 'job'): StatusInfo =>
  values[`${group}:${value}`] ?? {
    ...unknown,
    label: value.replaceAll('_', ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
  };

export const jobIndicatorInfo = (
  job: Pick<JobRow, 'kind' | 'status'>,
  healthy?: unknown,
): StatusInfo => {
  const kind = statusInfo(job.kind, 'kind');
  const status = statusInfo(job.status);
  const health =
    job.kind === 'healthcheck' && job.status === 'succeeded'
      ? healthy === true
        ? 'Healthy'
        : healthy === false
          ? 'Corrupt'
          : null
      : null;
  return {
    icon: kind.icon,
    color: health === 'Corrupt' ? 'error' : status.color,
    label: `${kind.label}: ${status.label}${health ? ` (${health})` : ''}`,
  };
};

export const fileIndicatorInfo = (
  file: Pick<
    MediaFileRow,
    'transcode_status' | 'health_status' | 'active_job_kind' | 'active_job_status'
  >,
): StatusInfo => {
  const transcode = statusInfo(file.transcode_status, 'media');
  const health = statusInfo(file.health_status, 'health');
  const active = file.active_job_status ? statusInfo(file.active_job_status) : null;
  const progress = active
    ? `${statusInfo(file.active_job_kind ?? '', 'kind').label}: ${active.label}. `
    : '';
  return {
    icon: 'i-lucide-file-video',
    color: active ? active.color : health.color === 'error' ? 'error' : transcode.color,
    label: `${progress}${transcode.label}. Health: ${health.label}.`,
  };
};

export const statusOptions = (
  group: string,
  allLabel = 'All',
): Array<StatusInfo & { value: string }> => [
  { ...unknown, label: allLabel, value: '' },
  ...Object.keys(values)
    .filter((key) => key.startsWith(`${group}:`))
    .map((key) => ({ ...values[key]!, value: key.slice(group.length + 1) })),
];

export const parseStatuses = (
  value: unknown,
  options: ReadonlyArray<{ value: string }>,
): string[] =>
  typeof value === 'string'
    ? [...new Set(value.split(',').map((status) => status.trim()))].filter((status) =>
        options.some((option) => option.value === status),
      )
    : [];
