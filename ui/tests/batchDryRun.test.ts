import { expect, test } from 'bun:test';
import { computed, ref, watch } from 'vue';
import ts from 'typescript';
import type {
  BatchDryRunReport,
  BatchDryRunRow,
  BatchDryRunScope,
  FlowRow,
  MediaFileRow,
} from '../app/types/api';
import type { useBatchDryRun } from '../app/composables/useBatchDryRun';

const source = await Bun.file(
  new URL('../app/composables/useBatchDryRun.ts', import.meta.url),
).text();
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

const row: BatchDryRunRow = {
  file_id: 12,
  root: '/media/Movies',
  file: 'Drama/Film.mkv',
  status: 'deferred',
  reason: 'First stage selected.',
  deferred_output: true,
  deferred_encoder: false,
};
const report = (state: BatchDryRunReport['state'] = 'running', id = 'run'): BatchDryRunReport => ({
  id,
  library_id: 7,
  library_name: 'Movies',
  file_ids: null,
  flow_id: 3,
  flow_name: 'Archive',
  flow_version: 4,
  created_at: '',
  finished_at: null,
  expires_at: null,
  state,
  error: '',
  processed: state === 'preparing' ? 0 : state === 'completed' ? 101 : 1,
  total: 101,
  captured: state === 'preparing' ? 0 : 101,
  snapshot_complete: state !== 'preparing',
  counts: state === 'preparing' ? {} : { deferred: state === 'completed' ? 101 : 1 },
  items: state === 'preparing' ? [] : [row],
  pagination: { limit: 50, offset: 0, total: 101 },
});
const flow: FlowRow = {
  id: 3,
  name: 'Archive',
  version: 4,
  created_at: '',
  updated_at: '',
  definition: { entry: 'x', nodes: [], edges: [] },
};
const scope: BatchDryRunScope = {
  library: {
    id: 7,
    name: 'Movies',
    roots: ['/media/Movies'],
    watch: false,
    scan_on_start: false,
    suspended: false,
    created_at: '',
    updated_at: '',
    cooldown_s: 0,
    scan_interval_s: null,
    mkv_stats: false,
    healthcheck_enabled: false,
    processing_cutoff: {},
    trash_mode: 'none',
    trash_retention_days: 30,
    flow_id: 3,
    schedule: null,
  },
};
const file: MediaFileRow = {
  id: 12,
  library_id: 7,
  path: '/media/Movies/Drama/Film.mkv',
  size: 1000,
  container: 'mkv',
  video_codec: 'h264',
  width: 1920,
  height: 1080,
  duration_s: 60,
  bit_rate: 1000,
  medium: 'video',
  modified_at: '2026-01-01T00:00:00Z',
  cooldown_until: null,
  created_at: '',
  updated_at: '',
  transcode_status: 'transcoded',
  health_status: 'corrupt',
  active_job_kind: null,
  active_job_status: null,
  active_job_id: null,
  active_job_progress: null,
  active_job_updated_at: null,
};
const pendingResponse = () => {
  let resolve!: (response: Response) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Response>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
};

const setup = () => {
  const calls: Array<{ url: string; options?: RequestInit }> = [];
  const timers = new Map<number, { callback: () => void; delay: number }>();
  let timerId = 0;
  let respond = async (_url: string, _options?: RequestInit): Promise<Response> =>
    Response.json([]);
  const exports: { useBatchDryRun?: typeof useBatchDryRun } = {};
  new Function('require', 'exports', 'setTimeout', 'clearTimeout', compiled)(
    (name: string) => {
      if (name === 'vue') return { computed, ref };
      if (name === '~/utils')
        return {
          request: (url: string, options?: RequestInit) => {
            calls.push({ url, options });
            return respond(url, options);
          },
          ensure_api_success: async (response: Response) => {
            if (!response.ok) throw new Error((await response.json()).detail);
          },
        };
      throw new Error(`Unexpected import: ${name}`);
    },
    exports,
    (callback: () => void, delay: number) => {
      timers.set(++timerId, { callback, delay });
      return timerId;
    },
    (id?: number) => {
      if (id) timers.delete(id);
    },
  );
  const store = exports.useBatchDryRun!();
  return {
    store,
    calls,
    timers,
    respond: (next: typeof respond) => {
      respond = next;
    },
    saved: (state: BatchDryRunReport['state'] = 'running', id = 'run') => {
      respond = async (url) =>
        Response.json(
          new URL(url, 'http://test').pathname.endsWith('/dry-runs')
            ? [report(state, id)]
            : report(state, id),
        );
    },
    tick: async () => {
      const [id, timer] = [...timers.entries()][0]!;
      timers.delete(id);
      const completed = Promise.withResolvers<undefined>();
      const stop = watch(
        store.loading,
        (loading) => {
          if (!loading) completed.resolve(undefined);
        },
        { flush: 'sync' },
      );
      try {
        timer.callback();
        if (store.loading.value) await completed.promise;
      } finally {
        stop();
      }
      expect(store.loading.value).toBe(false);
    },
  };
};

