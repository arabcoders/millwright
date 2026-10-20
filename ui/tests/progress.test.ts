import { expect, test } from 'bun:test';
import {
  cooldownColor,
  cooldownPercent,
  cooldownRemaining,
  estimatedJobCompletion,
  fileRingProgress,
  formatCountdown,
  progressPercent,
} from '../app/utils/progress';
import { elapsedSeconds } from '../app/utils/date';

test('Job remaining estimate', () => {
  const job = {
    status: 'running' as const,
    started_at: '2026-10-15T12:00:00Z',
    updated_at: '2026-10-15T12:04:20Z',
    finished_at: null,
    progress: { stage: 'decode', percent: 50 },
  };
  for (const kind of ['transcode', 'healthcheck']) {
    const end = estimatedJobCompletion({ ...job, kind });
    expect(elapsedSeconds(job.updated_at, end)).toBe(260);
    expect(elapsedSeconds('2026-10-15T12:04:21Z', end)).toBe(259);
    expect(elapsedSeconds('2026-10-15T12:09:00Z', end)).toBeNull();
  }
});

test('Unestimated job phases', () => {
  const job = {
    kind: 'healthcheck',
    status: 'running' as const,
    started_at: '2026-10-15T12:00:00Z',
    updated_at: '2026-10-15T12:04:20Z',
    finished_at: null,
    progress: { stage: 'decode', percent: 50 },
  };
  expect(estimatedJobCompletion({ ...job, progress: { stage: 'probe', percent: 50 } })).toBeNull();
  expect(estimatedJobCompletion({ ...job, progress: { stage: 'decode' } })).toBeNull();
  expect(estimatedJobCompletion({ ...job, progress: { stage: 'decode', percent: 0 } })).toBeNull();
  expect(estimatedJobCompletion({ ...job, progress: { stage: 'decode', percent: 100 } })).toBeNull();
  expect(estimatedJobCompletion({ ...job, status: 'finalizing' })).toBeNull();
  expect(estimatedJobCompletion({ ...job, status: 'succeeded', finished_at: job.updated_at })).toBeNull();
});

test('Progress values', () => {
  expect(progressPercent({ percent: 42.5 })).toBe(42.5);
  expect(progressPercent({ percent: 0 })).toBe(0);
  expect(progressPercent({ percent: -5 })).toBe(0);
  expect(progressPercent({ percent: 105 })).toBe(100);
});

test('Unavailable progress', () => {
  for (const value of [
    null,
    undefined,
    {},
    { percent: null },
    { percent: NaN },
    { percent: Infinity },
    { percent: 'unknown' },
  ]) {
    expect(progressPercent(value)).toBeNull();
  }
});

test('Cooldown progress', () => {
  const start = '2026-01-01T00:00:00Z';
  const until = '2026-01-01T01:00:00Z';
  expect(cooldownPercent(start, until, new Date(start))).toBe(0);
  expect(cooldownPercent(start, until, new Date('2026-01-01T00:30:00Z'))).toBe(50);
  expect(cooldownPercent(start, until, new Date(until))).toBe(100);
  expect(cooldownPercent(start, until, new Date('2026-01-01T02:00:00Z'))).toBe(100);
  expect(cooldownRemaining('hold', start, until, new Date(until))).toBe(0);
  expect(cooldownRemaining('hold', null, until, new Date(start))).toBe(3600);
  expect(cooldownPercent(start, until, new Date('2025-12-31T23:00:00Z'))).toBe(0);
  expect(cooldownPercent(null, until, new Date(start))).toBeNull();
  expect(cooldownPercent(start, null, new Date(start))).toBeNull();
  expect(cooldownPercent(start, start, new Date(start))).toBeNull();
  expect(cooldownPercent(until, start, new Date(start))).toBeNull();
  expect(cooldownPercent('not-a-date', until, new Date(start))).toBeNull();
  expect(cooldownPercent('2026-01-01T00:30:00Z', until, new Date('2026-01-01T00:45:00Z'))).toBe(50);
});

