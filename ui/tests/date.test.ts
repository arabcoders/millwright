import { beforeEach, expect, test } from 'bun:test';
import { computed } from 'vue';
import {
  configureDates,
  elapsedSeconds,
  formatDate,
  formatDateTimeInput,
  parseDate,
  parseDateTimeInput,
} from '../app/utils/date';

beforeEach(() => configureDates(null));

test('Processing duration', () => {
  expect(elapsedSeconds('2026-10-06T12:00:00Z', '2026-10-06T12:03:30Z')).toBe(210);
  expect(elapsedSeconds('2026-10-06T12:00:00Z', new Date('2026-10-06T12:05:00Z'))).toBe(300);
});

test('Missing timestamps', () => {
  expect(elapsedSeconds(null, '2026-10-06T12:00:00Z')).toBeNull();
  expect(elapsedSeconds('2026-10-06T12:00:00Z', null)).toBeNull();
  expect(elapsedSeconds('invalid', '2026-10-06T12:00:00Z')).toBeNull();
  expect(elapsedSeconds('2026-10-06T12:00:00Z', 'invalid')).toBeNull();
});

test('Timestamp ordering', () => {
  expect(elapsedSeconds('2026-10-06T12:00:00Z', '2026-10-06T12:00:00Z')).toBe(0);
  expect(elapsedSeconds('2026-10-06T12:00:01Z', '2026-10-06T12:00:00Z')).toBeNull();
});

test('Configured timezone input', () => {
  configureDates('Asia/Kuwait');
  expect(formatDateTimeInput('2026-10-10T12:30:00Z')).toBe('2026-10-10T15:30:00.000');
  expect(parseDateTimeInput('2026-10-10T15:30')?.toISOString()).toBe('2026-10-10T12:30:00.000Z');
  expect(parseDateTimeInput('2026-01-10T15:30')?.toISOString()).toBe('2026-01-10T12:30:00.000Z');
  configureDates('Asia/Kathmandu');
  expect(parseDateTimeInput('2026-10-10T18:15:00.123')?.toISOString()).toBe(
    '2026-10-10T12:30:00.123Z',
  );
  configureDates('UTC');
  expect(formatDateTimeInput('2026-10-10T00:00:00Z')).toBe('2026-10-10T00:00:00.000');
  expect(formatDateTimeInput(null)).toBe('');
});

test('Invalid cutoff times', () => {
  configureDates('UTC');
  expect(parseDateTimeInput('')).toBeNull();
  expect(parseDateTimeInput('invalid')).toBeNull();
  expect(parseDateTimeInput('2026-02-30T12:00')).toBeNull();
  configureDates('Europe/Berlin');
  expect(parseDateTimeInput('2026-03-29T02:30')).toBeNull();
});

test('Repeated cutoff times', () => {
  configureDates('Europe/Berlin');
  expect(formatDateTimeInput('2026-10-25T00:30:00Z')).toBe('2026-10-25T02:30:00.000');
  expect(formatDateTimeInput('2026-10-25T01:30:00Z')).toBe('2026-10-25T02:30:00.000');
  expect(parseDateTimeInput('2026-10-25T02:30')?.toISOString()).toBe('2026-10-25T00:30:00.000Z');
});

test('Shared timezone updates', () => {
  const clock = computed(() => formatDate('2026-10-10T12:30:00Z', 'clock'));
  expect(clock.value).toBe('');
  configureDates('Asia/Kuwait');
  expect(clock.value).toBe('03:30:00 PM');
  expect(formatDate('2026-10-10T12:30:00Z', 'timestamp')).toContain('03:30:00 PM GMT+03:00');
  configureDates('Asia/Kathmandu');
  expect(clock.value).toBe('06:15:00 PM');
  expect(formatDate('2026-10-10T12:30:00Z')).toContain('GMT+05:45');
});

test('Calendar date labels', () => {
  configureDates('Pacific/Honolulu');
  expect(formatDate('2026-10-10', 'shortDate')).toBe('Oct 10');
  expect(formatDate('2026-10-10T00:00:00Z', 'shortDate')).toBe('Oct 9');
  expect(parseDate('20261010')?.toISOString()).toBe('2026-10-10T10:00:00.000Z');
  expect(parseDate('2026-10-10T08:30')?.toISOString()).toBe('2026-10-10T18:30:00.000Z');
});

test('Timezone not loaded', () => {
  expect(formatDate('2026-10-10T12:30:00Z')).toBe('');
  expect(formatDateTimeInput('2026-10-10T12:30:00Z')).toBe('');
  expect(parseDateTimeInput('2026-10-10T08:30')).toBeNull();
});

test('Skipped calendar midnight', () => {
  configureDates('America/Santiago');
  expect(formatDate('2026-09-06', 'shortDate')).toBe('Sep 6');
  expect(parseDateTimeInput('2026-09-06T00:00')).toBeNull();
});
