import { expect, test } from 'bun:test';
import * as Vue from 'vue';
import ts from 'typescript';
import { readFileSync } from 'node:fs';
import type { useAuth } from '../app/composables/useAuth';
import type { ServerEvent, useEvents } from '../app/composables/useEvents';
import type { useJobs } from '../app/composables/useJobs';
import * as jobList from '../app/utils/jobList';

const load = <T>(
  path: string,
  modules: Record<string, unknown>,
  globals: Record<string, unknown>,
  exports: object = {},
  extra = '',
): T => {
  const source = readFileSync(new URL(`../app/${path}`, import.meta.url), 'utf8').replaceAll(
    'import.meta.client',
    'true',
  );
  const code = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  new Function('require', 'exports', ...Object.keys(globals), code + extra)(
    (name: string) => modules[name],
    exports,
    ...Object.values(globals),
  );
  return exports as T;
};

class Socket {
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {}
  close() {
    this.closed = true;
    this.onclose?.();
  }
}

const setup = async (onSocket?: (socket: Socket) => void) => {
  const storage = new Map<string, string>();
  const sockets: Socket[] = [];
  const timers = new Map<number, () => void>();
  let timerId = 0;
  const calls: Array<{ url: string; options: RequestInit }> = [];
  let respond = async (url: string): Promise<Response> =>
    url === '/api/auth/me'
      ? Response.json({ id: 1, username: 'user', auth_method: 'session' })
      : Response.json({ ticket: 'ticket' });
  const config = { public: { wss: 'wss://example.test' }, app: { baseURL: '/' } };
  const globals = {
    useRuntimeConfig: () => config,
    window: { location: { protocol: 'https:', host: 'ui.example.test:8082' } },
    localStorage: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
      removeItem: (key: string) => storage.delete(key),
    },
    fetch: (url: string, options: RequestInit) => {
      calls.push({ url, options });
      return respond(url);
    },
    navigateTo() {},
    WebSocket: class extends Socket {
      constructor(url: string) {
        super(url);
        sockets.push(this);
        onSocket?.(this);
      }
    },
    setTimeout: (callback: () => void) => {
      timers.set(++timerId, callback);
      return timerId;
    },
    clearTimeout: (id: number) => timers.delete(id),
  };
  const authModule: { useAuth?: typeof useAuth; invalidateAuth?: () => void } = {};
  const modules: Record<string, unknown> = {
    vue: Vue,
    '~/composables/useAuth': authModule,
    '~/utils/diagnostic': { diagnostic() {} },
  };
  modules['~/utils'] = load('utils/index.ts', modules, globals);
  load('composables/useAuth.ts', modules, globals, authModule);
  const auth = authModule.useAuth!();
  await auth.probe();
  const scope = Vue.effectScope();
  let events!: {
    useEvents: typeof useEvents;
    connect: () => Promise<void>;
    wsUrl: () => string;
  };
  scope.run(() => {
    events = load(
      'composables/useEvents.ts',
      modules,
      globals,
      {},
      '; exports.connect = connect; exports.wsUrl = wsUrl;',
    );
  });
  return {
    auth,
    events,
    modules,
    globals,
    sockets,
    timers,
    storage,
    config,
    calls,
    respond: (handler: typeof respond) => (respond = handler),
    close: () => {
      authModule.invalidateAuth!();
      scope.stop();
    },
  };
};

test('logout closes socket', async () => {
  const state = await setup();
  try {
    await state.events.connect();
    const events = state.events.useEvents();
    let received = 0;
    events.on(() => received++);
    const socket = state.sockets[0]!;
    expect(socket.url).toBe('wss://example.test/api/events?ticket=ticket');
    socket.onopen?.();
    expect(events.connected.value).toBe(true);
    await state.auth.logout();
    socket.onmessage?.({ data: JSON.stringify({ type: 'job.deleted', payload: { job_id: 1 } }) });
    expect(socket.closed).toBe(true);
    expect(events.connected.value).toBe(false);
    expect(received).toBe(0);
    expect(state.timers.size).toBe(0);
    expect(state.auth.status.value).toBeNull();
  } finally {
    state.close();
  }
});

