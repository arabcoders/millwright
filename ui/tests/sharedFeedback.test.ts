import { expect, test } from 'bun:test';
import * as Vue from 'vue';
import ts from 'typescript';
import type { useAuth } from '../app/composables/useAuth';
import type { useDialog } from '../app/composables/useDialog';
import type { useGlobalSearch } from '../app/composables/useGlobalSearch';
import type { useNotificationTargets } from '../app/composables/useNotificationTargets';
import type { useTrash } from '../app/composables/useTrash';
import type { useNodes } from '../app/composables/useNodes';
import type { useLibraries } from '../app/composables/useLibraries';
import type { useFlows } from '../app/composables/useFlows';
import type { usePlugins } from '../app/composables/usePlugins';
import type { useStats } from '../app/composables/useStats';
import type { useSystem } from '../app/composables/useSystem';
import type { useSettings } from '../app/composables/useSettings';
import { configureDates, formatDate } from '../app/utils/date';
import type {
  DryRunResult,
  FlowDefinition,
  LibraryBody,
  LibraryProgress,
  LibraryRow,
  PluginRow,
  OperationalSettings,
  StatsOverview,
} from '../app/types/api';
import type { formatBytes, ensure_api_success } from '../app/utils';

const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};
const ensure_api_success = async (response: Response) => {
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
};
const load = async (
  path: string,
  modules: Record<string, unknown>,
  globals: Record<string, unknown> = {},
) => {
  const source = (await Bun.file(`app/${path}`).text()).replaceAll('import.meta.client', 'false');
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const exports: Record<string, any> = {};
  new Function('require', 'exports', ...Object.keys(globals), compiled)(
    (name: string) => (name === 'vue' ? Vue : (modules[name] ?? {})),
    exports,
    ...Object.values(globals),
  );
  return exports;
};

test('bytes preserve growth', async () => {
  const exported = await load('utils/index.ts', {});
  const format = exported.formatBytes as typeof formatBytes;
  expect(format(0)).toBe('0 Bytes');
  for (const [bytes, expected] of [
    [1, '1 Bytes'],
    [1023, '1023 Bytes'],
    [1024, '1 KiB'],
    [1536, '1.5 KiB'],
    [1024 * 1024 - 1, '1024 KiB'],
    [1024 * 1024, '1 MiB'],
    [1024 ** 3, '1 GiB'],
  ] as const) {
    expect(format(bytes)).toBe(expected);
    expect(format(-bytes)).toBe(`-${expected}`);
  }
  expect(format(-1536, 0)).toBe('-2 KiB');
  expect(format(1536, -1)).toBe('2 KiB');
});

test('system configures dates', async () => {
  let response = Response.json({ timezone: 'Asia/Kuwait' });
  const exported = await load('composables/useSystem.ts', {
    '~/utils': { request: async () => response },
    '~/utils/date': { configureDates },
  });
  const system = (exported.useSystem as typeof useSystem)();
  configureDates(null);
  try {
    expect((await system.load())?.timezone).toBe('Asia/Kuwait');
    expect(formatDate('2026-10-10T12:30:00Z', 'clock')).toBe('03:30:00 PM');
    response = Response.json({ timezone: 'Asia/Kathmandu' });
    expect((await system.load())?.timezone).toBe('Asia/Kathmandu');
    expect(formatDate('2026-10-10T12:30:00Z', 'clock')).toBe('06:15:00 PM');
    response = Response.json({ timezone: 'UTC' });
    expect((await system.load())?.timezone).toBe('UTC');
    expect(formatDate('2026-10-10T12:30:00Z', 'clock')).toBe('12:30:00 PM');
    response = Response.json({}, { status: 503 });
    expect(await system.load()).toBeNull();
    expect(formatDate('2026-10-10T12:30:00Z', 'clock')).toBe('12:30:00 PM');
  } finally {
    configureDates(null);
  }
});

test('library totals aggregate', async () => {
  const payload: StatsOverview = {
    days: 7,
    library_id: 1,
    libraries: [
      {
        id: 1,
        name: 'Small',
        files: 2,
        bytes: 200,
        successful_transcodes: 1,
        successful_remuxes: 2,
        successful_unclassified: 0,
        transcode_saved_bytes: 30,
        remux_saved_bytes: 20,
        unclassified_saved_bytes: 0,
        source_bytes: 100,
        saved_bytes: 50,
        avg_reduction_pct: 50,
      },
      {
        id: 2,
        name: 'Large',
        files: 10,
        bytes: 1200,
        successful_transcodes: 3,
        successful_remuxes: 1,
        successful_unclassified: 1,
        transcode_saved_bytes: 260,
        remux_saved_bytes: 60,
        unclassified_saved_bytes: -20,
        source_bytes: 900,
        saved_bytes: 300,
        avg_reduction_pct: 33.3,
      },
    ],
    inventory: {
      files: 2,
      bytes: 200,
      video_codecs: {},
      audio_codecs: {},
      subtitle_codecs: {},
      audio_languages: {},
      subtitle_languages: {},
      containers: {},
      resolutions: {},
      health_status: {},
      transcode_status: {},
    },
    jobs: {
      completed: 1,
      succeeded: 1,
      failed: 0,
      cancelled: 0,
      skipped: 0,
      avg_duration_s: 0,
      by_kind: {},
    },
    acquisition: { series: [] },
    savings: {
      days: 7,
      series: [],
      totals: { files: 3, source_bytes: 100, saved_bytes: 50, avg_reduction_pct: 50 },
      by_kind: {
        transcode: {
          series: [],
          totals: { files: 1, source_bytes: 70, saved_bytes: 30, avg_reduction_pct: 42.9 },
        },
        remux: {
          series: [],
          totals: { files: 2, source_bytes: 30, saved_bytes: 20, avg_reduction_pct: 66.7 },
        },
        unclassified: {
          series: [],
          totals: { files: 0, source_bytes: 0, saved_bytes: 0, avg_reduction_pct: 0 },
        },
      },
    },
  };
  const exported = await load('composables/useStats.ts', {
    '~/utils': { request: async () => Response.json(payload) },
    '~/composables/useApiErrorMessage': {
      useApiErrorMessage: () => ({ messageFor: () => '' }),
    },
  });
  const stats = (exported.useStats as typeof useStats)();
  expect(stats.libraryTotals.value).toMatchObject({
    files: 0,
    bytes: 0,
    successful_transcodes: 0,
    source_bytes: 0,
    saved_bytes: 0,
    avg_reduction_pct: 0,
  });
  await stats.loadOverview(7, 1);
  expect(stats.libraryTotals.value).toEqual({
    files: 12,
    bytes: 1400,
    successful_transcodes: 4,
    successful_remuxes: 3,
    successful_unclassified: 1,
    transcode_saved_bytes: 290,
    remux_saved_bytes: 80,
    unclassified_saved_bytes: -20,
    source_bytes: 1000,
    saved_bytes: 350,
    avg_reduction_pct: 35,
  });

  for (const [source, saved, reduction] of [
    [0, -20, 0],
    [900, -300, -33.3],
    [1600, 500, 31.2],
    [1600, 508, 31.8],
    [1600, -500, -31.2],
    [1600, -508, -31.8],
    [2000, 603, 30.1],
    [2000, -603, -30.1],
  ] as const) {
    payload.libraries = [{ ...payload.libraries[0]!, source_bytes: source, saved_bytes: saved }];
    await stats.loadOverview(7, 1);
    expect(stats.libraryTotals.value.saved_bytes).toBe(saved);
    expect(stats.libraryTotals.value.avg_reduction_pct).toBe(reduction);
  }
  payload.libraries = [];
  await stats.loadOverview(7, 1);
  expect(stats.libraryTotals.value.files).toBe(0);
  expect(stats.libraryTotals.value.source_bytes).toBe(0);
  expect(stats.libraryTotals.value.avg_reduction_pct).toBe(0);
});

test('non-json errors retain status', async () => {
  const exported = await load('utils/index.ts', {});
  const ensureSuccess = exported.ensure_api_success as typeof ensure_api_success;
  await expect(
    ensureSuccess(new Response('<html>Bad Gateway</html>', { status: 502 })),
  ).rejects.toMatchObject({
    name: 'ApiError',
    status: 502,
    payload: null,
  });
});

