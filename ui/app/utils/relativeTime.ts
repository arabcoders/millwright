import { parseDate, type DateInput } from './date';

const UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['year', 365 * 24 * 60 * 60],
  ['month', 30 * 24 * 60 * 60],
  ['week', 7 * 24 * 60 * 60],
  ['day', 24 * 60 * 60],
  ['hour', 60 * 60],
  ['minute', 60],
  ['second', 1],
];

export const formatRelativeTime = (value: DateInput, locale: string): string => {
  const date = parseDate(value);

  if (!date) {
    return '';
  }

  const diff = Math.round((date.getTime() - Date.now()) / 1000);
  const abs = Math.abs(diff);
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });

  for (const [unit, seconds] of UNITS) {
    if (abs >= seconds || unit === 'second') {
      return formatter.format(Math.round(diff / seconds), unit);
    }
  }

  return formatter.format(0, 'second');
};
