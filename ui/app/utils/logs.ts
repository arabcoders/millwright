import type { LogEntry } from '../types/logs';

export const filterLogTextLines = (value: string, filter: string): string[] => {
  const query = filter.trim().toLowerCase();
  const lines = value.split('\n');
  return query ? lines.filter((line) => line.toLowerCase().includes(query)) : lines;
};

export const LOG_WINDOW = 300;

export const appendLog = (entries: LogEntry[], ids: Set<number>, entry: LogEntry): boolean => {
  if (ids.has(entry.id)) return false;
  entries.push(entry);
  ids.add(entry.id);
  if (entries.length > LOG_WINDOW) {
    const removed = entries.shift();
    if (removed) ids.delete(removed.id);
    return true;
  }
  return false;
};

export const serializeLogs = (entries: LogEntry[]): string =>
  entries.map((entry) => JSON.stringify(entry)).join('\n') + (entries.length ? '\n' : '');

export const jobLogLines = (text: string, filter: string): { stage: string; line: string }[] => {
  const lines: { stage: string; line: string }[] = [];
  const query = filter.trim().toLowerCase();
  let stage = 'Recent output';
  let start = 0;
  let count = 0;
  while (start < text.length) {
    const newline = text.indexOf('\n', start);
    const end = newline < 0 ? text.length : newline;
    const line = text.slice(start, end);
    start = end + 1;
    const marker = line.match(/phase:\s*(prepare|ffmpeg|probe)/i);
    if (marker)
      stage =
        { prepare: 'Prepare', ffmpeg: 'Encode', probe: 'Verify' }[
          (marker[1] ?? '').toLowerCase()
        ] ?? stage;
    else if (/healthcheck/i.test(line)) stage = 'Health check';
    if (line && (!query || line.toLowerCase().includes(query))) {
      lines[count % LOG_WINDOW] = { stage, line };
      count += 1;
    }
  }
  const boundary = count > LOG_WINDOW ? count % LOG_WINDOW : 0;
  return boundary ? [...lines.slice(boundary), ...lines.slice(0, boundary)] : lines;
};
