import { expect, test } from 'bun:test';
import { validJobExport } from '../app/utils/jobExport';

test('accepts complete imported snapshots', () => {
  const snapshot = {
    id: 8,
    status: 'succeeded',
    kind: 'transcode',
    attempts: [
      {
        id: 4,
        attempt_number: 1,
        log_data: 'full log',
        input_probe: { streams: [], raw: { retained: true } },
      },
    ],
    events: [{ id: 1, type: 'succeeded', ts: '2026-01-01T00:00:00Z' }],
    notification_deliveries: [
      {
        id: 2,
        target_name: 'external:sonarr',
        status: 'failed',
        attempts: 3,
        error: 'Series not identified.',
      },
    ],
    extra: { retained: ['value'] },
  };
  const imported: unknown = JSON.parse(JSON.stringify(snapshot));
  expect(validJobExport(imported)).toBe(true);
  expect(imported).toEqual(snapshot);
});

test('rejects malformed view fields', () => {
  const root = {
    id: 1,
    status: 'failed',
    kind: 'transcode',
    attempts: [],
    events: [],
    notification_deliveries: [],
  };
  for (const file of [{}, { path: 42, size: 1 }, { path: 'x', size: 1, video_fps: '24' }])
    expect(validJobExport({ ...root, file })).toBe(false);
  for (const log_data of [{}, 1, ['line']])
    expect(validJobExport({ ...root, attempts: [{ id: 1, attempt_number: 1, log_data }] })).toBe(
      false,
    );
  expect(
    validJobExport({
      ...root,
      attempts: [{ id: 1, attempt_number: 1, input_probe: { streams: [null] } }],
    }),
  ).toBe(false);
  expect(validJobExport({ ...root, decision_log: [null] })).toBe(false);
  expect(validJobExport({ ...root, notification_deliveries: [null] })).toBe(false);
  expect(
    validJobExport({
      ...root,
      events: [{ id: 1, type: 'stage', ts: '2026-01-01T00:00:00Z', payload: {} }],
      attempts: [
        {
          id: 1,
          attempt_number: 1,
          input_probe: { streams: [{ disposition_default: 'false' }] },
        },
      ],
    }),
  ).toBe(false);
});
