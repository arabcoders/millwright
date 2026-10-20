import { readonly, ref, watch } from 'vue';
import type { FlowDecision, JobStatus, NodeUsage, StatsResponse } from '~/types/api';
import { useAuth } from '~/composables/useAuth';
import { request } from '~/utils';
import { diagnostic } from '~/utils/diagnostic';

export type JobStatusUpdate = {
  job_id: number;
  media_file_id: number | null;
  kind: string;
  status: 'pending' | 'queued';
  updated_at: string;
};

export type ServerEvent =
  | {
      type: 'library.deletion';
      payload: {
        library_id: number;
        library_name: string;
        status: 'deleting' | 'failed' | 'deleted';
        error: string | null;
      };
    }
  | { type: 'library.progress'; payload: import('~/types/api').LibraryProgress }
  | { type: 'scanner.state'; payload: import('~/types/api').ScannerState }
  | { type: 'probing.state'; payload: import('~/types/api').ProbeState }
  | { type: 'events.resync'; payload: { reason: 'overflow' } }
  | {
      type: 'events.batch';
      payload: {
        events: Array<
          Extract<ServerEvent, { type: 'job.status' | 'job.progress' | 'file.status' }>
        >;
      };
    }
  | {
      type: 'job.progress';
      payload: { job_id: number; progress: Record<string, unknown>; updated_at: string };
    }
  | { type: 'job.deleted'; payload: { job_id: number } }
  | {
      type: 'job.deletion';
      payload: { job_id: number; outcome: 'deleted' | 'failed'; reason: string };
    }
  | { type: 'job.cancel_requested'; payload: { job_id: number } }
  | {
      type: 'job.requeue';
      payload: {
        job_id: number;
        outcome: 'queued' | 'not_queued' | 'failed' | 'cancelled';
        reason: string;
        decision?: FlowDecision;
      };
    }
  | {
      type: 'job.request_result';
      payload: {
        job_id: number;
        file_id: number;
        kind: import('~/types/api').FileJobKind;
        outcome: 'queued' | 'not_queued' | 'failed' | 'cancelled';
        reason: string;
        decision?: FlowDecision;
      };
    }
  | {
      type: 'job.status';
      payload:
        | JobStatusUpdate
        | {
            job_id: number;
            status: JobStatus;
            priority: number;
            error: string | null;
            warning: string | null;
            finished_at: string | null;
            job: import('~/types/api').JobRow;
          };
    }
  | {
      type: 'node.status';
      payload: {
        name: string;
        state: string;
        active_jobs: number;
        active_job_ids: number[];
        active_transcodes: number;
        active_healthchecks: number;
        active_mkv_stats: number;
      };
    }
  | { type: 'node.usage'; payload: { name: string; usage: NodeUsage; last_seen_at: string } }
  | {
      type: 'operator.alert';
      payload: {
        operation: string;
        media_id: number;
        media_path: string;
        library_id: number;
        library_name: string | null;
        error: string;
      };
    }
  | {
      type: 'file.status';
      payload: {
        action: string;
        library_id: number;
        added?: number;
        updated?: number;
        pruned?: number;
        count?: number;
        media_file_id?: number;
        file?: import('~/types/api').MediaFileRow;
      };
    }
  | {
      type: 'library.scan';
      payload:
        | {
            status: 'succeeded' | 'cancelled';
            reason: string;
            library_id: number;
            library_name: string;
            found: number;
            added: number;
            updated: number;
            unchanged: number;
            pruned: number;
            error_count: number;
            error_samples: string[];
          }
        | {
            status: 'failed';
            reason: string;
            library_id: number;
            library_name: string;
            error: string;
          };
    }
  | { type: 'stats'; payload: StatsResponse };

export type ServerEventHandler = (event: ServerEvent) => void;

const BASE_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 30_000;

