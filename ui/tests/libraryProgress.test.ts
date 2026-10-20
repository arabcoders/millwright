import { expect, test } from 'bun:test';
import { computed, ref, shallowReadonly } from 'vue';
import type { ServerEvent } from '../app/composables/useEvents';
import type { LibraryProgress } from '../app/types/api';

const source = await Bun.file(
  new URL('../app/composables/useLibraryProgress.ts', import.meta.url),
).text();
const code = new Bun.Transpiler({ loader: 'ts' })
  .transformSync(source)
  .replace(/^import[\s\S]*?from\s+["'][^"']+["'];?\s*$/gm, '')
  .replace('export const useLibraryProgress', 'const useLibraryProgress');

const sample = (id: number, processed = 0): LibraryProgress => ({
  library_id: id,
  scanner: { paused: false, next_run_at: null, discovery_pending: false },
  discovery: {
    status: 'completed',
    found: 5,
    added: 5,
    updated: 0,
    error_count: 0,
    error: null,
    reason: 'manual',
    latest_result: 'completed',
    finished_at: '2026-01-01T12:00:00+00:00',
  },
  probing: { status: 'scanning', processed, remaining: 5 - processed, error_count: 0, active: [] },
});
const snapshot = (libraries: LibraryProgress[], paused = false) => ({
  scanner: { paused, failed: false },
  probing: { failed: false },
  libraries,
});

const setup = () => {
  const callbacks: Array<(event: ServerEvent) => void> = [];
  const reconnects: Array<() => void> = [];
  const calls: Array<{ url: string; options?: RequestInit }> = [];
  const feedback: string[] = [];
  let respond = async (url: string): Promise<Response> =>
    url.endsWith('/progress')
      ? Response.json(snapshot([sample(1)]))
      : Response.json({ paused: url.endsWith('/pause'), failed: false });
  const useLibraryProgress: typeof import('../app/composables/useLibraryProgress').useLibraryProgress =
    new Function(
      'computed',
      'ref',
      'shallowReadonly',
      'request',
      'useEvents',
      'parse_api_error',
      'useApiErrorMessage',
      'useNotification',
      `${code}\nreturn useLibraryProgress;`,
    )(
      computed,
      ref,
      shallowReadonly,
      (url: string, options?: RequestInit) => {
        calls.push({ url, options });
        return respond(url);
      },
      () => ({
        on: (callback: (event: ServerEvent) => void) => callbacks.push(callback),
        onReconnect: (callback: () => void) => reconnects.push(callback),
      }),
      (body: { detail?: string }) => body?.detail,
      () => ({ messageFor: (text: string) => text }),
      () => ({ success: () => feedback.push('success'), error: () => feedback.push('error') }),
    );
  return {
    store: useLibraryProgress(),
    calls,
    feedback,
    emit: (event: ServerEvent) => callbacks.forEach((callback) => callback(event)),
    reconnect: () => reconnects.forEach((callback) => callback()),
    respond: (next: typeof respond) => {
      respond = next;
    },
  };
};

test('progress updates directly', async () => {
  const env = setup();
  await env.store.ensure();
  expect(env.store.items.value[1]).toEqual(sample(1));
  expect(env.calls).toEqual([{ url: '/api/libraries/progress', options: { cache: 'no-store' } }]);
  env.emit({ type: 'library.progress', payload: sample(1, 3) });
  env.emit({ type: 'library.progress', payload: sample(2, 1) });
  await env.store.ensure();
  expect(env.store.items.value[1]?.probing.processed).toBe(3);
  expect(env.store.items.value[2]?.probing.processed).toBe(1);
  expect(env.calls).toHaveLength(1);
});

test('active tools update live', async () => {
  const env = setup();
  const progress = sample(1);
  progress.probing.active = [
    {
      media_file_id: 7,
      path: '/media/movie.mkv',
      tool: 'mkvpropedit',
      started_at: '2026-01-01T12:00:00Z',
      percent: 42,
    },
  ];
  env.respond(async () => Response.json(snapshot([progress])));
  await env.store.load();
  expect(env.store.summary.value.activeProbes).toBe(1);
  expect(env.store.items.value[1]?.probing.active[0]?.percent).toBe(42);
  env.emit({
    type: 'library.progress',
    payload: { ...progress, probing: { ...progress.probing, active: [] } },
  });
  expect(env.store.summary.value.activeProbes).toBe(0);
  expect(env.store.items.value[1]?.probing.active).toEqual([]);
  expect(env.calls).toHaveLength(1);
});

test('progress totals files', async () => {
  const env = setup();
  const pending = sample(1, 1262);
  pending.probing.remaining = 23248;
  const complete = sample(2, 5);
  complete.probing.status = 'completed';
  env.respond(async () => Response.json(snapshot([pending, complete])));
  await env.store.load();
  expect(env.store.summary.value).toEqual({
    processed: 1267,
    remaining: 23248,
    activeProbes: 0,
    found: 0,
    discoveryStatus: 'idle',
    discoveryLibraryId: null,
    queued: 0,
    probingStatus: 'scanning',
  });
  env.emit({
    type: 'library.progress',
    payload: { ...pending, probing: { ...pending.probing, processed: 1263, remaining: 23247 } },
  });
  expect(env.store.summary.value.processed).toBe(1268);
  expect(env.store.summary.value.remaining).toBe(23247);
});

