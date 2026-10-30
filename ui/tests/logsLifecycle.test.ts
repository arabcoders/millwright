import { expect, test } from 'bun:test';
import * as Vue from 'vue';
import ts from 'typescript';
import * as logs from '../app/utils/logs';
import { LogStreamParser } from '../app/utils/logStream';
import type { LogEntry, LogsResponse } from '../app/types/logs';
import type { useLogs } from '../app/composables/useLogs';

const renderer = Vue.createRenderer({
  createElement: () => ({}),
  createText: () => ({}),
  createComment: () => ({}),
  insert() {},
  remove() {},
  setText() {},
  setElementText() {},
  patchProp() {},
  parentNode: () => null,
  nextSibling: () => null,
});
const record = (id: number): LogEntry => ({
  id,
  timestamp: '',
  level: 'warning',
  logger: 'test',
  message: String(id),
  fields: {},
});
const page = (ids: number[], next: number | null = null): LogsResponse => ({
  entries: ids.map(record),
  next_before: next,
  latest_id: 100,
  oldest_id: 1,
  truncated: false,
});
const mount = async (pages: (LogsResponse | Promise<LogsResponse>)[], visible = false) => {
  const urls: string[] = [];
  let stream: ReadableStreamDefaultController<Uint8Array> | null = null;
  const connected = Promise.withResolvers<undefined>();
  const disconnected = Promise.withResolvers<undefined>();
  let consumed: ReturnType<typeof Promise.withResolvers<undefined>> | null = null;
  const request = async (url: string, options: { signal?: AbortSignal }) => {
    urls.push(url);
    if (url.startsWith('/api/logs/stream')) {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          stream = controller;
          options.signal?.addEventListener('abort', () => controller.close(), { once: true });
        },
      });
      return {
        ok: true,
        body: {
          getReader: () => {
            const reader = body.getReader();
            return {
              read: () => {
                connected.resolve(undefined);
                // The next read starts after the previous chunk was processed.
                consumed?.resolve(undefined);
                consumed = null;
                return reader.read();
              },
              cancel: () => reader.cancel(),
              releaseLock: () => {
                reader.releaseLock();
                disconnected.resolve(undefined);
              },
            };
          },
        },
      };
    }
    return Response.json((await pages.shift()) ?? page([]));
  };
  const modules: Record<string, unknown> = {
    vue: Vue,
    '~/utils': { request, parse_api_error: () => 'Failed' },
    '~/utils/logStream': { LogStreamParser },
    '~/utils/logs': logs,
  };
  const compiled = ts.transpileModule(await Bun.file('app/composables/useLogs.ts').text(), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const exports: { useLogs?: typeof useLogs } = {};
  new Function('require', 'exports', 'document', compiled)(
    (name: string) => modules[name] ?? {},
    exports,
    {
      visibilityState: visible ? 'visible' : 'hidden',
      addEventListener() {},
      removeEventListener() {},
    },
  );
  let state!: ReturnType<typeof useLogs>;
  const root = {};
  renderer.render(
    Vue.h({
      setup() {
        state = exports.useLogs!();
        return () => null;
      },
    }),
    root,
  );
  return {
    state,
    urls,
    ready: connected.promise,
    async emit(entries: LogEntry[]) {
      await connected.promise;
      const processed = Promise.withResolvers<undefined>();
      consumed = processed;
      stream!.enqueue(
        new TextEncoder().encode(
          entries
            .map((entry) => `id: ${entry.id}\nevent: log_lines\ndata: ${JSON.stringify(entry)}\n\n`)
            .join(''),
        ),
      );
      await processed.promise;
    },
    close: async () => {
      renderer.render(null, root);
      if (stream) await disconnected.promise;
    },
  };
};

test('applies initial persisted levels and pages empty results', async () => {
  const view = await mount([page([], 50), page([20, 30])]);
  try {
    await view.state.start(['warning']);
    expect(view.urls[0]).toContain('levels=warning');
    expect(view.state.hasOlder.value).toBe(true);
    await view.state.loadOlder();
    expect(view.urls[1]).toContain('before=50');
    expect(view.state.entries.value.map((entry) => entry.id)).toEqual([20, 30]);
    expect(view.state.live.value).toBe(false);
  } finally {
    await view.close();
  }
});

test('keeps older history accessible after live eviction', async () => {
  const view = await mount(
    [
      { ...page([100]), entries: [{ ...record(100), logger: 'server.initial' }] },
      {
        ...page([400, 401], 400),
        entries: [
          { ...record(400), logger: 'server.older' },
          { ...record(401), logger: 'server.older' },
        ],
      },
    ],
    true,
  );
  try {
    await view.state.start(['warning']);
    await view.emit(Array.from({ length: 700 }, (_, index) => record(index + 101)));
    expect(view.state.entries.value.length).toBe(logs.LOG_WINDOW);
    expect(view.state.entries.value[0]?.id).toBe(501);
    expect(view.state.loggerNames.value).toEqual(['server.initial', 'test']);
    expect(view.state.hasOlder.value).toBe(true);
    await view.state.loadOlder();
    expect(view.urls.at(-1)).toContain('before=501');
    expect(view.state.entries.value[0]?.id).toBe(400);
    expect(view.state.entries.value.length).toBe(logs.LOG_WINDOW);
    expect(view.state.loggerNames.value).toEqual(['server.initial', 'server.older', 'test']);
  } finally {
    await view.close();
  }
});