test('starts whole library', async () => {
  const env = setup();
  await env.store.open(scope);
  env.respond(async () => Response.json(report('preparing')));
  await env.store.start(flow);
  expect(env.calls.map(({ url }) => url)).toEqual([
    '/api/libraries/7/dry-runs',
    '/api/libraries/7/dry-runs',
  ]);
  expect(env.calls[1]?.options?.method).toBe('POST');
  expect(JSON.parse(String(env.calls[1]?.options?.body))).toEqual({
    flow_id: 3,
    expected_version: 4,
  });
  expect(env.store.active.value).toBe(true);
  expect([...env.timers.values()].map(({ delay }) => delay)).toEqual([1000]);
  env.store.stop();
  expect(env.timers.size).toBe(0);
});

test('starts selected files', async () => {
  const env = setup();
  const files = [{ ...file, id: 13, library_id: 8 }, file, file];
  await env.store.open({ files });
  expect(env.calls.map(({ url }) => url)).toEqual(['/api/files/dry-runs?file_ids=12&file_ids=13']);
  expect(env.calls[0]?.options?.method).toBeUndefined();
  files.splice(0);
  env.respond(async () =>
    Response.json({
      ...report('preparing'),
      library_id: null,
      library_name: '',
      file_ids: [12, 13],
    }),
  );
  await env.store.start(flow);
  expect(env.calls.at(-1)?.url).toBe('/api/files/dry-runs');
  expect(JSON.parse(String(env.calls.at(-1)?.options?.body))).toEqual({
    flow_id: 3,
    expected_version: 4,
    file_ids: [12, 13],
  });
  expect(env.store.active.value).toBe(true);
  env.store.stop();
  expect(env.timers.size).toBe(0);
});

test('reopens selected report', async () => {
  const env = setup();
  const saved = { ...report('completed'), library_id: null, library_name: '', file_ids: [12, 13] };
  env.respond(async (url) =>
    Response.json(new URL(url, 'http://test').pathname.endsWith('/dry-runs') ? [saved] : saved),
  );
  await env.store.open({ files: [file, { ...file, id: 13, library_id: 8 }] });
  expect(env.calls.map(({ url }) => url)).toEqual([
    '/api/files/dry-runs?file_ids=12&file_ids=13',
    '/api/files/dry-runs/run?limit=50&offset=0',
  ]);
  await env.store.changePage(2);
  expect(env.calls.at(-1)?.url).toBe('/api/files/dry-runs/run?limit=50&offset=50');
  env.respond(async () => new Response('root,file,status\n'));
  expect(await (await env.store.exportCsv())?.text()).toBe('root,file,status\n');
  expect(env.calls.at(-1)?.url).toBe('/api/files/dry-runs/run/export.csv');
  env.store.stop();
});

test('cancels selected report', async () => {
  const env = setup();
  const saved = { ...report(), library_id: null, library_name: '', file_ids: [12, 13] };
  env.respond(async (url) =>
    Response.json(new URL(url, 'http://test').pathname.endsWith('/dry-runs') ? [saved] : saved),
  );
  await env.store.open({ files: [file, { ...file, id: 13 }] });
  env.respond(async () => Response.json({ ...saved, state: 'cancelled' }));
  await env.store.cancel();
  expect(env.calls.at(-2)?.url).toBe('/api/files/dry-runs/run/cancel');
  expect(env.calls.at(-2)?.options?.method).toBe('POST');
  expect(env.calls.at(-1)?.url).toBe('/api/files/dry-runs/run?limit=50&offset=0');
  expect(env.store.report.value?.items).toEqual([row]);
  expect(env.store.active.value).toBe(false);
  expect(env.timers.size).toBe(0);
  env.store.stop();
});

