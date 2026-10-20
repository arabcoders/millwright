import { expect, test } from 'bun:test';
import { computed, ref, shallowReadonly } from 'vue';
import type { WorkspaceList, WorkspaceSnapshot } from '../app/types/api';

const source = await Bun.file(
  new URL('../app/composables/useWorkspaces.ts', import.meta.url),
).text();
const code = new Bun.Transpiler({ loader: 'ts' })
  .transformSync(source)
  .replace(/^import[\s\S]*?from\s+["'][^"']+["'];?\s*$/gm, '')
  .replace('export const useWorkspaces', 'const useWorkspaces');

const sample = (second: number, id = 1): WorkspaceList => ({
  total: 1,
  items: [
    {
      workspace_id: id,
      job_id: id,
      file_id: id,
      filename: 'movie.mkv',
      status: 'failed',
      node_id: null,
      node_name: null,
      attempt_number: 1,
      progress: null,
      size_bytes: 6,
      file_count: 1,
      source_bytes: 6,
      output_bytes: null,
      last_activity_at: null,
      eligible: true,
      reason: 'Unused',
      error: null,
      sampled_at: `2026-01-01T00:00:${String(second).padStart(2, '0')}+00:00`,
      files: [
        { path: `jobs/${id}/source.mkv`, size_bytes: 6, modified_at: '2026-01-01T00:00:00+00:00' },
      ],
      attempts: [],
    },
  ],
  summary: {
    count: 1,
    active_count: 0,
    unused_count: 1,
    unused_size_bytes: 6,
    protected_count: 0,
    size_bytes: 6,
    free_bytes: 100,
  },
  sampled_at: `2026-01-01T00:00:${String(second).padStart(2, '0')}+00:00`,
  error: null,
});

const summary = (list: WorkspaceList): WorkspaceSnapshot => ({
  summary: list.summary,
  sampled_at: list.sampled_at,
  error: list.error,
});

const setup = () => {
  const calls: Array<{ url: string; options?: RequestInit }> = [];
  let respond = async (_url: string, _options?: RequestInit): Promise<Response> =>
    Response.json(sample(1));
  const useWorkspaces: typeof import('../app/composables/useWorkspaces').useWorkspaces =
    new Function(
      'ref',
      'shallowReadonly',
      'request',
      'parse_api_error',
      `${code}\nreturn useWorkspaces;`,
    )(
      ref,
      shallowReadonly,
      (url: string, options?: RequestInit) => {
        calls.push({ url, options });
        return respond(url, options);
      },
      async (body: { detail?: string } | null) => body?.detail ?? 'Request failed',
    );
  return {
    store: useWorkspaces(),
    calls,
    respond: (next: typeof respond) => {
      respond = next;
    },
  };
};

test('samples preserve order', async () => {
  const env = setup();
  let resolve!: (value: Response) => void;
  env.respond(
    () =>
      new Promise<Response>((done) => {
        resolve = done;
      }),
  );
  const pending = env.store.load();
  env.respond(async () =>
    Response.json({
      items: [{ workspace_id: 2, outcome: 'removed', reason: null }],
      removed: 1,
      skipped: 0,
      errors: 0,
      snapshot: summary(sample(3, 2)),
    }),
  );
  await env.store.prune({ job_ids: [2] });
  resolve(Response.json(sample(1)));
  await pending;
  expect(env.store.snapshot.value?.items[0]?.workspace_id).toBe(1);
  expect(env.store.summary.value?.sampled_at).toBe(sample(3).sampled_at);
  env.respond(async () => Response.json(sample(4)));
  await env.store.load();
  expect(env.store.snapshot.value?.items[0]?.workspace_id).toBe(1);
  expect(env.store.summary.value?.sampled_at).toBe(sample(4).sampled_at);
});