test('discovery totals active scans', async () => {
  const env = setup();
  const scanning = sample(1);
  scanning.discovery.status = 'scanning';
  const queued = sample(2);
  queued.discovery.status = 'queued';
  queued.discovery.found = 0;
  env.respond(async () => Response.json(snapshot([scanning, queued, sample(3)])));
  await env.store.load();
  expect(env.store.summary.value.found).toBe(5);
  expect(env.store.summary.value.discoveryStatus).toBe('scanning');
  expect(env.store.summary.value.discoveryLibraryId).toBe(1);
  expect(env.store.summary.value.queued).toBe(1);
  env.emit({
    type: 'library.progress',
    payload: { ...scanning, discovery: { ...scanning.discovery, status: 'completed' } },
  });
  expect(env.store.summary.value.found).toBe(0);
  expect(env.store.summary.value.discoveryStatus).toBe('queued');
  expect(env.store.summary.value.discoveryLibraryId).toBe(2);
  env.emit({
    type: 'library.progress',
    payload: { ...queued, discovery: { ...queued.discovery, status: 'completed' } },
  });
  expect(env.store.summary.value.discoveryStatus).toBe('idle');
  expect(env.store.summary.value.discoveryLibraryId).toBeNull();
});

test('paused work remains visible', async () => {
  const env = setup();
  const progress = sample(1, 2);
  progress.scanner.paused = true;
  progress.discovery.status = 'paused';
  progress.probing.status = 'paused';
  env.respond(async () => Response.json(snapshot([progress], true)));
  await env.store.load();
  expect(env.store.summary.value.remaining).toBe(3);
  expect(env.store.summary.value.discoveryStatus).toBe('paused');
  expect(env.store.summary.value.probingStatus).toBe('paused');
  progress.discovery.status = 'cancelling';
  progress.scanner.discovery_pending = true;
  env.store.update(progress);
  expect(env.store.summary.value.discoveryStatus).toBe('cancelling');
  expect(env.store.items.value[1]?.scanner.paused).toBe(true);
  expect(env.calls).toHaveLength(1);
});

test('running stages survive pause', async () => {
  const env = setup();
  const active = sample(1, 2);
  active.discovery.status = 'scanning';
  active.scanner.discovery_pending = true;
  const waiting = sample(2);
  waiting.discovery.status = 'paused';
  waiting.scanner.discovery_pending = true;
  waiting.probing.status = 'paused';
  env.respond(async () => Response.json(snapshot([active, waiting], true)));
  await env.store.load();
  expect(env.store.summary.value.discoveryStatus).toBe('scanning');
  expect(env.store.summary.value.discoveryLibraryId).toBe(1);
  expect(env.store.summary.value.queued).toBe(1);
  expect(env.store.summary.value.probingStatus).toBe('scanning');
  env.store.update({
    ...active,
    discovery: { ...active.discovery, status: 'completed' },
    scanner: { ...active.scanner, discovery_pending: false },
    probing: { ...active.probing, status: 'paused' },
  });
  expect(env.store.summary.value.discoveryStatus).toBe('paused');
  expect(env.store.summary.value.probingStatus).toBe('paused');
});

test('probing tracks independent stages', async () => {
  const env = setup();
  const pending = sample(1);
  pending.probing.status = 'queued';
  env.respond(async () => Response.json(snapshot([pending])));
  await env.store.load();
  expect(env.store.summary.value.discoveryStatus).toBe('idle');
  expect(env.store.summary.value.probingStatus).toBe('queued');
  env.store.update({
    ...pending,
    probing: { ...pending.probing, status: 'completed', remaining: 0 },
  });
  expect(env.store.summary.value.probingStatus).toBe('idle');
  env.store.update({
    ...pending,
    probing: { ...pending.probing, status: 'failed', remaining: 0, error_count: 1 },
  });
  expect(env.store.summary.value.probingStatus).toBe('idle');
  env.store.update({
    ...pending,
    discovery: { ...pending.discovery, status: 'failed', error_count: 1 },
    probing: { ...pending.probing, status: 'failed', remaining: 0, error_count: 1 },
  });
  expect(env.store.summary.value.discoveryStatus).toBe('idle');
  expect(env.store.summary.value.probingStatus).toBe('idle');
  env.emit({ type: 'probing.state', payload: { failed: true } });
  expect(env.store.summary.value.probingStatus).toBe('failed');
  env.emit({ type: 'scanner.state', payload: { paused: false, failed: true } });
  expect(env.store.summary.value.discoveryStatus).toBe('failed');
});

