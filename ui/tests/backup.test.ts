import { expect, test } from 'bun:test';

let respond: (path: string, options: RequestInit) => Promise<Response>;
const transportSource = await Bun.file(
  new URL('../app/composables/useBackup.ts', import.meta.url),
).text();
const transportCode = new Bun.Transpiler({ loader: 'ts' })
  .transformSync(transportSource)
  .replace(/^import[\s\S]*?from\s+["'][^"']+["'];?\s*$/gm, '')
  .replace('export const useBackup', 'const useBackup');
const useBackup: typeof import('../app/composables/useBackup').useBackup = new Function(
  'request',
  'parse_api_error',
  `${transportCode}\nreturn useBackup;`,
)(
  (path: string, options: RequestInit) => respond(path, options),
  async (payload: { detail: string }) => payload.detail,
);

test('reports rejected requests and malformed responses', async () => {
  const backup = useBackup();
  const file = new File(['encrypted'], 'backup.mwbackup');
  respond = async () => {
    throw new Error('network unavailable');
  };
  expect(await backup.exportBackup('password')).toEqual({
    ok: false,
    status: null,
    reason: 'network',
  });
  expect(await backup.preview(file, 'password')).toEqual({
    ok: false,
    status: null,
    reason: 'network',
  });
  expect(await backup.reset([])).toEqual({ ok: false, status: null, reason: 'network' });
  respond = async () => new Response('{invalid');
  expect(await backup.preview(file, 'password')).toEqual({
    ok: false,
    status: null,
    reason: 'decode',
  });
  expect(await backup.reset([])).toEqual({ ok: false, status: null, reason: 'decode' });
});

test('preserves root overrides', async () => {
  const selection = {
    flows: [],
    libraries: ['films'],
    notifications: [],
    webhooks: [],
    plugins: [],
    nodes: [],
    config: false,
    settings: true,
    replace: false,
    roots: { films: [] },
  };
  respond = async (path, options) => {
    expect(path).toBe('/api/backups/restore');
    expect(options.body).toBeInstanceOf(FormData);
    expect(JSON.parse(String((options.body as FormData).get('selection')))).toEqual(selection);
    return Response.json({ detail: 'Library roots must not be empty.' }, { status: 422 });
  };
  expect(
    await useBackup().restore(new File(['backup'], 'backup.mwbackup'), 'password', selection),
  ).toMatchObject({ ok: false, status: 422, reason: 'http' });
});