test('flows retry failures', async () => {
  let fail = true;
  let requests = 0;
  const exported = await load('composables/useFlows.ts', {
    '~/utils': {
      request: async () => {
        requests++;
        return fail
          ? Response.json({ detail: 'Flows are unavailable.' }, { status: 503 })
          : Response.json([{ id: 1, name: 'Kept flow' }]);
      },
      parse_api_error: async (body: { detail: string }) => body.detail,
    },
  });
  const flows = (exported.useFlows as typeof useFlows)();
  await flows.ensure();
  expect(flows.lastError.value).toEqual({ status: 503, detail: 'Flows are unavailable.' });
  expect(flows.items.value).toEqual([]);
  fail = false;
  await flows.ensure();
  expect(flows.lastError.value).toBeNull();
  fail = true;
  await flows.reload();
  expect(flows.lastError.value?.status).toBe(503);
  expect(flows.items.value[0]?.name).toBe('Kept flow');
  fail = false;
  await flows.ensure();
  expect(flows.lastError.value).toBeNull();
  expect(requests).toBe(4);
});

test('auth retains session', async () => {
  const me = { id: 1, username: 'user', auth_method: 'session' };
  let response: Response | Error = Response.json(me);
  const exported = await load('composables/useAuth.ts', {
    '~/utils': {
      ensure_api_success,
      request: async () => {
        if (response instanceof Error) throw response;
        return response;
      },
    },
    '~/utils/diagnostic': { diagnostic() {} },
  });
  const auth = (exported.useAuth as typeof useAuth)();
  expect(await auth.probe()).toEqual(me);
  response = new Error('offline');
  await expect(auth.probe(true)).rejects.toThrow();
  expect(auth.status.value).toEqual(me);
  expect(auth.lastError.value).toContain('Couldn’t verify your sign-in.');
  response = new Response(null, { status: 503 });
  await expect(auth.probe(true)).rejects.toThrow();
  expect(auth.status.value).toEqual(me);
  expect(auth.lastError.value).toContain('Couldn’t verify your sign-in.');
  response = Response.json(me);
  expect(await auth.probe(true)).toEqual(me);
  expect(auth.lastError.value).toBeNull();
  response = new Response(null, { status: 401 });
  expect(await auth.probe(true)).toBeNull();
  expect(auth.status.value).toBeNull();
  expect(auth.lastError.value).toBeNull();
  response = Response.json(me);
  expect(await auth.probe()).toEqual(me);
  expect(auth.status.value).toEqual(me);

  let mode = 'offline';
  const redirects: string[] = [];
  const middleware = await load(
    'middleware/auth.global.ts',
    {
      '~/composables/useAuth': {
        useAuth: () => ({
          probe: async () => {
            if (mode === 'offline') throw new Error('offline');
            return null;
          },
        }),
      },
    },
    {
      defineNuxtRouteMiddleware: (guard: unknown) => guard,
      navigateTo: (path: string) => {
        redirects.push(path);
        return 'redirect';
      },
      abortNavigation: () => 'aborted',
    },
  );
  expect(await middleware.default({ path: '/nodes' }, { matched: [{}] })).toBe('aborted');
  expect(redirects).toEqual([]);
  mode = 'rejected';
  expect(await middleware.default({ path: '/nodes' }, { matched: [{}] })).toBe('redirect');
  expect(redirects).toEqual(['/login']);
});

test('dryrun sends flow', async () => {
  const flow: FlowDefinition = {
    entry: 'gate',
    when: { 'media.audio.count': { gt: 0 } },
    nodes: [
      { id: 'gate', type: 'condition', when: { 'media.video.count': { gt: 0 } } },
      { id: 'skip', type: 'terminal', outcome: 'skip' },
    ],
    edges: [{ src: 'gate', port: 'false', dst: 'skip' }],
  };
  const result: DryRunResult = {
    status: 'skipped',
    reason: '',
    steps: [
      { node: 'gate', outcome: 'false' },
      { node: 'skip', outcome: 'skip' },
    ],
    facets: {},
    plan: null,
    argv: null,
    stream_log: [],
  };
  const calls: Array<{ path: string; options?: RequestInit }> = [];
  const exported = await load('composables/useFlows.ts', {
    '~/utils': {
      request: async (path: string, options?: RequestInit) => {
        calls.push({ path, options });
        return Response.json(result);
      },
    },
  });
  const flows = (exported.useFlows as typeof useFlows)();
  expect(await flows.dryRun(42, flow)).toEqual(result);
  expect(calls.length).toBe(1);
  expect(calls[0]?.path).toBe('/api/flows/dry-run');
  expect(calls[0]?.options?.method).toBe('POST');
  expect(JSON.parse(String(calls[0]?.options?.body))).toEqual({ file_id: 42, flow });
});

test('dryrun preserves errors', async () => {
  const utilities = await load('utils/index.ts', {});
  const parse = utilities.parse_api_error as typeof parse_api_error;
  const flow: FlowDefinition = {
    entry: 'skip',
    nodes: [{ id: 'skip', type: 'terminal', outcome: 'skip' }],
    edges: [],
  };
  for (const [status, detail] of [
    [404, 'file not found'],
    [422, 'file has no probe'],
  ] as const) {
    const exported = await load('composables/useFlows.ts', {
      '~/utils': {
        request: async (path: string) => {
          expect(path).toBe('/api/flows/dry-run');
          return Response.json({ detail }, { status });
        },
        parse_api_error: parse,
      },
    });
    const flows = (exported.useFlows as typeof useFlows)();
    await expect(flows.dryRun(42, flow)).rejects.toEqual({ status, detail });
  }
  const failure = new Error('Connection lost');
  const exported = await load('composables/useFlows.ts', {
    '~/utils': {
      request: async () => {
        throw failure;
      },
    },
  });
  await expect((exported.useFlows as typeof useFlows)().dryRun(42, flow)).rejects.toBe(failure);
});

test('node lanes update independently', async () => {
  let requests = 0;
  const exported = await load('composables/useNodes.ts', {
    '~/utils': {
      request: async (path: string) => {
        requests++;
        if (path === '/api/nodes/registration')
          return Response.json({ enabled: true, pending: [] });
        return Response.json([{ id: 1, name: 'node', state: 'offline' }]);
      },
    },
  });
  const nodes = (exported.useNodes as typeof useNodes)();
  await nodes.ensure();
  nodes.setStatus('node', 'online', 3, [3, 4, 5], 0, 1, 2);
  expect({ ...nodes.items.value[0] }).toMatchObject({
    active_jobs: 3,
    active_transcodes: 0,
    active_healthchecks: 1,
    active_mkv_stats: 2,
  });
  expect([...nodes.items.value[0]!.active_job_ids]).toEqual([3, 4, 5]);
  nodes.setStatus('node', 'online', 1, [4], 0, 1, 0);
  expect({ ...nodes.items.value[0] }).toMatchObject({
    active_jobs: 1,
    active_transcodes: 0,
    active_healthchecks: 1,
    active_mkv_stats: 0,
  });
  nodes.setStatus('new-node', 'online', 0, [], 0, 0, 0);
  expect(requests).toBe(2);
  await nodes.reload();
  expect(requests).toBe(4);
});

test('settings preserve rejected drafts', async () => {
  const original: OperationalSettings = {
    node_registration_enabled: true,
    max_healthchecks: 1,
    max_probes: 4,
    max_mkv_stats: 1,
    healthcheck_hwaccel: true,
    healthcheck_location: 'local',
    mkv_stats_location: 'local',
    min_free_gb: 10,
    job_retention_days: 30,
    session_ttl_days: 7,
    scanner: { containers: ['mkv'], ignore_dirs: [] },
    playback: {
      segment_duration_s: 6,
      max_concurrent_segments: 2,
      grant_ttl_s: 3600,
      max_grants: 64,
    },
  };
  let response = Response.json(original);
  const dirty = await load('composables/useDirtyState.ts', {});
  const exported = await load('composables/useSettings.ts', {
    '~/utils': {
      request: async () => response.clone(),
      parse_api_error: async (body: { detail: string }) => body.detail,
    },
    '~/composables/useDirtyState': dirty,
  });
  const settings = (exported.useSettings as typeof useSettings)();
  await settings.load();
  settings.draft.value!.healthcheck_location = 'node';
  settings.draft.value!.scanner.ignore_dirs.push('extras');
  response = Response.json({ detail: 'Settings could not be stored.' }, { status: 503 });
  expect(await settings.save()).toBe(false);
  expect(settings.error.value).toBe('Settings could not be stored.');
  expect(settings.draft.value!.healthcheck_location).toBe('node');
  expect(settings.draft.value!.scanner.ignore_dirs).toEqual(['extras']);
  expect(settings.isDirty.value).toBe(true);
  expect(settings.saving.value).toBe(false);
  response = Response.json({
    ...original,
    healthcheck_location: 'node',
    scanner: { containers: ['mkv'], ignore_dirs: ['extras'] },
  });
  expect(await settings.save()).toBe(true);
  expect(settings.error.value).toBeNull();
  expect(settings.isDirty.value).toBe(false);
  settings.draft.value!.playback.segment_duration_s = 8;
  expect(settings.isDirty.value).toBe(true);
  response = Response.json({ detail: 'Settings unavailable.' }, { status: 503 });
  await settings.load();
  expect(settings.draft.value!.playback.segment_duration_s).toBe(8);
  expect(settings.isDirty.value).toBe(true);
  expect(settings.loading.value).toBe(false);
});

