import { describe, expect, test } from 'bun:test';
import { parseStatuses, statusInfo, statusOptions } from '../app/utils/status';

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
    color: 'info',
    icon: 'i-lucide-hourglass',
    active: true,
  });
  expect(statusInfo('pending', 'media')).toMatchObject({
    color: 'neutral',
    icon: 'i-lucide-clock',
  });
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