const connected = ref(false);
const handlers = new Set<ServerEventHandler>();
const reconnectHandlers = new Set<() => void>();
let socket: WebSocket | null = null;
let connecting = false;
let connectionVersion = 0;
let attempt = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let hasOpened = false;
let connectionFailureReported = false;

const wsUrl = (): string => {
  // NUXT_PUBLIC_WSS overrides the socket origin (see ui/.env.example). The
  // browser requires ws/wss schemes even when the base is configured as https.
  const base = useRuntimeConfig().public.wss?.trim();
  if (base) {
    const url = new URL('/api/events', base);
    if (url.protocol === 'https:') url.protocol = 'wss:';
    else if (url.protocol === 'http:') url.protocol = 'ws:';
    return url.toString();
  }
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${window.location.host}/api/events`;
};

const scheduleReconnect = (): void => {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
  }
  const backoff = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** attempt);
  attempt += 1;
  reconnectTimer = setTimeout(
    () => {
      reconnectTimer = null;
      void connect();
    },
    Math.round(backoff * (0.5 + Math.random())),
  );
};

// The server consumes each ticket once during the WebSocket upgrade.
const fetchTicket = async (): Promise<string | null> => {
  const response = await request('/api/auth/ws-ticket', { method: 'POST' });
  if (!response.ok) {
    return null;
  }
  const body = (await response.json()) as { ticket?: string };
  return body.ticket ?? null;
};

const connect = async (): Promise<void> => {
  if (!import.meta.client || socket || connecting) {
    return;
  }
  connecting = true;
  const version = connectionVersion;
  try {
    const me = useAuth().status.value;
    if (!me) {
      return;
    }

    const ticket = await fetchTicket();
    if (version !== connectionVersion) return;
    if (!ticket) {
      scheduleReconnect();
      return;
    }
    const url = `${wsUrl()}?ticket=${encodeURIComponent(ticket)}`;

    const ws = new WebSocket(url);
    socket = ws;
    ws.onopen = () => {
      attempt = 0;
      connected.value = true;
      connectionFailureReported = false;
      if (hasOpened) {
        diagnostic('debug', { event: 'events.reconnected', component: 'events' });
        for (const handler of reconnectHandlers) handler();
      }
      hasOpened = true;
    };
    ws.onmessage = (message) => {
      let event: ServerEvent;
      try {
        event = JSON.parse(message.data) as ServerEvent;
      } catch {
        diagnostic('debug', { event: 'events.invalid_message', component: 'events' });
        return;
      }
      const dispatch = (next: ServerEvent): void => {
        if (next.type === 'events.batch') {
          for (const item of next.payload.events) dispatch(item);
          return;
        }
        for (const handler of handlers) handler(next);
      };
      dispatch(event);
    };
    ws.onclose = () => {
      socket = null;
      connected.value = false;
      diagnostic('debug', { event: 'events.closed', component: 'events' });
      scheduleReconnect();
    };
  } catch {
    if (version !== connectionVersion) return;
    socket = null;
    diagnostic(connectionFailureReported ? 'debug' : 'warn', {
      event: 'events.connect_failed',
      component: 'events',
    });
    connectionFailureReported = true;
    scheduleReconnect();
  } finally {
    if (version === connectionVersion) connecting = false;
  }
};

if (import.meta.client) {
  watch(
    useAuth().status,
    (me) => {
      connectionVersion++;
      connecting = false;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = null;
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onclose = null;
        socket.close();
        socket = null;
      }
      connected.value = false;
      if (me) void connect();
    },
    { flush: 'sync' },
  );
}

export const useEvents = () => {
  // The first caller opens the socket. Later callers only subscribe.
  if (import.meta.client) {
    void connect();
  }
  return {
    connected: readonly(connected),
    on(handler: ServerEventHandler): () => void {
      handlers.add(handler);
      return () => handlers.delete(handler);
    },
    onReconnect(handler: () => void): () => void {
      reconnectHandlers.add(handler);
      return () => reconnectHandlers.delete(handler);
    },
  };
};