test('node registration retains failures', async () => {
  let fail = true;
  const errors: string[] = [];
  const successes: string[] = [];
  const exported = await load(
    'composables/useNodes.ts',
    {
      '~/utils': {
        request: async () => {
          if (fail) throw new Error('Connection lost');
          return new Response(null, { status: 202 });
        },
      },
      '~/composables/useApiErrorMessage': {
        useApiErrorMessage: () => ({ messageFor: (text: string) => text }),
      },
    },
    {
      useNotification: () => ({
        success: (text: string) => successes.push(text),
        error: (text: string) => errors.push(text),
      }),
    },
  );
  const nodes = (exported.useNodes as typeof useNodes)();
  nodes.setRegistration({
    enabled: true,
    pending: [{ request_id: 'request-a', capabilities: {} }],
  });
  await expect(nodes.claimNode('request-a')).rejects.toThrow('Connection lost');
  await expect(nodes.allowRegistration(false)).rejects.toThrow('Connection lost');
  expect(nodes.registration.value?.enabled).toBe(true);
  expect(nodes.registration.value?.pending.map((node) => node.request_id)).toEqual(['request-a']);
  expect(errors).toEqual(['Connection lost', 'Connection lost']);
  expect(successes).toEqual([]);
  fail = false;
  await nodes.claimNode('request-a');
  expect(nodes.registration.value?.pending).toEqual([]);
  nodes.setRegistration({
    enabled: true,
    pending: [{ request_id: 'request-b', capabilities: {} }],
  });
  await nodes.allowRegistration(false);
  expect(nodes.registration.value).toEqual({ enabled: false, pending: [] });
  expect(successes).toHaveLength(2);
});

test('cancels queued dialogs', async () => {
  const states = new Map<string, Vue.Ref>();
  const exported = await load(
    'composables/useDialog.ts',
    {},
    {
      useState: (key: string, init: () => unknown) => {
        if (!states.has(key)) states.set(key, Vue.ref(init()));
        return states.get(key);
      },
    },
  );
  const dialog = (exported.useDialog as typeof useDialog)();
  const first = dialog.confirmDialog({ title: 'first' });
  const second = dialog.confirmDialog({ title: 'second' });
  dialog.confirm();
  expect((await first).status).toBe(true);
  expect(dialog.state.current?.opts.title).toBe('second');
  const third = dialog.promptDialog({ title: 'third' });
  dialog.cancelAll();
  expect(await second).toEqual({ status: false, value: null });
  expect(await third).toEqual({ status: false, value: null });
  expect(dialog.state.current).toBeNull();
  expect(dialog.state.queue).toEqual([]);
}, 1000);

test('search reloads and cancels', async () => {
  const signals: AbortSignal[] = [];
  const jobs: Array<ReturnType<typeof deferred<Response>>> = [];
  const timers = new Map<number, () => void>();
  let timerId = 0;
  let failList = false;
  let calls = 0;
  const exported = await load(
    'composables/useGlobalSearch.ts',
    {
      '~/utils': {
        ensure_api_success,
        request: async (path: string, options: RequestInit) => {
          calls++;
          signals.push(options.signal as AbortSignal);
          if (path.startsWith('/api/jobs/')) {
            const pending = deferred<Response>();
            jobs.push(pending);
            return pending.promise;
          }
          if (failList) throw new Error('offline');
          return Response.json([{ id: 1 }]);
        },
      },
    },
    {
      setTimeout: (callback: () => void) => {
        timers.set(++timerId, callback);
        return timerId;
      },
      clearTimeout: (id: number) => timers.delete(id),
    },
  );
  const lookupJob =
    exported.lookupJob as typeof import('../app/composables/useGlobalSearch').lookupJob;
  const lookups: ReturnType<typeof lookupJob>[] = [];
  // The debounced watcher doesn't return its lookup promises.
  exported.lookupJob = (...args: Parameters<typeof lookupJob>) => {
    const pending = lookupJob(...args);
    lookups.push(pending);
    return pending;
  };
  const scope = Vue.effectScope();
  const search = scope.run(() => (exported.useGlobalSearch as typeof useGlobalSearch)())!;
  try {
    await Promise.all([search.openSearch(), search.openSearch()]);
    expect(calls).toBe(3);
    search.closeSearch();
    await search.openSearch();
    expect(calls).toBe(6);
    search.searchTerm.value = '12';
    await Vue.nextTick();
    timers.get(timerId)!();
    search.searchTerm.value = '13';
    await Vue.nextTick();
    expect(signals.at(-1)!.aborted).toBe(true);
    jobs[0]!.resolve(Response.json({ id: 12 }));
    await lookups[0];
    timers.get(timerId)!();
    jobs[1]!.resolve(new Response(null, { status: 503 }));
    await expect(lookups[1]!).rejects.toThrow('HTTP 503');
    expect(search.matchedJob.value).toBeNull();
    expect(search.jobError.value).toBe('Could not look up this job. Change the query to retry.');
    search.searchTerm.value = '14';
    await Vue.nextTick();
    timers.get(timerId)!();
    scope.stop();
    expect(signals.at(-1)!.aborted).toBe(true);
    jobs[2]!.resolve(Response.json({ id: 14 }));
    await lookups[2];
    expect(search.matchedJob.value).toBeNull();
    failList = true;
    const nextScope = Vue.effectScope();
    try {
      const next = nextScope.run(() => (exported.useGlobalSearch as typeof useGlobalSearch)())!;
      await next.openSearch();
      expect(next.listError.value).toBe(
        'Could not load search data. Close and reopen search to retry.',
      );
    } finally {
      nextScope.stop();
    }
  } finally {
    jobs.forEach((job) => job.resolve(Response.json({ id: 0 })));
    scope.stop();
    await Promise.allSettled(lookups);
  }
});

test('search close aborts lists', async () => {
  const requests: Array<{ signal: AbortSignal; pending: ReturnType<typeof deferred<Response>> }> =
    [];
  const exported = await load('composables/useGlobalSearch.ts', {
    '~/utils': {
      ensure_api_success,
      request: (_path: string, options: RequestInit) => {
        const pending = deferred<Response>();
        requests.push({ signal: options.signal as AbortSignal, pending });
        return pending.promise;
      },
    },
  });
  const scope = Vue.effectScope();
  const search = scope.run(() => (exported.useGlobalSearch as typeof useGlobalSearch)())!;
  try {
    const first = search.openSearch();
    search.closeSearch();
    expect(requests.every((request) => request.signal.aborted)).toBe(true);
    const second = search.openSearch();
    requests.slice(3).forEach(({ pending }) => pending.resolve(Response.json([{ id: 2 }])));
    await second;
    requests.slice(0, 3).forEach(({ pending }) => pending.resolve(Response.json([{ id: 1 }])));
    await first;
    expect(search.searchable.value.nodes[0]?.id).toBe(2);
    expect(search.listError.value).toBe('');
    search.closeSearch();
    const third = search.openSearch();
    expect(requests.length).toBe(9);
    requests.slice(6).forEach(({ pending }) => pending.resolve(Response.json([{ id: 3 }])));
    await third;
    expect(search.searchable.value.nodes[0]?.id).toBe(3);
  } finally {
    requests.forEach(({ pending }) => pending.resolve(Response.json([])));
    scope.stop();
  }
}, 1000);