test('batch file dispatch', async () => {
  const state = await setup();
  try {
    await state.events.connect();
    const events = state.events.useEvents();
    const received: ServerEvent[] = [];
    events.on((event) => received.push(event));
    state.sockets[0]!.onmessage?.({
      data: JSON.stringify({
        type: 'events.batch',
        payload: {
          events: [
            {
              type: 'file.status',
              payload: { action: 'pruned', library_id: 1, media_file_id: 42 },
            },
          ],
        },
      }),
    });
    expect(received).toEqual([
      {
        type: 'file.status',
        payload: { action: 'pruned', library_id: 1, media_file_id: 42 },
      },
    ]);
  } finally {
    state.close();
  }
});

test('deletion events dispatch', async () => {
  const state = await setup();
  try {
    await state.events.connect();
    const received: ServerEvent[] = [];
    state.events.useEvents().on((event) => received.push(event));
    const events: ServerEvent[] = [
      {
        type: 'job.deletion',
        payload: { job_id: 1, outcome: 'failed', reason: 'Workspace cleanup failed.' },
      },
      { type: 'job.deleted', payload: { job_id: 2 } },
      { type: 'job.deletion', payload: { job_id: 2, outcome: 'deleted', reason: 'Deleted.' } },
    ];
    for (const event of events) state.sockets[0]!.onmessage?.({ data: JSON.stringify(event) });
    expect(received).toEqual(events);
  } finally {
    state.close();
  }
});

test('failed logout keeps session', async () => {
  const state = await setup();
  try {
    await state.events.connect();
    state.sockets[0]!.onopen?.();
    state.respond(async () => Response.json({ detail: 'Revocation unavailable' }, { status: 500 }));
    await expect(state.auth.logout()).rejects.toMatchObject({ status: 500 });
    expect(state.auth.status.value?.username).toBe('user');
    expect(state.events.useEvents().connected.value).toBe(true);
    expect(state.sockets[0]!.closed).toBe(false);
    state.respond(async () => new Response(null, { status: 204 }));
    await state.auth.logout();
    expect(state.sockets[0]!.closed).toBe(true);
  } finally {
    state.close();
  }
});

test('offline logout keeps session', async () => {
  const state = await setup();
  try {
    state.respond(async () => {
      throw new Error('offline');
    });
    await expect(state.auth.logout()).rejects.toThrow();
    expect(state.auth.status.value?.username).toBe('user');
  } finally {
    state.close();
  }
});

test('expired logout clears session', async () => {
  const state = await setup();
  try {
    state.respond(async () => new Response(null, { status: 401 }));
    await state.auth.logout();
    expect(state.auth.status.value).toBeNull();
  } finally {
    state.close();
  }
});

test('setup establishes session', async () => {
  let opened!: (socket: Socket) => void;
  const socket = new Promise<Socket>((resolve) => (opened = resolve));
  const state = await setup(opened);
  try {
    await state.auth.logout();
    state.respond(async (url) =>
      url === '/api/auth/setup'
        ? Response.json({ id: 2, username: 'new-user', auth_method: 'session' })
        : Response.json({ ticket: 'new-ticket' }),
    );
    await state.auth.login('new-user', 'password', true);
    await socket;
    expect(state.auth.status.value).toEqual({
      id: 2,
      username: 'new-user',
      auth_method: 'session',
    });
    expect(state.sockets.at(-1)?.url).toContain('ticket=new-ticket');
    const call = state.calls.find((call) => call.url === '/api/auth/setup')!;
    expect(JSON.parse(String(call.options.body))).toEqual({
      username: 'new-user',
      password: 'password',
    });
    expect(call.options.credentials).toBe('same-origin');
    expect(new Headers(call.options.headers).get('X-Millwright-Request')).toBe('1');
    expect(new Headers(call.options.headers).has('Authorization')).toBe(false);
  } finally {
    state.close();
  }
});