test('prune returns result', async () => {
  const env = setup();
  await env.store.load();
  const next = {
    ...sample(2),
    items: [],
    total: 0,
    summary: {
      count: 0,
      active_count: 0,
      unused_count: 0,
      unused_size_bytes: 0,
      protected_count: 0,
      size_bytes: 0,
      free_bytes: 106,
    },
  };
  env.respond(async (url) =>
    url.includes('/prune')
      ? Response.json({
          items: [{ workspace_id: 1, outcome: 'removed', reason: null }],
          removed: 1,
          skipped: 0,
          errors: 0,
          snapshot: summary(next),
        })
      : Response.json(next),
  );
  const result = await env.store.prune({ job_ids: [1] });
  expect(result.removed).toBe(1);
  expect(result.items[0]?.outcome).toBe('removed');
  expect(env.calls[1]?.options?.method).toBe('POST');
  expect(JSON.parse(String(env.calls[1]?.options?.body))).toEqual({ job_ids: [1] });
  expect(env.store.summary.value?.summary.count).toBe(0);
  expect(env.calls.length).toBe(2);
  await env.store.prune({ all_unused: true });
  expect(JSON.parse(String(env.calls[2]?.options?.body))).toEqual({ all_unused: true });
  await env.store.load();
  expect(env.store.snapshot.value?.items).toEqual([]);
});

test('unavailable measurements stay null', async () => {
  const env = setup();
  const unavailable: WorkspaceList = {
    ...sample(2),
    items: [],
    error: 'Workspace unavailable',
    summary: {
      count: null,
      active_count: null,
      unused_count: null,
      unused_size_bytes: null,
      protected_count: null,
      size_bytes: null,
      free_bytes: null,
    },
  };
  env.respond(async () => Response.json(unavailable));
  await env.store.load();
  expect(env.store.lastError.value).not.toBeNull();
  expect(env.store.snapshot.value?.summary.free_bytes).toBeNull();
  env.respond(
    async () =>
      new Response(JSON.stringify({ detail: 'Cannot remove workspace' }), { status: 409 }),
  );
  await expect(env.store.prune({ job_ids: [1] })).rejects.toThrow();
  expect(env.store.snapshot.value?.summary.size_bytes).toBeNull();
  await env.store.load();
});

test('sampled files refresh', async () => {
  const env = setup();
  await env.store.load();
  const files = computed(
    () => env.store.snapshot.value?.items.find((item) => item.workspace_id === 1)?.files,
  );
  expect(files.value?.[0]?.size_bytes).toBe(6);
  const newer = sample(2);
  newer.items[0]!.files[0]!.size_bytes = 10;
  newer.items[0]!.attempts = [
    { id: 1, attempt_number: 1, status: 'running', result: null, node_name: 'Node' },
  ];
  env.respond(async () => Response.json(newer));
  await env.store.load();
  expect(files.value?.[0]?.size_bytes).toBe(10);
  expect(env.store.snapshot.value?.items[0]?.sampled_at).toBe(newer.sampled_at);
  expect(env.store.snapshot.value?.items[0]?.attempts[0]?.status).toBe('running');
  expect(env.calls.length).toBe(2);
});

test('listing sends server filters', async () => {
  const env = setup();
  await env.store.load({
    query: 'movie',
    eligibility: 'unused',
    sort_by: 'size_bytes',
    sort_dir: 'asc',
    limit: 999,
    offset: 999,
  });
  const params = new URL(env.calls[0]!.url, 'http://localhost').searchParams;
  expect(Object.fromEntries(params)).toEqual({
    query: 'movie',
    eligibility: 'unused',
    sort_by: 'size_bytes',
    sort_dir: 'asc',
    limit: '999',
    offset: '999',
  });
  await env.store.load();
  expect(env.calls[1]?.url).toBe(env.calls[0]?.url);
});

test('latest request wins', async () => {
  const env = setup();
  const responses: Array<(response: Response) => void> = [];
  env.respond(() => new Promise<Response>((resolve) => responses.push(resolve)));
  const older = env.store.load({ limit: 50, offset: 0 });
  const newer = env.store.load({ limit: 50, offset: 50 });
  responses[1]!(Response.json(sample(2, 2)));
  await newer;
  expect(env.store.isLoading.value).toBe(false);
  responses[0]!(Response.json(sample(1, 1)));
  await older;
  expect(env.store.snapshot.value?.sampled_at).toBe(sample(2).sampled_at);
  expect(env.store.summary.value?.sampled_at).toBe(sample(2).sampled_at);
  expect(env.store.lastError.value).toBeNull();
});