test('notification commit survives refresh', async () => {
  const target = { name: 'mail', url: 'mailto://kept', created_at: '', updated_at: '' };
  let fail = false;
  let gets = 0;
  const exported = await load('composables/useNotificationTargets.ts', {
    '~/utils': {
      request: async (_path: string, options?: RequestInit) => {
        if (options?.method === 'PUT') {
          fail = true;
          return Response.json({ ...target, url: 'mailto://new' });
        }
        if (options?.method === 'DELETE' || options?.method === 'POST')
          return new Response(null, { status: 204 });
        gets++;
        if (fail) throw new Error('offline');
        return Response.json([target]);
      },
    },
  });
  const targets = (exported.useNotificationTargets as typeof useNotificationTargets)();
  await targets.ensure();
  const result = await targets.save('mail', 'mailto://new', target);
  expect(result.ok).toBe(true);
  expect(result).toHaveProperty('refreshError');
  expect(targets.items.value[0]?.url).toBe('mailto://new');
  const before = gets;
  expect((await targets.test('mail')).ok).toBe(true);
  expect(gets).toBe(before);
  expect((await targets.remove('mail')).ok).toBe(true);
  expect(targets.items.value).toEqual([]);
});

test('restore lists backup', async () => {
  const entry = { id: 'old', size_bytes: 10 };
  const backup = { id: 'backup', size_bytes: 20 };
  const urls: string[] = [];
  let restored = false;
  const exported = await load('composables/useTrash.ts', {
    '~/utils': {
      request: async (url: string, options?: RequestInit) => {
        urls.push(url);
        if (options?.method === 'POST') {
          expect(JSON.parse(String(options.body))).toEqual({
            replace_existing: true,
            move_to_trash: true,
            sync_to_disk: true,
          });
          restored = true;
          return new Response(null, { status: 204 });
        }
        const offset = Number(new URL(url, 'https://test').searchParams.get('offset'));
        return Response.json({
          items: offset ? [] : [restored ? backup : entry],
          pagination: { total: 1 },
          total_size_bytes: restored ? 20 : 10,
        });
      },
    },
  });
  const trash = (exported.useTrash as typeof useTrash)();
  const filters = {
    query: 'movie',
    library_id: '2',
    limit: 100,
    offset: 0,
    sort_by: 'purge_at' as const,
    sort_dir: 'asc' as const,
  };
  await trash.load(filters);
  const options = { replace_existing: true, move_to_trash: true, sync_to_disk: true };
  expect((await trash.restore('old', options, filters)).ok).toBe(true);
  expect(trash.items.value.map((row) => row.id)).toEqual(['backup']);
  expect(trash.total.value).toBe(1);
  expect(trash.totalSizeBytes.value).toBe(20);
  const result = await trash.restore('old', options, { ...filters, offset: 100 });
  expect(result).toEqual({ ok: true, offset: 0 });
  expect(urls.at(-1)).toContain('offset=0');
  expect(urls.at(-1)).toContain('query=movie');
  expect(urls.at(-1)).toContain('limit=100');
  expect(urls.at(-1)).toContain('library_id=2');
  expect(urls.at(-1)).toContain('sort_by=purge_at');
  expect(urls.at(-1)).toContain('sort_dir=asc');
  expect(trash.items.value[0]?.id).toBe('backup');
});

test('trash retains failures', async () => {
  const kept = { id: 'kept', size_bytes: 10 };
  const deleted = { id: 'deleted', size_bytes: 20 };
  const exported = await load('composables/useTrash.ts', {
    '~/utils': {
      request: async (url: string, options?: RequestInit) => {
        if (options?.method === 'DELETE')
          return url.endsWith('/kept')
            ? Response.json({ detail: 'File is in use.' }, { status: 409 })
            : new Response(null, { status: 204 });
        return Response.json({
          items: [kept, deleted],
          pagination: { total: 2 },
          total_size_bytes: 30,
        });
      },
      parse_api_error: async (body: { detail: string }) => body.detail,
    },
  });
  const trash = (exported.useTrash as typeof useTrash)();
  await trash.load({ limit: 50, offset: 0 });
  expect(await trash.expunge('kept')).toEqual({ ok: false, error: 'File is in use.' });
  expect(trash.items.value.map((entry) => entry.id)).toEqual(['kept', 'deleted']);
  expect(await trash.expunge('deleted')).toEqual({ ok: true });
  expect(trash.items.value.map((entry) => entry.id)).toEqual(['kept']);
  expect(trash.total.value).toBe(1);
  expect(trash.totalSizeBytes.value).toBe(10);
});

test('partial empty refreshes pagination', async () => {
  const retained = { id: 'kept', size_bytes: 10 };
  const urls: string[] = [];
  let emptied = false;
  const exported = await load('composables/useTrash.ts', {
    '~/utils': {
      request: async (url: string, options?: RequestInit) => {
        if (options?.method === 'DELETE') {
          emptied = true;
          return Response.json({
            deleted: 100,
            deleted_size_bytes: 1000,
            failed: ['kept', 'other'],
          });
        }
        urls.push(url);
        const offset = Number(new URL(url, 'https://test').searchParams.get('offset'));
        return Response.json({
          items: emptied && offset ? [] : [retained],
          pagination: { total: emptied ? 2 : 101 },
          total_size_bytes: emptied ? 30 : 1010,
        });
      },
    },
  });
  const trash = (exported.useTrash as typeof useTrash)();
  await trash.load({ library_id: '1', query: 'movie', limit: 50, offset: 100 });
  expect(await trash.empty()).toEqual({
    deleted: 100,
    deleted_size_bytes: 1000,
    failed: ['kept', 'other'],
    offset: 0,
  });
  expect(trash.items.value.map((entry) => entry.id)).toEqual(['kept']);
  expect(trash.total.value).toBe(2);
  expect(trash.totalSizeBytes.value).toBe(30);
  expect(urls).toEqual([
    '/api/trash?limit=50&offset=100&query=movie&library_id=1',
    '/api/trash?limit=50&offset=100&query=movie&library_id=1',
    '/api/trash?limit=50&offset=0&query=movie&library_id=1',
  ]);
});

test('empty resets pagination', async () => {
  const exported = await load('composables/useTrash.ts', {
    '~/utils': {
      request: async (_url: string, options?: RequestInit) =>
        options?.method === 'DELETE'
          ? Response.json({ deleted: 51, deleted_size_bytes: 510, failed: [] })
          : Response.json({
              items: [{ id: 'last', size_bytes: 10 }],
              pagination: { total: 51 },
              total_size_bytes: 510,
            }),
    },
  });
  const trash = (exported.useTrash as typeof useTrash)();
  await trash.load({ library_id: '1', limit: 50, offset: 50 });
  const result = await trash.empty();
  expect(trash.items.value).toEqual([]);
  expect(trash.total.value).toBe(0);
  expect(trash.totalSizeBytes.value).toBe(0);
  expect(result).toMatchObject({ deleted: 51, failed: [], offset: 0 });
});

test('empty retains reset filters', async () => {
  const urls: string[] = [];
  let deletions = 0;
  const exported = await load('composables/useTrash.ts', {
    '~/utils': {
      request: async (url: string, options?: RequestInit) => {
        if (options?.method === 'DELETE') {
          deletions++;
          return Response.json({
            deleted: deletions === 1 ? 51 : 0,
            deleted_size_bytes: deletions === 1 ? 510 : 0,
            failed: deletions === 1 ? [] : ['locked'],
          });
        }
        urls.push(url);
        return Response.json({
          items: [{ id: 'locked', size_bytes: 10 }],
          pagination: { total: deletions ? 1 : 51 },
          total_size_bytes: deletions ? 10 : 510,
        });
      },
    },
  });
  const trash = (exported.useTrash as typeof useTrash)();
  await trash.load({ library_id: '1', query: 'movie', limit: 50, offset: 50 });
  expect(await trash.empty()).toMatchObject({ offset: 0 });
  expect(await trash.empty()).toMatchObject({ failed: ['locked'] });
  expect(urls).toEqual([
    '/api/trash?limit=50&offset=50&query=movie&library_id=1',
    '/api/trash?limit=50&offset=0&query=movie&library_id=1',
  ]);
  expect(trash.items.value.map((entry) => entry.id)).toEqual(['locked']);
  expect(trash.total.value).toBe(1);
});