test('password change reconnects', async () => {
  let connected: (() => void) | undefined;
  const state = await setup(() => connected?.());
  try {
    await state.events.connect();
    const previousSocket = state.sockets[0]!;
    previousSocket.onopen?.();
    state.respond(async () =>
      Response.json({ detail: 'Current password is incorrect.' }, { status: 403 }),
    );
    await expect(state.auth.changePassword('wrong', 'new-password')).rejects.toMatchObject({
      status: 403,
    });
    expect(state.auth.status.value?.username).toBe('user');
    expect(previousSocket.closed).toBe(false);
    const connection = new Promise<void>((resolve) => (connected = resolve));
    state.respond(async (url) => {
      if (url === '/api/auth/ws-ticket') {
        return Response.json({ ticket: 'changed-ticket' });
      }
      return Response.json({ id: 1, username: 'user', auth_method: 'session' });
    });
    await state.auth.changePassword('old-password', 'new-password');
    await connection;
    expect(state.auth.status.value?.username).toBe('user');
    expect(previousSocket.closed).toBe(true);
    expect(state.sockets.at(-1)?.url).toContain('ticket=changed-ticket');
  } finally {
    state.close();
  }
});

test('outage preserves session', async () => {
  const state = await setup();
  try {
    state.respond(async () => Response.json({ detail: 'Unavailable' }, { status: 503 }));
    await expect(state.auth.checkStatus()).rejects.toMatchObject({ status: 503 });
    expect(state.auth.status.value?.username).toBe('user');
    state.respond(async () => Response.json({ detail: 'Invalid credentials' }, { status: 401 }));
    await expect(state.auth.login('user', 'incorrect')).rejects.toMatchObject({ status: 401 });
    expect(state.auth.status.value?.username).toBe('user');
  } finally {
    state.close();
  }
});

test('mutations send header', async () => {
  const state = await setup();
  const transport = state.modules['~/utils'] as {
    request: (url: string, options: RequestInit) => Promise<Response>;
  };
  try {
    for (const method of ['POST', 'put', 'PATCH', 'DELETE']) {
      await transport.request('/api/jobs', {
        method,
        headers: new Headers({ Accept: 'text/plain' }),
      });
      const call = state.calls.at(-1)!;
      const headers = new Headers(call.options.headers);
      expect(headers.get('X-Millwright-Request')).toBe('1');
      expect(headers.get('Accept')).toBe('text/plain');
    }
  } finally {
    state.close();
  }
});

test('old rejection preserves login', async () => {
  const state = await setup();
  let release!: (response: Response) => void;
  const rejected = new Promise<Response>((resolve) => (release = resolve));
  state.respond((url) =>
    url === '/api/jobs'
      ? rejected
      : Promise.resolve(Response.json({ id: 2, username: 'new-user', auth_method: 'session' })),
  );
  const transport = state.modules['~/utils'] as { request: (url: string) => Promise<Response> };
  const pending = transport.request('/api/jobs');
  try {
    expect(state.calls.at(-1)!.options.credentials).toBe('same-origin');
    expect(new Headers(state.calls.at(-1)!.options.headers).has('Authorization')).toBe(false);
    await state.auth.login('new-user', 'password');
    release(new Response(null, { status: 401 }));
    await pending;
    expect(state.auth.status.value?.username).toBe('new-user');
  } finally {
    release(new Response(null, { status: 401 }));
    await pending;
    state.close();
  }
});

