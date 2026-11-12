import { describe, expect, test } from 'bun:test';
import {
  fileIndicatorInfo,
  jobIndicatorInfo,
  parseStatuses,
  statusInfo,
  statusOptions,
} from '../app/utils/status';

describe('Status selections', () => {
  test('parses selected states', () => {
    expect(
      parseStatuses('failed, cancelled,failed,unknown,,queued', statusOptions('media').slice(1)),
    ).toEqual(['failed', 'cancelled', 'queued']);
    expect(parseStatuses('healthy,corrupt,pending', statusOptions('health').slice(1))).toEqual([
      'healthy',
      'corrupt',
      'pending',
    ]);
    expect(parseStatuses('queued,running', statusOptions('job').slice(1))).toEqual([
      'queued',
      'running',
    ]);
  });

  test('clears status selection', () => {
    for (const group of ['media', 'health', 'job']) {
      const options = statusOptions(group).slice(1);
      for (const value of [undefined, null, '', ', ,', 'unknown', ['healthy']]) {
        expect(parseStatuses(value, options)).toEqual([]);
      }
    }
  });
});

describe('Media status', () => {
  test('offers separate filters', () => {
    expect(statusOptions('media').map(({ value }) => value)).toEqual([
      '',
      'pending',
      'failed',
      'cancelled',
      'rejected',
      'staging',
      'hold',
      'queued',
      'claimed',
      'running',
      'verifying',
      'finalizing',
      'not_needed',
      'transcoded',
    ]);
  });
});

test('pending job active', () => {
  expect(statusInfo('pending', 'job')).toMatchObject({
    color: 'neutral',
    icon: 'i-lucide-hourglass',
    active: true,
  });
  expect(statusInfo('pending', 'media')).toMatchObject({
    color: 'neutral',
    icon: 'i-lucide-clock',
  });
});

test('File error precedence', () => {
  const file = {
    transcode_status: 'transcoded',
    health_status: 'healthy',
    active_job_kind: null,
    active_job_status: null,
  } as const;
  expect(fileIndicatorInfo({ ...file, transcode_status: 'failed' }).color).toBe('error');
  for (const health_status of ['corrupt', 'probe_failed']) {
    const info = fileIndicatorInfo({ ...file, health_status });
    expect(info.color).toBe('error');
    expect(info.label).toContain(statusInfo(health_status, 'health').label);
  }
});

test('File active precedence', () => {
  const info = fileIndicatorInfo({
    transcode_status: 'failed',
    health_status: 'corrupt',
    active_job_kind: 'healthcheck',
    active_job_status: 'running',
  });
  expect(info.color).toBe('info');
  expect(info.label).toContain('Health check: Running');
  expect(info.label).toContain('Transcode failed');
  expect(info.label).toContain('Issues found');
});

test('Healthy unprocessed file', () => {
  expect(
    fileIndicatorInfo({
      transcode_status: 'pending',
      health_status: 'healthy',
      active_job_kind: null,
      active_job_status: null,
    }).color,
  ).toBe('neutral');
});

test('Job health result', () => {
  const job = { kind: 'healthcheck', status: 'succeeded' } as const;
  const corrupt = jobIndicatorInfo(job, false);
  expect(corrupt.color).toBe('error');
  expect(corrupt.label).toContain('Corrupt');
  expect(jobIndicatorInfo({ ...job, status: 'running' }, false).color).toBe('info');
});

describe('Library preview', () => {
  test('offers preview filters', () => {
    expect(
      statusOptions('flow')
        .slice(1)
        .map(({ value }) => value),
    ).toEqual([
      'matched',
      'deferred',
      'skipped',
      'vetoed',
      'error',
      'unavailable',
      'not_evaluated',
    ]);
  });

  test('marks active states', () => {
    for (const state of ['preparing', 'running', 'cancelling']) {
      expect(statusInfo(state, 'dry_run').active).toBe(true);
    }
    for (const state of ['completed', 'cancelled', 'failed']) {
      expect(statusInfo(state, 'dry_run').active).toBeUndefined();
    }
  });
});