test('partial empty keeps success', async () => {
  const retained = { id: 'kept', size_bytes: 10 };
  const deleted = { id: 'deleted', size_bytes: 20 };
  let emptied = false;
  const exported = await load('composables/useTrash.ts', {
    '~/utils': {
      request: async (_url: string, options?: RequestInit) => {
        if (options?.method === 'DELETE') {
          emptied = true;
          return Response.json({ deleted: 100, deleted_size_bytes: 1000, failed: ['kept'] });
        }
        if (emptied) throw new Error('offline');
        return Response.json({
          items: [retained, deleted],
          pagination: { total: 2 },
          total_size_bytes: 30,
        });
      },
    },
  });
  const trash = (exported.useTrash as typeof useTrash)();
  await trash.load({ library_id: '1', limit: 50, offset: 0 });
  const outcome = await trash.empty();
  expect(outcome).toMatchObject({ deleted: 100, failed: ['kept'] });
  expect(outcome).toHaveProperty('refreshError');
  expect(trash.items.value.map((entry) => entry.id)).toEqual(['kept']);
  expect(trash.total.value).toBe(1);
  expect(trash.totalSizeBytes.value).toBe(10);
  expect(trash.lastError.value).toBeInstanceOf(Error);
});

test('empty preserves newer entries', async () => {
  const old = { id: 'old', size_bytes: 20 };
  const added = { id: 'added', size_bytes: 10 };
  let release!: (response: Response) => void;
  const deletion = new Promise<Response>((resolve) => (release = resolve));
  let gets = 0;
  const exported = await load('composables/useTrash.ts', {
    '~/utils': {
      request: async (_url: string, options?: RequestInit) => {
        if (options?.method === 'DELETE') return deletion;
        gets++;
        if (gets > 2) throw new Error('refresh offline');
        return Response.json({
          items: gets === 1 ? [old] : [added],
          pagination: { total: 1 },
          total_size_bytes: gets === 1 ? 20 : 10,
        });
      },
    },
  });
  const trash = (exported.useTrash as typeof useTrash)();
  await trash.load({ library_id: '1', limit: 50, offset: 0 });
  const pending = trash.empty();
  try {
    // This entry was created after the bulk operation selected its targets.
    await trash.load({ library_id: '2', limit: 50, offset: 0 });
    release(Response.json({ deleted: 1, deleted_size_bytes: 20, failed: ['locked'] }));
    const outcome = await pending;
    expect(outcome).toHaveProperty('refreshError');
    expect(trash.items.value.map((entry) => entry.id)).toEqual(['added']);
    expect(trash.total.value).toBe(1);
    expect(trash.totalSizeBytes.value).toBe(10);
  } finally {
    release(Response.json({ deleted: 1, deleted_size_bytes: 20, failed: ['locked'] }));
    await pending;
  }
});

test('complete empty preserves snapshot', async () => {
  for (const pendingLoad of [false, true]) {
    const old = { id: 'old', size_bytes: 20 };
    const added = { id: 'added', size_bytes: 10 };
    let releaseDelete!: (response: Response) => void;
    let releaseLoad!: (response: Response) => void;
    const deletion = new Promise<Response>((resolve) => (releaseDelete = resolve));
    const loading = new Promise<Response>((resolve) => (releaseLoad = resolve));
    let gets = 0;
    const exported = await load('composables/useTrash.ts', {
      '~/utils': {
        request: async (_url: string, options?: RequestInit) => {
          if (options?.method === 'DELETE') return deletion;
          gets++;
          if (gets === 2) return loading;
          if (gets > 2) throw new Error('refresh offline');
          return Response.json({ items: [old], pagination: { total: 1 }, total_size_bytes: 20 });
        },
      },
    });
    const trash = (exported.useTrash as typeof useTrash)();
    await trash.load({ library_id: '1', limit: 50, offset: 0 });
    let refresh: Promise<void> | undefined;
    if (pendingLoad) refresh = trash.load({ library_id: '2', limit: 50, offset: 0 });
    const pending = trash.empty();
    try {
      refresh ??= trash.load({ library_id: '2', limit: 50, offset: 0 });
      releaseLoad(
        Response.json({ items: [added], pagination: { total: 1 }, total_size_bytes: 10 }),
      );
      await refresh;
      releaseDelete(Response.json({ deleted: 1, deleted_size_bytes: 20, failed: [] }));
      const outcome = await pending;
      expect(outcome).toHaveProperty('refreshError');
      expect(trash.items.value.map((entry) => entry.id)).toEqual(['added']);
      expect(trash.total.value).toBe(1);
      expect(trash.totalSizeBytes.value).toBe(10);
    } finally {
      releaseLoad(
        Response.json({ items: [added], pagination: { total: 1 }, total_size_bytes: 10 }),
      );
      releaseDelete(Response.json({ deleted: 1, deleted_size_bytes: 20, failed: [] }));
      await Promise.all([pending, refresh]);
    }
  }
});

test('empty supersedes pending load', async () => {
  const first = { id: 'first', size_bytes: 20 };
  const second = { id: 'second', size_bytes: 10 };
  let release!: (response: Response) => void;
  const oldResponse = new Promise<Response>((resolve) => (release = resolve));
  let gets = 0;
  const exported = await load('composables/useTrash.ts', {
    '~/utils': {
      request: async (_url: string, options?: RequestInit) => {
        if (options?.method === 'DELETE')
          return Response.json({ deleted: 2, deleted_size_bytes: 30, failed: [] });
        gets++;
        if (gets === 2) return oldResponse;
        return Response.json({
          items: gets === 1 ? [first] : [],
          pagination: { total: gets === 1 ? 1 : 0 },
          total_size_bytes: gets === 1 ? 20 : 0,
        });
      },
    },
  });
  const trash = (exported.useTrash as typeof useTrash)();
  await trash.load({ library_id: '1', limit: 50, offset: 0 });
  const pending = trash.load({ library_id: '2', limit: 50, offset: 0 });
  try {
    expect(await trash.empty()).toMatchObject({ deleted: 2, failed: [] });
    // The older GET selected this entry before the global deletion committed.
    release(Response.json({ items: [second], pagination: { total: 1 }, total_size_bytes: 10 }));
    await pending;
    expect(trash.items.value).toEqual([]);
    expect(trash.total.value).toBe(0);
    expect(trash.totalSizeBytes.value).toBe(0);
  } finally {
    release(Response.json({ items: [second], pagination: { total: 1 }, total_size_bytes: 10 }));
    await pending;
  }
});

test('empty supersedes load error', async () => {
  const failure = new Error('old load offline');
  let reject!: (error: Error) => void;
  const oldResponse = new Promise<Response>(
    (_resolve, rejectResponse) => (reject = rejectResponse),
  );
  let gets = 0;
  const exported = await load('composables/useTrash.ts', {
    '~/utils': {
      request: async (_url: string, options?: RequestInit) => {
        if (options?.method === 'DELETE')
          return Response.json({ deleted: 1, deleted_size_bytes: 20, failed: [] });
        gets++;
        if (gets === 2) return oldResponse;
        return Response.json({
          items: [{ id: 'old', size_bytes: 20 }],
          pagination: { total: 1 },
          total_size_bytes: 20,
        });
      },
    },
  });
  const trash = (exported.useTrash as typeof useTrash)();
  await trash.load({ limit: 50, offset: 0 });
  const pending = trash
    .load({ library_id: '2', limit: 50, offset: 0 })
    .catch((error: Error) => error);
  try {
    expect(await trash.empty()).toMatchObject({ deleted: 1, failed: [] });
    expect(trash.isLoading.value).toBe(false);
    reject(failure);
    expect(await pending).toBe(failure);
    expect(trash.lastError.value).toBeNull();
    expect(trash.items.value).toEqual([]);
    expect(trash.total.value).toBe(0);
    expect(trash.totalSizeBytes.value).toBe(0);
  } finally {
    reject(failure);
    await pending;
  }
});

test('restore skips optional sync', async () => {
  const options = { replace_existing: false, move_to_trash: false, sync_to_disk: false };
  const exported = await load('composables/useTrash.ts', {
    '~/utils': {
      request: async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          expect(url).toBe('/api/trash/old/restore');
          expect(JSON.parse(String(init.body))).toEqual(options);
          return Response.json({ id: 'old', restored_path: '/library/movie.mkv' });
        }
        return Response.json({ items: [], pagination: { total: 0 }, total_size_bytes: 0 });
      },
    },
  });
  const trash = (exported.useTrash as typeof useTrash)();
  expect(await trash.restore('old', options, { limit: 50, offset: 0 })).toEqual({
    ok: true,
    offset: 0,
  });
});