test('Cooldown milestone colors', () => {
  expect(cooldownColor(null)).toBe('stroke-(--ui-text-muted)');
  expect(cooldownColor(0)).toBe('stroke-(--ui-text-muted)');
  expect(cooldownColor(24.99)).toBe('stroke-(--ui-text-muted)');
  expect(cooldownColor(25)).toBe('stroke-info');
  expect(cooldownColor(49.99)).toBe('stroke-info');
  expect(cooldownColor(50)).toBe('stroke-warning');
  expect(cooldownColor(74.99)).toBe('stroke-warning');
  expect(cooldownColor(75)).toBe('stroke-success');
  expect(cooldownColor(100)).toBe('stroke-success');
});

test('Pending cooldown expiry', () => {
  const file = {
    active_job_status: null,
    active_job_progress: null,
    transcode_status: 'pending' as const,
    cooldown_started_at: '2026-01-01T00:00:00Z',
    cooldown_until: '2026-01-01T01:00:00Z',
  };
  const ring = fileRingProgress(file, new Date('2026-01-01T00:30:00Z'));
  expect(ring?.status).toBe('pending');
  if (!ring) throw new Error('Pending file ring unavailable.');
  const remaining = (now: string) =>
    cooldownRemaining(ring.status, ring.cooldownStartedAt, ring.cooldownUntil, new Date(now));
  expect(remaining('2026-01-01T00:30:00Z')).toBe(1800);
  expect(remaining('2026-01-01T00:59:59.500Z')).toBe(1);
  expect(remaining('2026-01-01T01:00:00Z')).toBeNull();
  expect(remaining('2026-01-01T02:00:00Z')).toBeNull();
  expect(fileRingProgress(file, new Date('2026-01-01T01:00:00Z'))).toBeNull();
  expect(fileRingProgress(file, new Date('2026-01-01T02:00:00Z'))).toBeNull();
  expect(
    fileRingProgress({ ...file, cooldown_until: null }, new Date('2026-01-01T00:30:00Z')),
  ).toBeNull();
});

test('Pending cooldown validity', () => {
  const until = '2026-01-01T01:00:00Z';
  const now = new Date('2026-01-01T00:30:00Z');
  expect(cooldownRemaining('pending', null, until, now)).toBeNull();
  expect(cooldownRemaining('pending', 'not-a-date', until, now)).toBeNull();
  expect(cooldownRemaining('pending', until, until, now)).toBeNull();
});

test('Active progress priority', () => {
  const file = {
    active_job_progress: { percent: 42 },
    transcode_status: 'pending',
    cooldown_started_at: '2026-01-01T00:00:00Z',
    cooldown_until: '2026-01-01T01:00:00Z',
  } as const;
  for (const status of ['running', 'queued', 'pending']) {
    const ring = fileRingProgress({ ...file, active_job_status: status });
    expect(ring).toEqual({
      status,
      percent: 42,
      cooldownStartedAt: null,
      cooldownUntil: null,
    });
  }
  const held = fileRingProgress({ ...file, active_job_status: 'hold' });
  expect(held?.cooldownStartedAt).toBe(file.cooldown_started_at);
  expect(held?.cooldownUntil).toBe(file.cooldown_until);
  expect(
    cooldownRemaining(
      'running',
      file.cooldown_started_at,
      file.cooldown_until,
      new Date(file.cooldown_started_at),
    ),
  ).toBeNull();
});

test('Completed file icons', () => {
  for (const transcode_status of ['transcoded', 'not_needed'] as const) {
    expect(
      fileRingProgress(
        {
          active_job_status: null,
          active_job_progress: null,
          transcode_status,
          cooldown_started_at: '2026-01-01T00:00:00Z',
          cooldown_until: '2026-01-01T01:00:00Z',
        },
        new Date('2026-01-01T00:30:00Z'),
      ),
    ).toBeNull();
  }
});

test('Countdown boundaries', () => {
  expect(formatCountdown(0)).toBe('00');
  expect(formatCountdown(9)).toBe('09');
  expect(formatCountdown(59)).toBe('59');
  expect(formatCountdown(60)).toBe('01:00');
  expect(formatCountdown(3599)).toBe('59:59');
  expect(formatCountdown(3600)).toBe('01:00');
  expect(formatCountdown(7199)).toBe('01:59');
  expect(formatCountdown(36000)).toBe('10:00');
});
