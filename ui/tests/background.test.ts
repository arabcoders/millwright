import { expect, test } from 'bun:test';
import * as Vue from 'vue';
import ts from 'typescript';
import type { useBackground } from '../app/composables/useBackground';

const setup = async () => {
  const urls: string[] = [];
  const revoked: string[] = [];
  const calls: string[] = [];
  let response = new Response(null, { status: 204 });
  let decode = async () => {};
  const source = await Bun.file(new URL('../app/composables/useBackground.ts', import.meta.url)).text();
  const code = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const exports: { useBackground?: typeof useBackground } = {};
  const modules: Record<string, unknown> = {
    vue: Vue,
    '@vueuse/core': { useStorage: (_key: string, value: unknown) => Vue.ref(value) },
    '~/utils': {
      request: async (path: string) => {
        calls.push(path);
        return response.clone();
      },
      ensure_api_success: async (result: Response) => {
        if (!result.ok) throw new Error(`HTTP ${result.status}`);
      },
      to_api_error: (error: Error) => error,
    },
  };
  new Function('require', 'exports', 'URL', 'Image', code)(
    (name: string) => modules[name],
    exports,
    {
      createObjectURL: () => {
        const url = `blob:${urls.length + 1}`;
        urls.push(url);
        return url;
      },
      revokeObjectURL: (url: string) => revoked.push(url),
    },
    class {
      src = '';
      decode() {
        return decode();
      }
    },
  );
  return {
    background: exports.useBackground!(),
    urls,
    revoked,
    calls,
    respond: (value: Response) => (response = value),
    decode: (value: () => Promise<void>) => (decode = value),
  };
};

test('empty backgrounds are normal', async () => {
  const { background, urls } = await setup();
  await background.load();
  expect(background.empty.value).toBe(true);
  expect(background.image.value).toBe('');
  expect(background.error.value).toBe('');
  expect(background.loading.value).toBe(false);
  expect(urls).toEqual([]);
});

test('refresh retains usable image', async () => {
  const state = await setup();
  const { background } = state;
  try {
    state.respond(new Response('image', { headers: { 'Content-Type': 'image/jpeg' } }));
    await background.load();
    const first = background.image.value;
    state.respond(new Response(null, { status: 204 }));
    await background.load(true);
    expect(background.image.value).toBe(first);
    expect(background.empty.value).toBe(true);
    expect(background.error.value).toBe('');
    state.respond(new Response('unavailable', { status: 503 }));
    await background.load(true);
    expect(background.image.value).toBe(first);
    expect(background.error.value).toBe('HTTP 503');
    state.respond(new Response('image', { headers: { 'Content-Type': 'image/jpeg' } }));
    state.decode(async () => { throw new Error('Invalid image.'); });
    await background.load(true);
    expect(background.image.value).toBe(first);
    expect(background.error.value).toBe('Invalid image.');
    expect(state.revoked).toEqual(['blob:2']);
    state.decode(async () => {});
    await background.load(true);
    expect(background.image.value).toBe('blob:3');
    expect(background.error.value).toBe('');
    expect(state.revoked).toEqual(['blob:2', first]);
    expect(state.calls).toEqual(['/api/background', ...Array(4).fill('/api/background?force=true')]);
  } finally {
    background.clear();
  }
  expect(state.revoked.at(-1)).toBe('blob:3');
});

test('clear discards pending image', async () => {
  const state = await setup();
  state.respond(new Response('image', { headers: { 'Content-Type': 'image/jpeg' } }));
  let started!: () => void;
  let finish!: () => void;
  const decoding = new Promise<void>((resolve) => { started = resolve; });
  const release = new Promise<void>((resolve) => { finish = resolve; });
  state.decode(() => { started(); return release; });
  const pending = state.background.load();
  try {
    await decoding;
    state.background.clear();
  } finally {
    finish();
    await pending;
  }
  expect(state.background.image.value).toBe('');
  expect(state.background.loading.value).toBe(false);
  expect(state.background.error.value).toBe('');
  expect(state.revoked).toEqual(['blob:1']);
});