test('refresh retains stale rows', async () => {
  for (const path of ['Nodes', 'Libraries'] as const) {
    let fail = false;
    const exported = await load(`composables/use${path}.ts`, {
      '~/utils': {
        request: async () => {
          if (fail) throw new Error('offline');
          return Response.json([{ id: 1, roots: [], trash_policy: {} }]);
        },
      },
    });
    const state = (exported[`use${path}`] as typeof useNodes | typeof useLibraries)();
    await state.ensure();
    fail = true;
    await state.reload();
    expect(state.lastError.value).toBeInstanceOf(Error);
    expect(state.items.value[0]?.id).toBe(1);
  }
});

test('scan cancellation returns snapshot', async () => {
  const progress: LibraryProgress = {
    library_id: 4,
    scanner: { paused: true, next_run_at: null, discovery_pending: false },
    discovery: {
      status: 'paused',
      found: 0,
      added: 0,
      updated: 0,
      error_count: 0,
      error: null,
      reason: null,
      latest_result: null,
      finished_at: null,
    },
    probing: { status: 'paused', processed: 0, remaining: 2, error_count: 0, active: [] },
  };
  const calls: Array<{ path: string; method?: string }> = [];
  const feedback: string[] = [];
  let failed = false;
  const exported = await load(
    'composables/useLibraries.ts',
    {
      '~/utils': {
        request: async (path: string, options?: RequestInit) => {
          calls.push({ path, method: options?.method });
          return failed
            ? Response.json({ detail: 'Unavailable' }, { status: 503 })
            : Response.json({ status: 'cancelling', progress });
        },
        parse_api_error: (body: { detail: string }) => body.detail,
      },
      '~/composables/useApiErrorMessage': {
        useApiErrorMessage: () => ({ messageFor: (text: string) => text }),
      },
    },
    {
      useNotification: () => ({
        success: () => feedback.push('success'),
        error: () => feedback.push('error'),
      }),
    },
  );
  const libraries = (exported.useLibraries as typeof useLibraries)();
  expect(await libraries.cancelScan({ id: 4, name: 'Films' })).toEqual(progress);
  failed = true;
  expect(await libraries.cancelScan({ id: 4, name: 'Films' })).toBeNull();
  expect(calls.map((call) => call.path)).toEqual([
    '/api/libraries/4/scan/cancel',
    '/api/libraries/4/scan/cancel',
  ]);
  expect(calls.every((call) => call.method === 'POST')).toBe(true);
  expect(feedback).toEqual(['success', 'error']);
});

test('batch scans summarize outcomes', async () => {
  const calls: Array<{ path: string; options?: RequestInit }> = [];
  const feedback: Array<{ level: string; message: string }> = [];
  let response = Response.json({
    outcomes: [
      { library_id: 1, status: 'queued' },
      { library_id: 2, status: 'already_running' },
      { library_id: 3, status: 'already_queued' },
    ],
  });
  const exported = await load(
    'composables/useLibraries.ts',
    {
      '~/utils': {
        request: async (path: string, options?: RequestInit) => {
          calls.push({ path, options });
          return response;
        },
        parse_api_error: (body: { detail: string }) => body.detail,
      },
      '~/composables/useApiErrorMessage': {
        useApiErrorMessage: () => ({ messageFor: (text: string) => text }),
      },
    },
    {
      useNotification: () =>
        Object.fromEntries(
          ['success', 'info', 'error'].map((level) => [
            level,
            (message: string) => feedback.push({ level, message }),
          ]),
        ),
    },
  );
  const libraries = (exported.useLibraries as typeof useLibraries)();
  await libraries.scanMany([]);
  expect(calls).toHaveLength(0);
  await libraries.scanMany([1, 2, 3]);
  expect(calls).toEqual([
    {
      path: '/api/libraries/scan',
      options: { method: 'POST', body: JSON.stringify({ library_ids: [1, 2, 3] }) },
    },
  ]);
  expect(feedback).toEqual([
    { level: 'success', message: '1 queued, 2 already queued or running, 0 suspended, 0 failed.' },
  ]);
  response = Response.json({ outcomes: [{ library_id: 1, status: 'suspended' }] });
  await libraries.scanMany([1]);
  expect(feedback.at(-1)).toEqual({
    level: 'info',
    message: '0 queued, 0 already queued or running, 1 suspended, 0 failed.',
  });
  response = Response.json({
    outcomes: [
      { library_id: 1, status: 'stopped' },
      { library_id: 2, status: 'missing' },
    ],
  });
  await libraries.scanMany([1, 2]);
  expect(feedback.at(-1)).toEqual({
    level: 'error',
    message: '0 queued, 0 already queued or running, 0 suspended, 2 failed.',
  });
  response = Response.json({ detail: 'Scanner is paused.' }, { status: 409 });
  await libraries.scanMany([1]);
  expect(feedback.at(-1)).toEqual({ level: 'error', message: 'Scanner is paused.' });
  expect(feedback).toHaveLength(4);
});

test('deletion retains pending', async () => {
  const rows = [{ id: 1, name: 'Movies', suspended: false, deleted: false, delete_error: null }];
  const urls: string[] = [];
  const exported = await load(
    'composables/useLibraries.ts',
    {
      '~/utils': {
        request: async (url: string, options?: RequestInit) => {
          urls.push(url);
          return options?.method === 'DELETE'
            ? Response.json({ library_id: 1, status: 'deleting' }, { status: 202 })
            : Response.json(rows);
        },
      },
      '~/composables/useEvents': { useEvents: () => ({ on: () => () => {} }) },
      '~/composables/useApiErrorMessage': { useApiErrorMessage: () => ({ messageFor: String }) },
    },
    { useNotification: () => ({ info: () => {}, error: () => {} }) },
  );
  const libraries = (exported.useLibraries as typeof useLibraries)();
  await libraries.ensure();
  expect(await libraries.deleteLibrary(rows[0]!)).toBe(true);
  expect(libraries.items.value[0]?.deleted).toBe(true);
  expect(libraries.items.value[0]?.suspended).toBe(true);
  expect(urls).toEqual(['/api/libraries', '/api/libraries/1']);
  exported.applyLibraryDeletionEvent({
    library_id: 1,
    status: 'failed',
    error: 'Worker still owns the source.',
  });
  expect(libraries.items.value[0]?.delete_error).toBe('Worker still owns the source.');
  exported.applyLibraryDeletionEvent({ library_id: 1, status: 'deleting', error: null });
  expect(libraries.items.value[0]?.delete_error).toBeNull();
  exported.applyLibraryDeletionEvent({ library_id: 1, status: 'deleted', error: null });
  expect(libraries.items.value).toEqual([]);
});

test('deletion events outrank', async () => {
  for (const status of ['failed', 'deleted'] as const) {
    const ack = deferred<Response>();
    const callbacks = new Set<(event: unknown) => void>();
    const messages: string[] = [];
    const exported = await load(
      'composables/useLibraries.ts',
      {
        '~/utils': {
          request: async (_url: string, options?: RequestInit) =>
            options?.method === 'DELETE'
              ? ack.promise
              : Response.json([
                  { id: 1, name: 'Movies', suspended: false, deleted: false, delete_error: null },
                ]),
        },
        '~/composables/useEvents': {
          useEvents: () => ({
            on: (callback: (event: unknown) => void) => {
              callbacks.add(callback);
              return () => callbacks.delete(callback);
            },
          }),
        },
        '~/composables/useApiErrorMessage': { useApiErrorMessage: () => ({ messageFor: String }) },
      },
      {
        useNotification: () => ({
          info: (message: string) => messages.push(message),
          error: () => {},
        }),
      },
    );
    const libraries = (exported.useLibraries as typeof useLibraries)();
    await libraries.ensure();
    const pending = libraries.deleteLibrary({ id: 1, name: 'Movies' });
    const payload = {
      library_id: 1,
      status,
      error: status === 'failed' ? 'Could not cancel work.' : null,
    };
    try {
      exported.applyLibraryDeletionEvent(payload);
      for (const callback of callbacks) callback({ type: 'library.deletion', payload });
    } finally {
      ack.resolve(Response.json({ library_id: 1, status: 'deleting' }, { status: 202 }));
      await pending;
    }
    if (status === 'failed') expect(libraries.items.value[0]?.delete_error).toBe(payload.error);
    else expect(libraries.items.value).toEqual([]);
    expect(callbacks.size).toBe(0);
    expect(messages).toEqual([]);
  }
});