test('events outrank snapshot', async () => {
  const env = setup();
  let resolve!: (value: Response) => void;
  env.respond(
    () =>
      new Promise<Response>((done) => {
        resolve = done;
      }),
  );
  const pending = env.store.load();
  const concurrent = env.store.load();
  try {
    env.emit({ type: 'library.progress', payload: sample(1, 2) });
    env.emit({ type: 'library.progress', payload: sample(1, 3) });
    env.emit({ type: 'library.progress', payload: sample(2, 1) });
  } finally {
    resolve(Response.json(snapshot([sample(1), sample(3)])));
    await Promise.all([pending, concurrent]);
  }
  expect(env.store.items.value[1]).toEqual(sample(1, 3));
  expect(env.store.items.value[2]).toEqual(sample(2, 1));
  expect(env.store.items.value[3]).toEqual(sample(3));
  expect(env.store.isLoading.value).toBe(false);
  expect(env.calls).toHaveLength(1);
});

test('live progress updates', async () => {
  const env = setup();
  await env.store.load();
  env.reconnect();
  env.emit({ type: 'library.progress', payload: sample(1, 3) });
  let resolve!: (value: Response) => void;
  env.respond(
    () =>
      new Promise<Response>((done) => {
        resolve = done;
      }),
  );
  const pending = env.store.load();
  try {
    env.emit({ type: 'events.resync', payload: { reason: 'overflow' } });
  } finally {
    resolve(Response.json(snapshot([sample(1, 3)])));
    await pending;
  }
  env.respond(async () => new Response(null, { status: 503 }));
  await env.store.load();
  expect(env.store.lastError.value).toBeInstanceOf(Error);
  expect(env.store.items.value[1]).toEqual(sample(1, 3));
  env.respond(async () => Response.json(snapshot([sample(1, 4)])));
  await env.store.ensure();
  expect(env.store.lastError.value).toBeNull();
  expect(env.store.items.value[1]).toEqual(sample(1, 4));
});

test('global state includes emptiness', async () => {
  const env = setup();
  expect(env.store.scanner.value).toBeNull();
  await env.store.setPaused(true);
  expect(env.calls).toHaveLength(0);
  env.respond(async () => Response.json(snapshot([], true)));
  await env.store.load();
  expect(env.store.scanner.value?.paused).toBe(true);
  env.emit({ type: 'library.progress', payload: sample(1) });
  env.emit({ type: 'library.progress', payload: sample(2) });
  expect(Object.values(env.store.items.value).every((row) => row.scanner.paused)).toBe(true);
  env.emit({ type: 'scanner.state', payload: { paused: false, failed: false } });
  expect(env.store.scanner.value?.paused).toBe(false);
  expect(Object.values(env.store.items.value).every((row) => !row.scanner.paused)).toBe(true);
  expect(env.calls).toHaveLength(1);
});

test('global events outrank snapshot', async () => {
  const env = setup();
  let resolve!: (value: Response) => void;
  env.respond(
    () =>
      new Promise<Response>((done) => {
        resolve = done;
      }),
  );
  const pending = env.store.load();
  try {
    await env.store.setPaused(true);
    expect(env.calls).toHaveLength(1);
    env.emit({ type: 'scanner.state', payload: { paused: true, failed: false } });
    env.emit({ type: 'probing.state', payload: { failed: true } });
  } finally {
    resolve(Response.json(snapshot([sample(1), sample(2)])));
    await pending;
  }
  expect(env.store.scanner.value?.paused).toBe(true);
  expect(Object.values(env.store.items.value).every((row) => row.scanner.paused)).toBe(true);
  expect(env.store.summary.value.probingStatus).toBe('failed');
});

test('global control guards busy', async () => {
  const env = setup();
  await env.store.load();
  let resolve!: (value: Response) => void;
  env.respond(
    () =>
      new Promise<Response>((done) => {
        resolve = done;
      }),
  );
  const pending = env.store.setPaused(true);
  try {
    expect(env.store.scannerBusy.value).toBe(true);
    await env.store.setPaused(false);
    expect(env.calls).toHaveLength(2);
    expect(env.calls[1]).toEqual({
      url: '/api/libraries/scanner/pause',
      options: { method: 'POST' },
    });
  } finally {
    resolve(Response.json({ paused: true, failed: false }));
    await pending;
  }
  expect(env.store.scannerBusy.value).toBe(false);
  expect(env.store.scanner.value?.paused).toBe(true);
  expect(env.store.items.value[1]?.scanner.paused).toBe(true);
  expect(env.feedback).toEqual(['success']);
  env.respond(async () => Response.json({ detail: 'Unavailable' }, { status: 503 }));
  await env.store.setPaused(false);
  expect(env.store.scannerBusy.value).toBe(false);
  expect(env.store.scanner.value?.paused).toBe(true);
  expect(env.feedback).toEqual(['success', 'error']);
  const calls = env.calls.length;
  await env.store.setPaused(false);
  expect(env.calls).toHaveLength(calls + 1);
});
