import { describe, expect, test } from 'bun:test';
import { compareRows, sortStateRows } from '../app/composables/stateHelpers';

const row = (id: number) => ({ id });

describe('shared state ordering', () => {
  test('keeps id ties in requested direction', () => {
    expect(compareRows(row(1), row(2), 'x', 'x', 1)).toBeLessThan(0);
    expect(compareRows(row(1), row(2), 'x', 'x', -1)).toBeGreaterThan(0);
  });

  test('puts nulls last both ways', () => {
    expect(compareRows(row(1), row(2), null, 1, 1)).toBeGreaterThan(0);
    expect(compareRows(row(1), row(2), null, 1, -1)).toBeGreaterThan(0);
  });

  test('compares timestamp precision', () => {
    expect(
      compareRows(row(1), row(2), '2026-01-01T00:00:00.1Z', '2026-01-01T00:00:00Z', 1),
    ).toBeGreaterThan(0);
    expect(
      compareRows(row(1), row(2), '2026-01-01T00:00:00.000002Z', '2026-01-01T00:00:00.000001Z', 1),
    ).toBeGreaterThan(0);
  });

  test('sorts without mutating rows', () => {
    const snapshot = [
      { id: 1, priority: 0, progress: 0 },
      { id: 2, priority: 1, progress: 0 },
    ];
    expect(sortStateRows(snapshot, (item) => item.priority, -1)).toEqual([
      { id: 2, priority: 1, progress: 0 },
      { id: 1, priority: 0, progress: 0 },
    ]);
    expect(snapshot[0]?.progress).toBe(0);
  });
});
