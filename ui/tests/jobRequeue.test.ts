import { expect, test } from 'bun:test';
import * as Vue from 'vue';
import ts from 'typescript';
import type { useJobs, JobRetryResult, BulkRequestResult } from '../app/composables/useJobs';
import type { useJobRequeue, notifyRequeueOutcome } from '../app/composables/useJobRequeue';
import * as jobList from '../app/utils/jobList';

const load = async (name: string, modules: Record<string, unknown>) => {
  const source = (await Bun.file(`app/composables/${name}.ts`).text()).replaceAll(
    'import.meta.client',
    'false',
  );
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const exports: {
    useJobs?: typeof useJobs;
    useJobRequeue?: typeof useJobRequeue;
    notifyRequeueOutcome?: typeof notifyRequeueOutcome;
  } = {};
  new Function('require', 'exports', compiled)((name: string) => modules[name] ?? {}, exports);
  return exports;
};

const handler = async (
  actions: JobRetryResult[] = [],
  bulks: (BulkRequestResult | { error: { detail: string } })[] = [],
) => {
  const calls: { kind: string; id: number | number[]; action: string }[] = [];
  const messages: { kind: string; text: string }[] = [];
  const exported = await load('useJobRequeue', {
    './useJobs': {
      useJobs: () => ({
        runAction: async (id: number, action: string) => {
          calls.push({ kind: 'single', id, action });
          return actions.shift()!;
        },
        runBulk: async (action: string, id: number[]) => {
          calls.push({ kind: 'bulk', id, action });
          return bulks.shift()!;
        },
      }),
    },
    './useNotification': {
      useNotification: () =>
        Object.fromEntries(
          ['success', 'info', 'error'].map((kind) => [
            kind,
            (text: string) => messages.push({ kind, text }),
          ]),
        ),
    },
    './useApiErrorMessage': {
      useApiErrorMessage: () => ({
        messageFor: (message: string | undefined, fallback: string) => message || fallback,
      }),
    },
  });
  return {
    ...exported.useJobRequeue!(),
    notifyOutcome: exported.notifyRequeueOutcome!,
    calls,
    messages,
  };
};

test('requeue acknowledges request', async () => {
  const state = await handler([{ ok: true, accepted: { id: 9, status: 'pending' } }]);
  expect(await state.requeue(9)).toBe(true);
  expect(state.calls).toEqual([{ kind: 'single', id: 9, action: 'retry' }]);
  expect(state.messages.map(({ kind }) => kind)).toEqual(['success']);
});

test.each([
  ['not_queued', 'info'],
  ['failed', 'error'],
  ['cancelled', 'info'],
] as const)('requeue outcome %s', async (outcome, kind) => {
  const state = await handler();
  state.notifyOutcome({ job_id: 9, outcome, reason: 'No changes needed.' });
  expect(state.messages.map(({ kind }) => kind)).toEqual([kind]);
  expect(state.messages[0]?.text).toContain('No changes needed.');
});

test('queued acknowledges once', async () => {
  const state = await handler([{ ok: true, accepted: { id: 9, status: 'pending' } }]);
  await state.requeue(9);
  state.notifyOutcome({ job_id: 9, outcome: 'queued', reason: 'Ready.' });
  expect(state.messages.map(({ kind }) => kind)).toEqual(['success']);
});

test('outcome keeps reason', async () => {
  const state = await handler();
  state.notifyOutcome({
    job_id: 9,
    outcome: 'not_queued',
    reason: '',
    decision: { status: 'vetoed', reason: 'Too large.', steps: [] },
  });
  expect(state.messages.map(({ kind }) => kind)).toEqual(['info']);
  expect(state.messages[0]?.text).toContain('Too large.');
});

test('requeue reports rejection', async () => {
  const state = await handler([
    { ok: false, error: { status: 409, detail: 'File already has an active job.' } },
  ]);
  expect(await state.requeue(9)).toBe(false);
  expect(state.messages).toEqual([{ kind: 'error', text: 'File already has an active job.' }]);
});

test('bulk partial summary', async () => {
  const state = await handler(
    [],
    [{ accepted: [1, 2, 3], failed: [{ id: 4, detail: 'active job' }] }],
  );
  expect(await state.requeueBulk([1, 2, 3, 4])).toEqual([1, 2, 3]);
  expect(state.calls).toEqual([{ kind: 'bulk', id: [1, 2, 3, 4], action: 'retry' }]);
  expect(state.messages).toEqual([
    {
      kind: 'error',
      text: 'Requeue requested for 3 jobs. 1 rejected. First rejection: #4: active job',
    },
  ]);
});

test('bulk accepted summary', async () => {
  const state = await handler([], [{ accepted: [2], failed: [] }]);
  expect(await state.requeueBulk([2])).toEqual([2]);
  expect(state.messages).toEqual([
    { kind: 'success', text: 'Requeue requested for 1 job. 0 rejected.' },
  ]);
});

test('bulk rejection summary', async () => {
  const state = await handler([], [{ accepted: [], failed: [{ id: 2, detail: 'Missing file.' }] }]);
  expect(await state.requeueBulk([2])).toEqual([]);
  expect(state.messages).toEqual([
    {
      kind: 'error',
      text: 'Requeue requested for 0 jobs. 1 rejected. First rejection: #2: Missing file.',
    },
  ]);
});

test('bulk request failure', async () => {
  const state = await handler([], [{ error: { detail: 'Server unavailable.' } }]);
  expect(await state.requeueBulk([2])).toBeNull();
  expect(state.messages).toEqual([{ kind: 'error', text: 'Server unavailable.' }]);
});

test('retry responses', async () => {
  const calls: { url: string; options: RequestInit }[] = [];
  const accepted = { id: 9, status: 'pending' };
  const bulk = { accepted: [9], failed: [] };
  const responses = [
    Response.json(accepted, { status: 202 }),
    Response.json(bulk, { status: 202 }),
    Response.json({ detail: 'File already has an active job.' }, { status: 409 }),
  ];
  const exported = await load('useJobs', {
    vue: Vue,
    '~/utils/jobList': jobList,
    '~/utils': {
      request: async (url: string, options: RequestInit) => {
        calls.push({ url, options });
        return responses.shift()!;
      },
      parse_api_error: (body: { detail: string }) => body.detail,
    },
  });
  const jobs = exported.useJobs!();
  expect(await jobs.runAction(9, 'retry')).toEqual({ ok: true, accepted });
  expect(await jobs.runBulk('retry', [9])).toEqual(bulk);
  expect(await jobs.runAction(9, 'retry')).toEqual({
    ok: false,
    error: { status: 409, detail: 'File already has an active job.' },
  });
  expect(calls).toEqual([
    { url: '/api/jobs/9/retry', options: { method: 'POST' } },
    {
      url: '/api/jobs/bulk',
      options: { method: 'POST', body: JSON.stringify({ action: 'retry', job_ids: [9] }) },
    },
    { url: '/api/jobs/9/retry', options: { method: 'POST' } },
  ]);
});
