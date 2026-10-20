import { expect, test } from 'bun:test';
import { appendLog, jobLogLines, LOG_WINDOW, serializeLogs } from '../app/utils/logs';
import type { LogEntry } from '../app/types/logs';

const entry = (id: number): LogEntry => ({
  id,
  timestamp: '2026-10-02T12:00:00Z',
  level: 'warning',
  logger: 'server.test',
  message: 'failure\nwith detail',
  fields: { nested: { value: ['café', 2] } },
  source: { path: '/full/source/path.py', line: 7 },
  exception: {
    type: 'ValueError',
    message: 'all details',
    stack: [{ path: '/full/exception/path.py', line: 9, function: 'check' }],
  },
});

test('bounds live records and deduplicates', () => {
  const entries: LogEntry[] = [];
  const ids = new Set<number>();
  for (let id = 1; id <= 10000; id++) {
    appendLog(entries, ids, entry(id));
    appendLog(entries, ids, entry(id));
  }
  expect(entries.length).toBe(LOG_WINDOW);
  expect(ids.size).toBe(LOG_WINDOW);
  expect(entries[0]?.id).toBe(10001 - LOG_WINDOW);
  expect(entries.at(-1)?.id).toBe(10000);
});

test('downloads complete structured records', () => {
  const entries = [entry(1), entry(2)];
  expect(
    serializeLogs(entries)
      .trim()
      .split('\n')
      .map((line) => JSON.parse(line)),
  ).toEqual(entries);
});

test('bounds job lines while filters reach earlier text', () => {
  const text =
    'phase: ffmpeg\n' + Array.from({ length: 10000 }, (_, id) => `line ${id}`).join('\n');
  const lines = jobLogLines(text, 'line');
  expect(lines.length).toBe(LOG_WINDOW);
  expect(lines[0]?.line).toBe(`line ${10000 - LOG_WINDOW}`);
  expect(lines.at(-1)?.line).toBe('line 9999');
  expect(jobLogLines(text, 'line 0').map(({ line }) => line)).toEqual(['line 0']);
});