test('old logout preserves login', async () => {
  for (const status of [204, 401]) {
    const state = await setup();
    let release!: (response: Response) => void;
    const response = new Promise<Response>((resolve) => (release = resolve));
    state.respond((url) =>
      url === '/api/auth/logout'
        ? response
        : Promise.resolve(Response.json({ id: 2, username: 'new-user', auth_method: 'session' })),
    );
    const pending = state.auth.logout();
    try {
      await state.auth.login('new-user', 'password');
      release(new Response(null, { status }));
      await pending;
      expect(state.auth.status.value?.username).toBe('new-user');
    } finally {
      release(new Response(null, { status }));
      await pending;
      state.close();
    }
  }
});

for (const status of [200, 401, 503]) {
  test(`stale probe result ${status}`, async () => {
    const state = await setup();
    let release!: (response: Response) => void;
    const oldResponse = new Promise<Response>((resolve) => (release = resolve));
    state.respond(() => oldResponse);
    const pending = state.auth.probe(true);
    const response =
      status === 401
        ? new Response(null, { status })
        : Response.json({ id: 1, username: 'old-user', auth_method: 'session' }, { status });
    try {
      state.respond(async () =>
        Response.json({ id: 2, username: 'new-user', auth_method: 'session' }),
      );
      await state.auth.refreshSession();
      release(response);
      const current = await pending;
      expect(state.auth.status.value?.username).toBe('new-user');
      expect(current?.username).toBe('new-user');
      expect(state.auth.lastError.value).toBeNull();
    } finally {
      release(response);
      await pending;
      state.close();
    }
  });
}

test('superseded probe awaits current', async () => {
  const state = await setup();
  let releaseOld!: (response: Response) => void;
  let releaseNew!: (response: Response) => void;
  const oldResponse = new Promise<Response>((resolve) => (releaseOld = resolve));
  const newResponse = new Promise<Response>((resolve) => (releaseNew = resolve));
  state.respond(() => oldResponse);
  const pending = state.auth.probe(true);
  let current: Promise<unknown> | undefined;
  try {
    state.respond(() => newResponse);
    current = state.auth.refreshSession();
    releaseOld(new Response(null, { status: 401 }));
    releaseNew(Response.json({ id: 2, username: 'new-user', auth_method: 'session' }));
    await current;
    expect((await pending)?.username).toBe('new-user');
  } finally {
    releaseOld(new Response(null, { status: 401 }));
    releaseNew(Response.json({ id: 2, username: 'new-user', auth_method: 'session' }));
    await Promise.all([pending, current]);
    state.close();
  }
});

test('logout invalidates ticket', async () => {
  const state = await setup();
  let release!: (response: Response) => void;
  const ticket = new Promise<Response>((resolve) => (release = resolve));
  state.respond((url) => (url.endsWith('ws-ticket') ? ticket : Promise.resolve(new Response())));
  const pending = state.events.connect();
  try {
    await state.auth.logout();
    release(Response.json({ ticket: 'stale-ticket' }));
    await pending;
    expect(state.sockets).toHaveLength(0);
    expect(state.timers.size).toBe(0);
    await state.events.connect();
    expect(state.sockets).toHaveLength(0);
  } finally {
    release(Response.json({ ticket: 'cleanup' }));
    await pending;
    state.close();
  }
});

test('outage retains socket', async () => {
  const state = await setup();
  try {
    await state.events.connect();
    const events = state.events.useEvents();
    state.sockets[0]!.onopen?.();
    state.respond(async () => {
      throw new Error('offline');
    });
    await expect(state.auth.refreshSession()).rejects.toThrow();
    expect(events.connected.value).toBe(true);
    state.respond(async () => Response.json({ detail: 'Unavailable' }, { status: 503 }));
    await expect(state.auth.probe(true)).rejects.toMatchObject({ status: 503 });
    expect(state.auth.status.value?.username).toBe('user');
    expect(events.connected.value).toBe(true);
    expect(state.sockets[0]!.closed).toBe(false);
    state.respond(async () => new Response(null, { status: 401 }));
    expect(await state.auth.probe(true)).toBeNull();
    expect(state.sockets[0]!.closed).toBe(true);
  } finally {
    state.close();
  }
});