test('deletion request fails', async () => {
  const messages: string[] = [];
  const exported = await load(
    'composables/useLibraries.ts',
    {
      '~/utils': {
        request: async (_url: string, options?: RequestInit) =>
          options?.method === 'DELETE'
            ? Response.json({ detail: 'Deletion unavailable.' }, { status: 409 })
            : Response.json([
                { id: 1, name: 'Movies', suspended: false, deleted: false, delete_error: null },
              ]),
        parse_api_error: async (body: { detail: string }) => body.detail,
      },
      '~/composables/useEvents': { useEvents: () => ({ on: () => () => {} }) },
      '~/composables/useApiErrorMessage': { useApiErrorMessage: () => ({ messageFor: String }) },
    },
    {
      useNotification: () => ({
        info: () => {},
        error: (message: string) => messages.push(message),
      }),
    },
  );
  const libraries = (exported.useLibraries as typeof useLibraries)();
  await libraries.ensure();
  expect(await libraries.deleteLibrary({ id: 1, name: 'Movies' })).toBe(false);
  expect(libraries.items.value[0]?.deleted).toBe(false);
  expect(messages).toEqual(['Deletion unavailable.']);
});

test('library rejects stale drafts', async () => {
  const editing: LibraryRow = {
    id: 1,
    name: 'movies',
    roots: ['/media'],
    cooldown_s: 0,
    schedule: null,
    watch: false,
    scan_on_start: false,
    scan_interval_s: null,
    mkv_stats: false,
    healthcheck_enabled: false,
    processing_cutoff: {},
    trash_mode: 'none',
    trash_retention_days: 30,
    flow_id: null,
    suspended: false,
    created_at: '',
    updated_at: 'opened',
  };
  const {
    id: _id,
    created_at: _created,
    updated_at: _updated,
    suspended: _suspended,
    ...body
  } = editing;
  const payload: LibraryBody = { ...body, name: 'renamed', healthcheck_enabled: true };
  for (const state of ['changed', 'offline', 'same'] as const) {
    const writes: unknown[] = [];
    const errors: string[] = [];
    const exported = await load(
      'composables/useLibraries.ts',
      {
        '~/utils': {
          request: async (path: string, options?: RequestInit) => {
            if (options?.method) {
              writes.push(JSON.parse(String(options.body)));
              return Response.json({ ...editing, name: 'renamed' });
            }
            if (state === 'offline') throw new Error('offline');
            if (path === '/api/libraries') return Response.json([editing]);
            return Response.json(
              state === 'changed'
                ? { ...editing, updated_at: 'new', healthcheck_enabled: true }
                : editing,
            );
          },
        },
        '~/composables/useApiErrorMessage': {
          useApiErrorMessage: () => ({ messageFor: (text: string) => text }),
        },
      },
      {
        useNotification: () => ({ error: (text: string) => errors.push(text), success: () => {} }),
      },
    );
    const libraries = (exported.useLibraries as typeof useLibraries)();
    const result = await libraries.save(payload, editing);
    if (state === 'same') {
      expect(result).toBeNull();
      expect(writes).toEqual([{ name: 'renamed', healthcheck_enabled: true }]);
    } else {
      expect(writes).toEqual([]);
      expect(errors).toEqual([result!]);
    }
    expect(payload.trash_mode).toBe('none');
    expect(payload.healthcheck_enabled).toBe(true);
  }
});

test('library saves healthcheck', async () => {
  const body: LibraryBody = {
    name: 'movies',
    roots: ['/media'],
    cooldown_s: 0,
    schedule: null,
    watch: false,
    scan_on_start: false,
    scan_interval_s: null,
    mkv_stats: false,
    healthcheck_enabled: false,
    processing_cutoff: {},
    trash_mode: 'none',
    trash_retention_days: 30,
    flow_id: null,
  };
  const current: LibraryRow = {
    ...body,
    id: 1,
    suspended: false,
    created_at: '',
    updated_at: 'opened',
  };
  for (const enabled of [false, true]) {
    const payload = { ...body, healthcheck_enabled: enabled };
    for (const editing of [null, { ...current, healthcheck_enabled: !enabled }, current]) {
      const writes: Array<{ method: string; body: unknown }> = [];
      const exported = await load(
        'composables/useLibraries.ts',
        {
          '~/utils': {
            request: async (path: string, options?: RequestInit) => {
              if (options?.method) {
                writes.push({ method: options.method, body: JSON.parse(String(options.body)) });
                return Response.json({ ...current, ...payload });
              }
              return Response.json(path === '/api/libraries' ? [current] : editing);
            },
          },
          '~/composables/useApiErrorMessage': {
            useApiErrorMessage: () => ({ messageFor: (text: string) => text }),
          },
        },
        { useNotification: () => ({ error: () => {}, success: () => {} }) },
      );
      expect(
        await (exported.useLibraries as typeof useLibraries)().save(payload, editing),
      ).toBeNull();
      expect(writes).toEqual([
        {
          method: editing ? 'PATCH' : 'POST',
          body: editing
            ? editing.healthcheck_enabled === enabled
              ? {}
              : { healthcheck_enabled: enabled }
            : payload,
        },
      ]);
    }
  }
});

test('library saves cutoff', async () => {
  const current: LibraryRow = {
    id: 1,
    name: 'movies',
    roots: ['/media'],
    cooldown_s: 0,
    schedule: null,
    watch: false,
    scan_on_start: false,
    scan_interval_s: null,
    mkv_stats: false,
    healthcheck_enabled: true,
    processing_cutoff: { at: '2026-10-01T00:00:00Z', fields: ['created_at'], match: 'all' },
    trash_mode: 'none',
    trash_retention_days: 30,
    flow_id: null,
    suspended: false,
    created_at: '',
    updated_at: 'opened',
  };
  const {
    id: _id,
    created_at: _created,
    updated_at: _updated,
    suspended: _suspended,
    ...body
  } = current;
  const changes: Array<Partial<LibraryBody>> = [
    {},
    { processing_cutoff: {} },
    {
      processing_cutoff: { at: '2026-10-10T12:30:00Z', fields: ['created_at'], match: 'all' },
    },
    {
      processing_cutoff: {
        at: '2026-10-01T00:00:00Z',
        fields: ['first_seen', 'mtime'],
        match: 'any',
      },
    },
  ];
  for (const change of changes) {
    for (const editing of [null, current]) {
      const writes: Array<{ method: string; body: unknown }> = [];
      const payload: LibraryBody = { ...body, ...change };
      const exported = await load(
        'composables/useLibraries.ts',
        {
          '~/utils': {
            request: async (path: string, options?: RequestInit) => {
              if (options?.method) {
                writes.push({ method: options.method, body: JSON.parse(String(options.body)) });
                return Response.json({ ...current, ...payload });
              }
              return Response.json(path === '/api/libraries' ? [current] : current);
            },
          },
          '~/composables/useApiErrorMessage': {
            useApiErrorMessage: () => ({ messageFor: (text: string) => text }),
          },
        },
        { useNotification: () => ({ error: () => {}, success: () => {} }) },
      );
      expect(
        await (exported.useLibraries as typeof useLibraries)().save(payload, editing),
      ).toBeNull();
      expect(writes).toEqual([
        { method: editing ? 'PATCH' : 'POST', body: editing ? change : payload },
      ]);
    }
  }
});