test('selection ignores stale', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const polling = env.store.refresh();
  const signal = env.calls.at(-1)?.options?.signal;
  env.respond(async () => Response.json([]));
  await env.store.open({ files: [file, { ...file, id: 13 }] });
  response.resolve(Response.json(report()));
  await polling;
  expect(signal?.aborted).toBe(true);
  expect(env.store.report.value).toBeNull();
  expect(env.store.error.value).toBe('');
  expect(env.timers.size).toBe(0);
  env.store.stop();
});

test('polls paged report', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  expect(env.calls.map(({ url }) => url)).toEqual([
    '/api/libraries/7/dry-runs',
    '/api/libraries/7/dry-runs/run?limit=50&offset=0',
  ]);
  env.respond(async (url) =>
    Response.json({
      ...report(),
      pagination: { limit: 50, offset: url.includes('offset=50') ? 50 : 0, total: 101 },
    }),
  );
  await env.store.changePage(2);
  expect(env.store.page.value).toBe(2);
  expect(env.store.report.value?.pagination.offset).toBe(50);
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/run?limit=50&offset=50');
  env.respond(async () => Response.json(report('completed')));
  await env.tick();
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/run?limit=50&offset=50');
  expect(env.store.active.value).toBe(false);
  expect(env.timers.size).toBe(0);
  env.store.stop();
});

test('reopens latest report', async () => {
  const env = setup();
  env.saved('completed', 'old');
  await env.store.open(scope);
  env.store.stop();
  env.respond(async (url) =>
    Response.json(
      url.endsWith('/dry-runs')
        ? [report('running', 'latest'), report('completed', 'old')]
        : report('running', 'latest'),
    ),
  );
  await env.store.open(scope);
  expect(env.calls.at(-2)?.url).toBe('/api/libraries/7/dry-runs');
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/latest?limit=50&offset=0');
  expect(env.store.report.value?.id).toBe('latest');
  expect(env.store.active.value).toBe(true);
  expect(env.timers.size).toBe(1);
  env.store.stop();
});

test('changes page size', async () => {
  const env = setup();
  const large = { ...report(), total: 2500 };
  env.respond(async (url) => {
    if (url.endsWith('/dry-runs')) return Response.json([large]);
    const query = new URL(url, 'http://test').searchParams;
    return Response.json({
      ...large,
      pagination: {
        limit: Number(query.get('limit')),
        offset: Number(query.get('offset')),
        total: 2500,
      },
    });
  });
  await env.store.open(scope);
  await env.store.changePage(4);
  expect(env.store.page.value).toBe(4);
  await env.store.changePage(1, 1000);
  expect(env.store.page.value).toBe(1);
  expect(env.store.pageSize.value).toBe(1000);
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/run?limit=1000&offset=0');
  expect(env.store.report.value?.pagination).toEqual({ limit: 1000, offset: 0, total: 2500 });
  await env.store.changePage(3);
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/run?limit=1000&offset=2000');
  const before = env.calls.length;
  await env.store.changePage(4);
  expect(env.calls).toHaveLength(before);
  expect(env.store.page.value).toBe(3);
  await env.tick();
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/run?limit=1000&offset=2000');
  env.store.stop();
  await env.store.open(scope);
  expect(env.store.pageSize.value).toBe(1000);
  expect(env.store.page.value).toBe(1);
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/run?limit=1000&offset=0');
  env.store.stop();
});

test('rejects invalid sizes', async () => {
  const env = setup();
  env.saved('completed');
  await env.store.open(scope);
  const before = env.calls.length;
  for (const size of [0, -1, 1001, 50.5, NaN]) await env.store.changePage(1, size);
  expect(env.calls).toHaveLength(before);
  expect(env.store.pageSize.value).toBe(50);
  expect(env.store.page.value).toBe(1);
  env.store.stop();
});