test('reconnect restores connection', async () => {
  let nextSocket: ((socket: Socket) => void) | undefined;
  const state = await setup((socket) => nextSocket?.(socket));
  try {
    await state.events.connect();
    const events = state.events.useEvents();
    let reconnects = 0;
    const unsubscribe = events.onReconnect(() => reconnects++);
    try {
      expect(events.connected.value).toBe(false);
      state.sockets[0]!.onopen?.();
      expect(events.connected.value).toBe(true);
      expect(reconnects).toBe(0);
      for (let index = 0; index < 2; index++) {
        state.sockets[index]!.onclose?.();
        expect(events.connected.value).toBe(false);
        expect(state.timers.size).toBe(1);
        const created = new Promise<Socket>((resolve) => (nextSocket = resolve));
        const [id, retry] = [...state.timers][0]!;
        state.timers.delete(id);
        retry();
        const socket = await created;
        expect(state.sockets[index + 1]).toBe(socket);
        socket.onopen?.();
        expect(events.connected.value).toBe(true);
        expect(reconnects).toBe(index + 1);
        expect(state.timers.size).toBe(0);
      }
    } finally {
      unsubscribe();
    }
  } finally {
    state.close();
  }
});

test('rejection cancels reconnect', async () => {
  const state = await setup();
  try {
    await state.events.connect();
    state.sockets[0]!.onclose?.();
    expect(state.timers.size).toBe(1);
    state.respond(async () => new Response(null, { status: 401 }));
    const transport = state.modules['~/utils'] as { request: (url: string) => Promise<Response> };
    await transport.request('/api/jobs');
    expect(state.auth.status.value).toBeNull();
    expect(state.timers.size).toBe(0);
    await state.events.connect();
    expect(state.sockets).toHaveLength(1);
  } finally {
    state.close();
  }
});

test('socket schemes remain secure', async () => {
  const state = await setup();
  try {
    for (const [scheme, expected] of [
      ['http', 'ws'],
      ['https', 'wss'],
      ['ws', 'ws'],
      ['wss', 'wss'],
    ]) {
      state.config.public.wss = `${scheme}://example.test`;
      expect(state.events.wsUrl()).toBe(`${expected}://example.test/api/events`);
    }
    state.config.public.wss = '';
    for (const [protocol, expected] of [
      ['http:', 'ws'],
      ['https:', 'wss'],
    ] as const) {
      state.globals.window.location.protocol = protocol;
      expect(state.events.wsUrl()).toBe(`${expected}://ui.example.test:8082/api/events`);
    }
  } finally {
    state.close();
  }
});

test('downloads authenticated complete logs', async () => {
  const state = await setup();
  try {
    const jobs = load<{ useJobs: typeof useJobs }>(
      'composables/useJobs.ts',
      {
        ...state.modules,
        './useEvents': state.events,
        './stateHelpers': {},
        '~/utils/jobList': jobList,
      },
      state.globals,
    );
    const full = 'first line\n' + 'output\n'.repeat(50_000) + 'last line\n';
    state.respond(async () => new Response(full));
    const blob = await jobs.useJobs().downloadLog(12, 4);
    expect(await blob.text()).toBe(full);
    const call = state.calls.find((call) => call.url.includes('/attempts/'))!;
    expect(call.url).toBe('/api/jobs/12/attempts/4/log');
    expect(call.options.credentials).toBe('same-origin');
    expect(new Headers(call.options.headers).get('Accept')).toBe('text/plain');
    expect(new Headers(call.options.headers).has('Range')).toBe(false);
    state.respond(async () => Response.json({ detail: 'Log unavailable' }, { status: 404 }));
    await expect(jobs.useJobs().downloadLog(12, 4)).rejects.toEqual({
      status: 404,
      detail: 'Log unavailable',
    });
  } finally {
    state.close();
  }
});
