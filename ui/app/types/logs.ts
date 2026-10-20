export type LogField = string | number | boolean | null | LogField[] | { [key: string]: LogField };
export type LogSource = {
  file?: string;
  path?: string;
  module?: string;
  function?: string;
  line?: number;
};
export type LogProcess = { id?: number | string; name?: string };
export type LogThread = { id?: number | string; name?: string };
export type LogException = {
  type: string;
  message?: string;
  file?: string;
  line?: number;
  stack?: Array<{
    path?: string;
    file?: string;
    module?: string;
    function?: string;
    line?: number;
  }>;
};

export type LogEntry = {
  id: number;
  timestamp: string;
  level: string;
  logger: string;
  message: string;
  fields: Record<string, LogField>;
  source?: LogSource;
  process?: LogProcess;
  thread?: LogThread;
  exception?: LogException;
};

export type LogsResponse = {
  entries: LogEntry[];
  next_before: number | null;
  latest_id: number;
  oldest_id: number | null;
  truncated: boolean;
};