test('filters whole report', async () => {
  for (const target of [scope, { files: [file] }]) {
    const env = setup();
    env.saved();
    await env.store.open(target);
    await env.store.changePage(3);
    env.respond(async (url) => {
      const query = new URL(url, 'http://test').searchParams;
      return Response.json({
        ...report(),
        pagination: {
          limit: Number(query.get('limit')),
          offset: Number(query.get('offset')),
          total: query.has('status') ? 61 : 101,
        },
      });
    });
    try {
      await env.store.changeStatuses(['matched', 'deferred']);
      expect(env.store.page.value).toBe(1);
      expect(env.calls.at(-1)?.url).toEndWith('?limit=50&offset=0&status=matched&status=deferred');
      expect(env.store.report.value?.total).toBe(101);
      await env.store.changePage(2);
      expect(env.calls.at(-1)?.url).toEndWith('?limit=50&offset=50&status=matched&status=deferred');
      const before = env.calls.length;
      await env.store.changePage(3);
      expect(env.calls).toHaveLength(before);
      await env.tick();
      expect(env.store.statuses.value).toEqual(['matched', 'deferred']);
      expect(env.calls.at(-1)?.url).toEndWith('?limit=50&offset=50&status=matched&status=deferred');
      await env.store.changeStatuses([]);
      expect(env.store.page.value).toBe(1);
      expect(env.calls.at(-1)?.url).toEndWith('?limit=50&offset=0');
    } finally {
      env.store.stop();
    }
  }
});

test('filters replace reads', async () => {
  const env = setup();
  env.saved('completed');
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const stale = env.store.refresh();
  const signal = env.calls.at(-1)?.options?.signal;
  env.respond(async () =>
    Response.json({
      ...report('completed'),
      items: [],
      pagination: { limit: 50, offset: 0, total: 0 },
    }),
  );
  try {
    await env.store.changeStatuses(['error']);
    expect(signal?.aborted).toBe(true);
    response.resolve(Response.json(report('completed')));
    await stale;
    expect(env.store.report.value?.items).toEqual([]);
    expect(env.store.report.value?.pagination.total).toBe(0);
    expect(env.store.error.value).toBe('');
  } finally {
    response.resolve(Response.json(report('completed')));
    await stale;
    env.store.stop();
  }
});

test('shrinking results repage', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  await env.store.changeStatuses(['not_evaluated']);
  await env.store.changePage(3);
  env.respond(async (url) => {
    const offset = Number(new URL(url, 'http://test').searchParams.get('offset'));
    return Response.json({
      ...report(),
      items: offset ? [] : [row],
      pagination: { limit: 50, offset, total: 1 },
    });
  });
  try {
    await env.store.refresh();
    expect(env.calls.at(-2)?.url).toEndWith('?limit=50&offset=100&status=not_evaluated');
    expect(env.calls.at(-1)?.url).toEndWith('?limit=50&offset=0&status=not_evaluated');
    expect(env.store.page.value).toBe(1);
    expect(env.store.report.value?.items).toEqual([row]);
    expect(env.store.report.value?.total).toBe(101);
  } finally {
    env.store.stop();
  }
});

test('rerun keeps filters', async () => {
  const env = setup();
  env.saved('completed');
  await env.store.open(scope);
  await env.store.changeStatuses(['error']);
  env.respond(async (_url, options) =>
    Response.json(
      options?.method === 'POST'
        ? report('preparing', 'next')
        : {
            ...report('completed', 'next'),
            items: [],
            pagination: { limit: 50, offset: 0, total: 0 },
          },
    ),
  );
  try {
    await env.store.start(flow);
    expect(env.calls.at(-1)?.url).toBe(
      '/api/libraries/7/dry-runs/next?limit=50&offset=0&status=error',
    );
    expect(env.store.statuses.value).toEqual(['error']);
    expect(env.store.report.value?.items).toEqual([]);
    env.respond(async () => new Response('root,file,status\n'));
    expect(await (await env.store.exportCsv())?.text()).toBe('root,file,status\n');
    expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/next/export.csv');
  } finally {
    env.store.stop();
  }
});

test('close aborts resizing', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const resizing = env.store.changePage(1, 500);
  const signal = env.calls.at(-1)?.options?.signal;
  env.store.stop();
  expect(signal?.aborted).toBe(true);
  response.resolve(
    Response.json({ ...report(), pagination: { limit: 500, offset: 0, total: 101 } }),
  );
  await resizing;
  expect(env.store.report.value?.pagination.limit).toBe(50);
  expect(env.store.loading.value).toBe(false);
  expect(env.timers.size).toBe(0);
});

