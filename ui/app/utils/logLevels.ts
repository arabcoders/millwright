export const logLevels = ['debug', 'info', 'warning', 'error'] as const;
export type LogLevel = (typeof logLevels)[number];

export const getLogLevel = (value: string): LogLevel => {
  switch (value.toLowerCase()) {
    case 'info':
      return 'info';
    case 'warning':
    case 'warn':
      return 'warning';
    case 'error':
    case 'critical':
    case 'fatal':
      return 'error';
    default:
      return 'debug';
  }
};

const isLogLevel = (value: unknown): value is LogLevel =>
  typeof value === 'string' && logLevels.some((level) => level === value);

export const normalizeLogLevels = (value: unknown): LogLevel[] => {
  if (!Array.isArray(value)) return [...logLevels];
  const normalized = [...new Set(value.filter(isLogLevel))];
  return normalized.length || value.length === 0 ? normalized : [...logLevels];
};
