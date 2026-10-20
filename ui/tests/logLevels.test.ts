import { expect, test } from 'bun:test';
import { getLogLevel, logLevels, normalizeLogLevels } from '../app/utils/logLevels';

test('invalid saved levels', () => {
  for (const value of ['info', { level: 'info' }, null, 1, ['unsupported']]) {
    const selected = normalizeLogLevels(value);
    expect(selected).toEqual([...logLevels]);
    expect(selected.includes('info')).toBe(true);
  }
});

test('mixed saved levels', () => {
  expect(normalizeLogLevels(['error', null, 'info', 'error', {}])).toEqual(['error', 'info']);
});

test('empty saved levels', () => {
  expect(normalizeLogLevels([])).toEqual([]);
});

test('critical log visibility', () => {
  expect(normalizeLogLevels(['error']).includes(getLogLevel('CRITICAL'))).toBe(true);
});