test('cancel keeps size', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  await env.store.changePage(1, 100);
  await env.store.changePage(2);
  env.respond(async (url) =>
    Response.json({
      ...report('cancelled'),
      pagination: {
        limit: url.endsWith('/cancel') ? 50 : 100,
        offset: url.endsWith('/cancel') ? 0 : 100,
        total: 101,
      },
    }),
  );
  await env.store.cancel();
  expect(env.store.pageSize.value).toBe(100);
  expect(env.store.page.value).toBe(2);
  expect(env.store.report.value?.pagination).toEqual({ limit: 100, offset: 100, total: 101 });
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/run?limit=100&offset=100');
  expect(env.timers.size).toBe(0);
  env.store.stop();
});

test('reopens expired report', async () => {
  const env = setup();
  env.saved('completed');
  await env.store.open(scope);
  env.store.stop();
  env.respond(async () => Response.json([]));
  await env.store.open(scope);
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs');
  expect(env.store.report.value).toBeNull();
  expect(env.store.active.value).toBe(false);
  expect(env.store.error.value).toBe('');
  expect(env.timers.size).toBe(0);
  expect(env.calls.every(({ options }) => !options?.method)).toBe(true);
  env.store.stop();
});

test('expiry clears polling', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  env.respond(async () => Response.json({ detail: 'Dry run not found.' }, { status: 404 }));
  await env.tick();
  expect(env.store.report.value).toBeNull();
  expect(env.store.active.value).toBe(false);
  expect(env.store.error.value).toBe('');
  expect(env.timers.size).toBe(0);
  env.respond(async () => Response.json(report('preparing', 'next')));
  await env.store.start(flow);
  expect(env.calls.at(-1)?.options?.method).toBe('POST');
  expect(env.store.report.value?.id).toBe('next');
  env.store.stop();
});

test('expiry during reopen', async () => {
  const env = setup();
  env.respond(async (url) =>
    url.endsWith('/dry-runs')
      ? Response.json([report()])
      : Response.json({ detail: 'Dry run not found.' }, { status: 404 }),
  );
  await env.store.open(scope);
  expect(env.store.report.value).toBeNull();
  expect(env.store.error.value).toBe('');
  expect(env.store.active.value).toBe(false);
  expect(env.timers.size).toBe(0);
  env.store.stop();
});

test('close ignores polling', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const polling = env.store.refresh();
  const signal = env.calls.at(-1)?.options?.signal;
  env.store.stop();
  expect(signal?.aborted).toBe(true);
  response.resolve(Response.json(report('completed', 'stale')));
  await polling;
  expect(env.store.report.value?.id).toBe('run');
  expect(env.store.active.value).toBe(true);
  expect(env.timers.size).toBe(0);
  expect(env.calls.every(({ options }) => !options?.method)).toBe(true);
});

test('close aborts list', async () => {
  const env = setup();
  const response = pendingResponse();
  env.respond(() => response.promise);
  const opening = env.store.open(scope);
  const signal = env.calls.at(-1)?.options?.signal;
  env.store.stop();
  response.resolve(Response.json([report()]));
  await opening;
  expect(signal?.aborted).toBe(true);
  expect(env.calls).toHaveLength(1);
  expect(env.store.report.value).toBeNull();
  expect(env.timers.size).toBe(0);
});

test('close preserves start', async () => {
  const env = setup();
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const starting = env.store.start(flow);
  env.store.stop();
  expect(env.calls.at(-1)?.options?.signal).toBeUndefined();
  expect(env.store.starting.value).toBe(true);
  response.resolve(Response.json(report()));
  await starting;
  expect(env.store.starting.value).toBe(false);
  expect(env.store.report.value).toBeNull();
  expect(env.timers.size).toBe(0);
});

test('pending start guards', async () => {
  const env = setup();
  env.saved('completed');
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const starting = env.store.start(flow);
  const before = env.calls.length;
  await Promise.all([
    env.store.changePage(2),
    env.store.changePage(1, 1000),
    env.store.refresh(),
    env.store.start(flow),
    env.store.cancel(),
  ]);
  expect(await env.store.exportCsv()).toBeNull();
  expect(env.calls).toHaveLength(before);
  expect(env.store.page.value).toBe(1);
  expect(env.store.pageSize.value).toBe(50);
  expect(env.store.starting.value).toBe(true);
  response.resolve(Response.json(report()));
  await starting;
  expect(env.store.report.value?.state).toBe('running');
  env.store.stop();
});