test('pause updates suspension', async () => {
  const library: LibraryRow = {
    id: 1,
    name: 'movies',
    roots: ['/media'],
    cooldown_s: 0,
    schedule: null,
    watch: false,
    scan_on_start: false,
    scan_interval_s: null,
    mkv_stats: false,
    healthcheck_enabled: false,
    processing_cutoff: {},
    trash_mode: 'none',
    trash_retention_days: 30,
    flow_id: null,
    suspended: false,
    created_at: '',
    updated_at: 'opened',
  };
  for (const outcome of ['success', 'rejected', 'offline'] as const) {
    const calls: string[] = [];
    const errors: string[] = [];
    const successes: string[] = [];
    let suspended = false;
    const exported = await load(
      'composables/useLibraries.ts',
      {
        '~/utils': {
          request: async (path: string, options?: RequestInit) => {
            calls.push(`${options?.method ?? 'GET'} ${path}`);
            if (options?.method === 'PATCH') {
              expect(JSON.parse(String(options.body))).toEqual({ suspended: true });
              if (outcome === 'offline') throw new Error('offline');
              if (outcome === 'rejected') {
                return Response.json({ detail: 'Pause refused.' }, { status: 409 });
              }
              suspended = true;
              return Response.json({ ...library, suspended });
            }
            return Response.json([{ ...library, suspended }]);
          },
          parse_api_error: async (body: { detail: string }) => body.detail,
        },
        '~/composables/useApiErrorMessage': {
          useApiErrorMessage: () => ({ messageFor: (text: string) => text }),
        },
      },
      {
        useNotification: () => ({
          error: (text: string) => errors.push(text),
          success: (text: string) => successes.push(text),
        }),
      },
    );
    const libraries = (exported.useLibraries as typeof useLibraries)();
    await libraries.ensure();
    calls.length = 0;
    await libraries.setSuspended(library, true);
    expect(calls).toEqual(
      outcome === 'success'
        ? ['PATCH /api/libraries/1', 'GET /api/libraries']
        : ['PATCH /api/libraries/1'],
    );
    expect(libraries.items.value[0]?.suspended).toBe(outcome === 'success');
    expect(successes).toHaveLength(outcome === 'success' ? 1 : 0);
    expect(errors).toEqual(
      outcome === 'success' ? [] : [outcome === 'offline' ? 'offline' : 'Pause refused.'],
    );
  }
});

test('resume checks freshness', async () => {
  const reviewed: LibraryRow = {
    id: 1,
    name: 'movies',
    roots: ['/media'],
    cooldown_s: 0,
    schedule: null,
    watch: false,
    scan_on_start: false,
    scan_interval_s: null,
    mkv_stats: false,
    healthcheck_enabled: false,
    processing_cutoff: {},
    trash_mode: 'none',
    trash_retention_days: 30,
    flow_id: null,
    suspended: true,
    created_at: '',
    updated_at: 'reviewed',
  };
  for (const state of ['current', 'changed', 'deleted', 'offline'] as const) {
    const calls: string[] = [];
    const errors: string[] = [];
    const exported = await load(
      'composables/useLibraries.ts',
      {
        '~/utils': {
          request: async (path: string, options?: RequestInit) => {
            calls.push(`${options?.method ?? 'GET'} ${path}`);
            if (options?.method === 'PATCH') {
              expect(JSON.parse(String(options.body))).toEqual({ suspended: false });
              return Response.json(reviewed);
            }
            if (state === 'offline') throw new Error('offline');
            if (state === 'deleted') return new Response(null, { status: 404 });
            return Response.json(
              path === '/api/libraries'
                ? [reviewed]
                : {
                    ...reviewed,
                    updated_at: state === 'changed' ? 'changed' : 'reviewed',
                  },
            );
          },
        },
        '~/composables/useApiErrorMessage': {
          useApiErrorMessage: () => ({ messageFor: (text: string) => text }),
        },
      },
      {
        useNotification: () => ({ error: (text: string) => errors.push(text), success: () => {} }),
      },
    );
    await (exported.useLibraries as typeof useLibraries)().setSuspended(reviewed, false);
    expect(calls).toEqual(
      state === 'current'
        ? ['GET /api/libraries/1', 'PATCH /api/libraries/1', 'GET /api/libraries']
        : ['GET /api/libraries/1'],
    );
    if (state === 'current') expect(errors).toEqual([]);
    else expect(errors).toHaveLength(1);
  }
});

test('notification checks freshness', async () => {
  const editing = { name: 'mail', url: 'mailto://kept', created_at: '', updated_at: 'opened' };
  for (const state of ['current', 'changed', 'deleted', 'offline', 'unavailable'] as const) {
    const calls: string[] = [];
    const exported = await load('composables/useNotificationTargets.ts', {
      '~/utils': {
        ApiError: Error,
        parse_api_error: async () => 'unavailable',
        request: async (path: string, options?: RequestInit) => {
          calls.push(`${options?.method ?? 'GET'} ${path}`);
          if (options?.method === 'PUT') {
            expect(JSON.parse(String(options.body))).toEqual({ url: 'mailto://draft' });
            return Response.json({ ...editing, url: 'mailto://draft' });
          }
          if (state === 'offline') throw new Error('offline');
          if (state === 'unavailable') return new Response(null, { status: 503 });
          return Response.json(
            state === 'deleted'
              ? []
              : [
                  {
                    ...editing,
                    updated_at: state === 'changed' ? 'changed' : 'opened',
                  },
                ],
          );
        },
      },
    });
    const result = await (exported.useNotificationTargets as typeof useNotificationTargets)().save(
      'mail',
      'mailto://draft',
      editing,
    );
    expect(result.ok).toBe(state === 'current');
    expect(calls).toEqual(
      state === 'current'
        ? ['GET /api/notifications', 'PUT /api/notifications/mail', 'GET /api/notifications']
        : ['GET /api/notifications'],
    );
    expect(editing.url).toBe('mailto://kept');
  }
});

test('notifications reject duplicate creation', async () => {
  let writes = 0;
  let offline = false;
  const target = { name: 'mail', url: 'mailto://kept', created_at: '', updated_at: '' };
  const exported = await load('composables/useNotificationTargets.ts', {
    '~/utils': {
      request: async (_path: string, options?: RequestInit) => {
        if (options?.method === 'PUT') {
          writes++;
          return Response.json(target);
        }
        if (offline) throw new Error('offline');
        return Response.json([target]);
      },
    },
  });
  const targets = (exported.useNotificationTargets as typeof useNotificationTargets)();
  expect((await targets.save('mail', 'mailto://new', null)).ok).toBe(false);
  expect(writes).toBe(0);
  offline = true;
  expect((await targets.save('other', 'mailto://new', null)).ok).toBe(false);
  expect(writes).toBe(0);
  offline = false;
  expect((await targets.save('mail', 'mailto://new', target)).ok).toBe(true);
  expect(writes).toBe(1);
});

test('plugins retain committed state', async () => {
  const plugin = { manifest: { id: 'test', name: 'Test' }, state: 'ready' } as PluginRow;
  let offline = false;
  const warnings: string[] = [];
  const errors: string[] = [];
  const exported = await load(
    'composables/usePlugins.ts',
    {
      '~/utils': {
        request: async (_path: string, options?: RequestInit) => {
          if (options?.method) return Response.json({ ...plugin, state: 'disabled' });
          if (offline) throw new Error('offline');
          return Response.json([plugin]);
        },
      },
      '~/composables/useApiErrorMessage': {
        useApiErrorMessage: () => ({ messageFor: (text: string) => text }),
      },
    },
    {
      useNotification: () => ({
        success: () => {},
        warning: (text: string) => warnings.push(text),
        error: (text: string) => errors.push(text),
      }),
    },
  );
  const plugins = (exported.usePlugins as typeof usePlugins)();
  await plugins.ensure();
  offline = true;
  await plugins.setEnabled(plugin, false);
  expect(plugins.items.value[0]?.state).toBe('disabled');
  expect(plugins.lastError.value).not.toBeNull();
  expect(warnings).toHaveLength(1);
  expect(errors).toEqual([]);
});

test('node rename patches name', async () => {
  const bodies: unknown[] = [];
  const exported = await load(
    'composables/useNodes.ts',
    {
      '~/utils': {
        request: async (_path: string, options?: RequestInit) => {
          bodies.push(JSON.parse(String(options?.body)));
          return Response.json({ id: 1, display_name: 'renamed', max_transcodes: 1 });
        },
      },
      '~/composables/useApiErrorMessage': {
        useApiErrorMessage: () => ({ messageFor: (text: string) => text }),
      },
    },
    { useNotification: () => ({ success: () => {}, error: () => {} }) },
  );
  const nodes = (exported.useNodes as typeof useNodes)();
  const row = await nodes.update(1, { display_name: 'renamed' });
  expect(bodies).toEqual([{ display_name: 'renamed' }]);
  expect(row.max_transcodes).toBe(1);
});
