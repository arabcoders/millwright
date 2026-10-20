import { ref, shallowReadonly } from 'vue';

export type DateInput = string | number | Date | null | undefined;
export type DateFormat =
  | 'datetime'
  | 'timestamp'
  | 'detail'
  | 'long'
  | 'clock'
  | 'shortClock'
  | 'date'
  | 'shortDate';

const dateFields: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
};
const timeFields: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit' };
const formats: Record<DateFormat, Intl.DateTimeFormatOptions> = {
  datetime: { ...dateFields, ...timeFields, timeZoneName: 'longOffset' },
  timestamp: { ...dateFields, ...timeFields, second: '2-digit', timeZoneName: 'longOffset' },
  detail: { ...dateFields, ...timeFields, second: '2-digit' },
  long: { ...dateFields, ...timeFields, month: 'long', second: '2-digit' },
  clock: { ...timeFields, second: '2-digit' },
  shortClock: timeFields,
  date: dateFields,
  shortDate: { month: 'short', day: 'numeric' },
};

const timezone = ref<string | null>(null);
export const dateTimezone = shallowReadonly(timezone);

export const configureDates = (value: string | null): void => {
  timezone.value = value;
};

export const parseDate = (value: DateInput): Date | null => {
  if (null == value || value === '') {
    return null;
  }

  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }

  if (typeof value === 'number') {
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  const dateOnly = /^(\d{4})-?(\d{2})-?(\d{2})$/.exec(value);
  if (dateOnly) {
    // A calendar day still exists when daylight saving skips its midnight.
    return parseDateTimeInput(`${dateOnly[1]}-${dateOnly[2]}-${dateOnly[3]}T00:00`, true);
  }
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(value)) {
    return parseDateTimeInput(value);
  }

  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
};

export const formatDate = (
  value: DateInput,
  format: DateFormat = 'datetime',
  locale: string = 'en',
): string => {
  if (!timezone.value) return '';
  const date = parseDate(value);
  if (!date) return '';
  return new Intl.DateTimeFormat(locale, {
    ...formats[format],
    timeZone: timezone.value,
  }).format(date);
};

export const toIsoString = (value: DateInput): string => parseDate(value)?.toISOString() ?? '';

export const formatDateTimeInput = (value: DateInput): string => {
  if (!timezone.value) return '';
  const date = parseDate(value);
  if (!date) return '';
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en', {
      timeZone: timezone.value,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    })
      .formatToParts(date)
      .map(({ type, value }) => [type, value]),
  );
  const milliseconds = String(date.getUTCMilliseconds()).padStart(3, '0');
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}.${milliseconds}`;
};

export const parseDateTimeInput = (value: string, allowSkippedMidnight = false): Date | null => {
  if (!timezone.value || !value) return null;
  const wallTime = new Date(`${value}Z`);
  if (Number.isNaN(wallTime.getTime())) return null;
  const normalized = wallTime.toISOString().slice(0, -1);
  if (!normalized.startsWith(value)) return null;

  // Sample both sides of a timezone transition. Reject skipped wall times and
  // choose the earlier occurrence when clocks repeat a time.
  const candidates: number[] = [];
  const shiftedMidnights: number[] = [];
  for (const shift of [-86400000, 0, 86400000]) {
    const sample = new Date(wallTime.getTime() + shift);
    const sampleWallTime = new Date(`${formatDateTimeInput(sample)}Z`);
    const offset = sampleWallTime.getTime() - sample.getTime();
    const candidate = new Date(wallTime.getTime() - offset);
    const local = formatDateTimeInput(candidate);
    if (local === normalized) candidates.push(candidate.getTime());
    else if (
      allowSkippedMidnight &&
      local > normalized &&
      local.slice(0, 10) === normalized.slice(0, 10)
    )
      shiftedMidnights.push(candidate.getTime());
  }
  const matches = candidates.length ? candidates : shiftedMidnights;
  return matches.length ? new Date(Math.min(...matches)) : null;
};

export const elapsedSeconds = (startedAt: DateInput, endedAt: DateInput): number | null => {
  const started = parseDate(startedAt);
  const ended = parseDate(endedAt);
  if (!started || !ended || ended < started) return null;
  return (ended.getTime() - started.getTime()) / 1000;
};

export const estimateCompletion = (
  startedAt: DateInput,
  percent: number | null,
  sampledAt: DateInput,
): Date | null => {
  const started = parseDate(startedAt);
  const sampled = parseDate(sampledAt);
  if (
    !started ||
    !sampled ||
    percent === null ||
    !Number.isFinite(percent) ||
    percent < 1 ||
    percent >= 100
  )
    return null;
  // Anchor the estimate to its progress sample, not the ticking display clock.
  const elapsed = sampled.getTime() - started.getTime();
  if (elapsed < 10_000) return null;
  return new Date(started.getTime() + elapsed / (percent / 100));
};