test('cancel preserves results', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  await env.store.changePage(2);
  expect(await env.store.exportCsv()).toBeNull();
  env.respond(async (url) =>
    Response.json({
      ...report('cancelled'),
      pagination: { limit: 50, offset: url.endsWith('/cancel') ? 0 : 50, total: 101 },
    }),
  );
  await env.store.cancel();
  expect(env.calls.at(-2)?.url).toBe('/api/libraries/7/dry-runs/run/cancel');
  expect(env.calls.at(-2)?.options?.method).toBe('POST');
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/run?limit=50&offset=50');
  expect(env.store.report.value?.state).toBe('cancelled');
  expect(env.store.report.value?.items).toEqual([row]);
  expect(env.store.page.value).toBe(2);
  expect(env.timers.size).toBe(0);
  env.store.stop();
});

test('pending cancel guards', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond((url) =>
    url.endsWith('/cancel')
      ? response.promise
      : Promise.resolve(Response.json(report('cancelled'))),
  );
  const cancelling = env.store.cancel();
  const before = env.calls.length;
  await Promise.all([
    env.store.changePage(2),
    env.store.changePage(1, 1000),
    env.store.refresh(),
    env.store.start(flow),
    env.store.cancel(),
  ]);
  expect(await env.store.exportCsv()).toBeNull();
  expect(env.calls).toHaveLength(before);
  expect(env.store.cancelling.value).toBe(true);
  expect(env.store.pageSize.value).toBe(50);
  expect(env.timers.size).toBe(0);
  response.resolve(Response.json(report('cancelling')));
  await cancelling;
  expect(env.store.report.value?.state).toBe('cancelled');
  env.store.stop();
});

test('cancel interrupts poll', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const polling = env.store.refresh();
  const signal = env.calls.at(-1)?.options?.signal;
  env.saved('cancelled');
  await env.store.cancel();
  expect(signal?.aborted).toBe(true);
  response.resolve(Response.json(report('running')));
  await polling;
  expect(env.store.report.value?.state).toBe('cancelled');
  expect(env.timers.size).toBe(0);
  env.store.stop();
});

test('exports completed report', async () => {
  const env = setup();
  env.saved('completed');
  await env.store.open(scope);
  env.respond(async () => new Response('root,file,status\n'));
  const csv = await env.store.exportCsv();
  expect(await csv?.text()).toBe('root,file,status\n');
  expect(env.calls.at(-1)?.url).toBe('/api/libraries/7/dry-runs/run/export.csv');
  expect(env.calls.at(-1)?.options?.headers).toEqual({ Accept: 'text/csv' });
  expect(env.store.exporting.value).toBe(false);
  env.store.stop();
});

test('incomplete capture blocks', async () => {
  const env = setup();
  const incomplete = {
    ...report('failed'),
    captured: 25,
    snapshot_complete: false,
    processed: 0,
    error: 'Capture failed.',
  };
  env.respond(async (url) => Response.json(url.endsWith('/dry-runs') ? [incomplete] : incomplete));
  await env.store.open(scope);
  const before = env.calls.length;
  expect(env.store.report.value?.total).toBe(101);
  expect(env.store.report.value?.captured).toBe(25);
  expect(env.store.report.value?.snapshot_complete).toBe(false);
  expect(env.store.report.value?.error).toBe('Capture failed.');
  expect(await env.store.exportCsv()).toBeNull();
  expect(env.calls).toHaveLength(before);
  expect(env.store.error.value).toBe('');
  env.store.stop();
});

test('poll preserves scope', async () => {
  const env = setup();
  const preparing = {
    ...report('preparing'),
    processed: 0,
    captured: 25,
    snapshot_complete: false,
  };
  env.respond(async (url) => Response.json(url.endsWith('/dry-runs') ? [preparing] : preparing));
  await env.store.open(scope);
  expect(env.store.active.value).toBe(true);
  expect(env.store.report.value?.total).toBe(101);
  expect(env.store.report.value?.captured).toBe(25);
  env.saved('running');
  await env.tick();
  expect(env.store.report.value?.state).toBe('running');
  expect(env.store.report.value?.total).toBe(101);
  expect(env.store.report.value?.snapshot_complete).toBe(true);
  env.store.stop();
});