test('pages exact logger history', async () => {
  const view = await mount([page([100]), page([], 50), page([]), page([]), page([]), page([])]);
  try {
    await view.state.start(['warning']);
    await view.state.searchHistory(' failure ', ['warning'], ' server.worker ');
    await view.state.loadOlder();
    await view.state.refreshHistory();
    const refreshed = Promise.withResolvers<undefined>();
    const stop = Vue.watch(
      view.state.loading,
      (loading) => {
        if (!loading) refreshed.resolve(undefined);
      },
      { flush: 'sync' },
    );
    try {
      view.state.setLive(true);
      await refreshed.promise;
    } finally {
      stop();
    }
    for (const url of view.urls.slice(1)) {
      const params = new URL(url, 'http://localhost').searchParams;
      expect(params.get('logger')).toBe('server.worker');
      expect(params.get('search')).toBe('failure');
      expect(params.get('levels')).toBe('warning');
    }
    expect(view.urls[2]).toContain('before=50');
    await view.state.searchHistory('', ['warning'], '');
    expect(new URL(view.urls.at(-1)!, 'http://localhost').searchParams.has('logger')).toBe(false);
  } finally {
    await view.close();
  }
});

test('filters exact live loggers', async () => {
  const view = await mount([page([]), page([]), page([])], true);
  try {
    await view.state.start();
    await view.ready;
    await view.state.searchHistory('failure', ['warning'], 'server.worker');
    await view.emit([
      { ...record(101), logger: 'server.worker', message: 'Failure' },
      { ...record(102), logger: 'server.worker.child', message: 'failure' },
      { ...record(103), logger: 'Server.worker', message: 'failure' },
      { ...record(104), logger: 'other', message: 'server.worker failure' },
      { ...record(105), logger: 'server.worker', message: 'failure', level: 'info' },
      { ...record(106), logger: 'server.worker', message: 'success' },
    ]);
    expect(view.state.entries.value.map((entry) => entry.id)).toEqual([101]);
    await view.state.searchHistory('failure', ['warning'], '');
    await view.emit([{ ...record(107), logger: 'other', message: 'failure' }]);
    expect(view.state.entries.value.map((entry) => entry.id)).toEqual([101, 107]);
  } finally {
    await view.close();
  }
});

test('filters retained live entries', async () => {
  const history = Promise.withResolvers<LogsResponse>();
  const view = await mount([page([]), history.promise, page([])], true);
  let search: Promise<void> | undefined;
  try {
    await view.state.start();
    await view.emit([
      { ...record(101), logger: 'server.worker' },
      { ...record(102), logger: 'other' },
    ]);
    search = view.state.searchHistory('', [], 'server.worker');
    await view.emit([
      { ...record(103), logger: 'server.worker' },
      { ...record(104), logger: 'server.worker.child' },
    ]);
    history.resolve(page([]));
    await search;
    expect(view.state.entries.value.map((entry) => entry.id)).toEqual([101, 103]);
    await view.state.refreshHistory();
    expect(view.state.entries.value.map((entry) => entry.id)).toEqual([101, 103]);
  } finally {
    history.resolve(page([]));
    await search;
    await view.close();
  }
});

test('retains observed logger suggestions', async () => {
  const initial = {
    ...page([]),
    entries: [
      { ...record(99), logger: 'server.beta' },
      { ...record(100), logger: 'server.alpha' },
    ],
  };
  const view = await mount([initial, page([]), page([])], true);
  try {
    await view.state.start(['warning']);
    await view.ready;
    expect(view.state.loggerNames.value).toEqual(['server.alpha', 'server.beta']);
    await view.state.searchHistory('', ['warning'], 'server.alpha');
    expect(view.state.entries.value).toEqual([]);
    expect(view.state.loggerNames.value).toEqual(['server.alpha', 'server.beta']);
    const historyRequests = view.urls.filter((url) => !url.startsWith('/api/logs/stream')).length;
    await view.emit([
      { ...record(101), logger: 'server.alpha' },
      { ...record(102), logger: 'server.gamma', level: 'info' },
      { ...record(103), logger: 'server.beta' },
      { ...record(104), logger: 'Server.alpha' },
    ]);
    expect(view.state.entries.value.map((entry) => entry.id)).toEqual([101]);
    expect(view.state.loggerNames.value).toEqual([
      'Server.alpha',
      'server.alpha',
      'server.beta',
      'server.gamma',
    ]);
    expect(view.urls.filter((url) => !url.startsWith('/api/logs/stream')).length).toBe(
      historyRequests,
    );
    await view.state.searchHistory('', ['warning'], 'server.beta');
    expect(view.urls.at(-1)).toContain('logger=server.beta');
    expect(view.state.loggerNames.value).toContain('server.alpha');
    expect(view.state.loggerNames.value).toContain('server.beta');
  } finally {
    await view.close();
  }
});
