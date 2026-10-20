import { computed, onBeforeUnmount, ref } from 'vue';
import { parse_api_error, request } from '~/utils';
import { LogStreamParser } from '~/utils/logStream';
import { appendLog, LOG_WINDOW } from '~/utils/logs';
import type { LogEntry, LogsResponse } from '~/types/logs';

export const useLogs = () => {
  const entries = ref<LogEntry[]>([]);
  const observedLoggers = ref(new Set<string>());
  const loggerNames = computed(() => [...observedLoggers.value].sort());
  const loading = ref(false);
  const error = ref<string | null>(null);
  const truncated = ref(false);
  const live = ref(true);
  const hasOlder = ref(true);
  const olderCursor = ref<number | null>(null);
  let streamController: AbortController | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectAttempt = 0;
  let mounted = true;
  let listening = false;
  let requestVersion = 0;
  let streamLastId = 0;
  let searchText = '';
  let searchLogger = '';
  let searchLevels: string[] = [];
  let ids = new Set<number>();
  const latestId = computed(() => entries.value.at(-1)?.id ?? 0);

  const mergeLive = (entry: LogEntry): void => {
    observedLoggers.value.add(entry.logger);
    if (searchLogger && entry.logger !== searchLogger) return;
    if (
      searchLevels.length &&
      !searchLevels.includes(entry.level === 'critical' ? 'error' : entry.level)
    )
      return;
    if (searchText && !JSON.stringify(entry).toLowerCase().includes(searchText.toLowerCase()))
      return;
    if (appendLog(entries.value, ids, entry)) {
      olderCursor.value = entries.value[0]?.id ?? null;
      hasOlder.value = true;
    }
  };

  const fetchHistory = async (older = false): Promise<void> => {
    const version = ++requestVersion;
    loading.value = true;
    const params = new URLSearchParams({ limit: '100' });
    if (searchText) params.set('search', searchText);
    if (searchLogger) params.set('logger', searchLogger);
    if (searchLevels.length) params.set('levels', searchLevels.join(','));
    if (older && olderCursor.value !== null) params.set('before', String(olderCursor.value));
    try {
      const response = await request(`/api/logs?${params}`, { timeout: 10 });
      if (!response.ok)
        throw new Error(await parse_api_error(await response.json().catch(() => null)));
      const body = (await response.json()) as LogsResponse;
      if (!mounted || version !== requestVersion) return;
      for (const entry of body.entries) observedLoggers.value.add(entry.logger);
      truncated.value = body.truncated;
      const retained = entries.value.filter(
        (entry) =>
          (older || entry.id > body.latest_id) && (!searchLogger || entry.logger === searchLogger),
      );
      entries.value = [
        ...new Map([...body.entries, ...retained].map((entry) => [entry.id, entry])).values(),
      ].sort((left, right) => left.id - right.id);
      const overflow = entries.value.length > LOG_WINDOW;
      entries.value = older ? entries.value.slice(0, LOG_WINDOW) : entries.value.slice(-LOG_WINDOW);
      ids = new Set(entries.value.map((entry) => entry.id));
      if (!older && !streamController) streamLastId = body.latest_id;
      olderCursor.value = body.next_before;
      hasOlder.value = body.next_before !== null;
      if (!older && overflow) {
        olderCursor.value = entries.value[0]?.id ?? null;
        hasOlder.value = true;
      }
      error.value = null;
    } catch (cause) {
      if (mounted && version === requestVersion)
        error.value = cause instanceof Error ? cause.message : 'Unable to load server logs.';
    } finally {
      if (mounted && version === requestVersion) loading.value = false;
    }
  };

  const scheduleReconnect = (): void => {
    if (!mounted || !live.value || reconnectTimer || document.visibilityState !== 'visible') return;
    reconnectTimer = setTimeout(
      () => {
        reconnectTimer = null;
        void connectStream();
      },
      Math.min(15_000, 500 * 2 ** reconnectAttempt++),
    );
  };

  const connectStream = async (): Promise<void> => {
    if (!mounted || !live.value || streamController || document.visibilityState !== 'visible')
      return;
    const controller = new AbortController();
    streamController = controller;
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    try {
      const response = await request(
        `/api/logs/stream${streamLastId ? `?after=${streamLastId}` : ''}`,
        { signal: controller.signal, timeout: 0 },
      );
      if (!response.ok || !response.body) throw new Error('Log stream unavailable.');
      reconnectAttempt = 0;
      error.value = null;
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = new LogStreamParser();
      while (mounted && live.value && !controller.signal.aborted) {
        const result = await reader.read();
        if (result.done) break;
        for (const event of parser.feed(decoder.decode(result.value, { stream: true }))) {
          if (event.event !== 'log_lines') continue;
          try {
            const entry = JSON.parse(event.data) as LogEntry;
            if (entry.id <= streamLastId) continue;
            streamLastId = Math.max(entry.id, event.id ?? 0);
            mergeLive(entry);
          } catch {
            /* malformed server event */
          }
        }
      }
    } catch (cause) {
      if (!controller.signal.aborted && mounted)
        error.value = cause instanceof Error ? cause.message : 'Log stream disconnected.';
    } finally {
      try {
        await reader?.cancel();
      } catch {
        // The server may already have closed the stream.
      } finally {
        reader?.releaseLock();
      }
      if (streamController === controller) streamController = null;
      if (mounted && live.value && !controller.signal.aborted) scheduleReconnect();
    }
  };

  const setLive = (value: boolean): void => {
    live.value = value;
    if (!value) {
      streamController?.abort();
      streamController = null;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = null;
      return;
    }
    void fetchHistory().then(connectStream);
  };
  const handleVisibility = (): void => {
    if (document.visibilityState === 'visible' && live.value) void connectStream();
    else if (document.visibilityState !== 'visible') {
      streamController?.abort();
      streamController = null;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };
  const start = async (levels: string[] = []): Promise<void> => {
    searchLevels = [...levels];
    await fetchHistory();
    if (!mounted) return;
    if (!listening) {
      document.addEventListener('visibilitychange', handleVisibility);
      listening = true;
    }
    if (live.value && document.visibilityState === 'visible') void connectStream();
  };
  const refreshHistory = (): Promise<void> => fetchHistory();
  const searchHistory = async (
    query: string,
    levels: string[] = [],
    logger = '',
  ): Promise<void> => {
    searchText = query.trim();
    searchLogger = logger.trim();
    searchLevels = [...levels];
    olderCursor.value = null;
    hasOlder.value = true;
    await fetchHistory();
  };
  const loadOlder = (): Promise<void> => {
    setLive(false);
    return fetchHistory(true);
  };
  onBeforeUnmount(() => {
    mounted = false;
    requestVersion += 1;
    streamController?.abort();
    if (reconnectTimer) clearTimeout(reconnectTimer);
    document.removeEventListener('visibilitychange', handleVisibility);
  });
  return {
    entries,
    loggerNames,
    loading,
    error,
    truncated,
    live,
    hasOlder,
    start,
    refreshHistory,
    searchHistory,
    loadOlder,
    setLive,
    latestId,
  };
};