test('close aborts export', async () => {
  const env = setup();
  env.saved('completed');
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const exporting = env.store.exportCsv();
  const signal = env.calls.at(-1)?.options?.signal;
  env.store.stop();
  response.resolve(new Response('stale export'));
  expect(await exporting).toBeNull();
  expect(signal?.aborted).toBe(true);
  expect(env.store.exporting.value).toBe(false);
  expect(env.timers.size).toBe(0);
});

test('pending export guards', async () => {
  const env = setup();
  env.saved('completed');
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const exporting = env.store.exportCsv();
  const before = env.calls.length;
  await Promise.all([
    env.store.changePage(2),
    env.store.changePage(1, 1000),
    env.store.refresh(),
    env.store.start(flow),
  ]);
  expect(await env.store.exportCsv()).toBeNull();
  expect(env.calls).toHaveLength(before);
  expect(env.store.exporting.value).toBe(true);
  expect(env.store.pageSize.value).toBe(50);
  response.resolve(new Response('csv'));
  expect(await (await exporting)?.text()).toBe('csv');
  env.store.stop();
});

test('missing operations clear', async () => {
  for (const state of ['running', 'completed'] as const) {
    const env = setup();
    env.saved(state);
    await env.store.open(scope);
    env.respond(async () => Response.json({ detail: 'Dry run not found.' }, { status: 404 }));
    if (state === 'running') await env.store.cancel();
    else expect(await env.store.exportCsv()).toBeNull();
    expect(env.store.report.value).toBeNull();
    expect(env.store.active.value).toBe(false);
    expect(env.store.error.value).toBe('');
    expect(env.timers.size).toBe(0);
    env.store.stop();
  }
});

test('pending start reopens', async () => {
  const env = setup();
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const starting = env.store.start(flow);
  env.store.stop();
  env.saved('running', 'latest');
  await env.store.open(scope);
  await env.tick();
  expect(env.store.starting.value).toBe(true);
  expect(env.timers.size).toBe(0);
  response.resolve(Response.json(report('completed', 'stale')));
  await starting;
  expect(env.store.report.value?.id).toBe('latest');
  expect(env.store.starting.value).toBe(false);
  expect(env.timers.size).toBe(1);
  env.store.stop();
});

test('close preserves cancel', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const cancelling = env.store.cancel();
  expect(env.calls.at(-1)?.options?.signal).toBeUndefined();
  env.store.stop();
  env.saved('completed', 'latest');
  await env.store.open(scope);
  response.resolve(Response.json(report('cancelled', 'stale')));
  await cancelling;
  expect(env.store.report.value?.id).toBe('latest');
  expect(env.store.cancelling.value).toBe(false);
  expect(env.timers.size).toBe(0);
  env.store.stop();
});

test('reopen ignores responses', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  const response = pendingResponse();
  env.respond(() => response.promise);
  const polling = env.store.refresh();
  env.saved('completed', 'latest');
  await env.store.open({ library: { ...scope.library, id: 8 } });
  response.resolve(Response.json(report('running', 'old')));
  await polling;
  expect(env.store.report.value?.id).toBe('latest');
  expect(env.calls.at(-2)?.url).toBe('/api/libraries/8/dry-runs');
  expect(env.store.error.value).toBe('');
  expect(env.timers.size).toBe(0);
  env.store.stop();
});

test('errors keep results', async () => {
  const env = setup();
  env.saved('completed');
  await env.store.open(scope);
  env.respond(async () =>
    Response.json({ detail: 'Flow changed. Refresh flows.' }, { status: 409 }),
  );
  await env.store.start(flow);
  expect(env.store.error.value).toBe('Flow changed. Refresh flows.');
  expect(env.store.report.value?.items).toEqual([row]);
  expect(env.store.starting.value).toBe(false);
  env.store.stop();
});

test('cancel failure resumes', async () => {
  const env = setup();
  env.saved();
  await env.store.open(scope);
  env.respond(async () => Response.json({ detail: 'Temporary failure.' }, { status: 503 }));
  await env.store.cancel();
  expect(env.store.report.value?.items).toEqual([row]);
  expect(env.store.error.value).toBe('Temporary failure.');
  expect(env.store.cancelling.value).toBe(false);
  expect(env.timers.size).toBe(1);
  env.saved('completed');
  await env.tick();
  expect(env.store.error.value).toBe('');
  expect(env.store.active.value).toBe(false);
  env.store.stop();
});
