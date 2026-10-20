import { afterEach, expect, test } from 'bun:test';
import * as Vue from 'vue';
import ts from 'typescript';
import type {
  canSkipCooldown,
  refreshMetadataPrerequisite,
  useFiles,
  notifyJobRequestResult,
} from '../app/composables/useFiles';
import * as stateHelpers from '../app/composables/stateHelpers';
import * as status from '../app/utils/status';
import type { ConfirmOptions } from '../app/composables/useDialog';
import type {
  BulkFileResult,
  DryRunResult,
  FlowRow,
  LibraryRow,
  MediaFileRow,
} from '../app/types/api';
import type { useTranscodeFlow } from '../app/composables/useTranscodeFlow';
import type { useFileDryRun } from '../app/composables/useFileDryRun';

const scopes: Vue.EffectScope[] = [];
afterEach(() => {
  for (const scope of scopes.splice(0)) scope.stop();
});

type PickerProps = {
  flows: Pick<FlowRow, 'id' | 'name' | 'version'>[];
  loading: boolean;
  error: string;
  selectedFlowId?: number;
  previewing: boolean;
  'onUpdate:open': (open: boolean) => void;
  'onUpdate:selectedFlowId': (id: number | undefined) => void;
  onRun: () => void;
  onDryRun: () => Promise<void>;
};
type PreviewProps = {
  filePath: string;
  facts: string;
  flows: FlowRow[];
  result: DryRunResult | null;
  running: boolean;
  error: string;
  onOpenFile?: () => void;
  'onUpdate:open': (open: boolean) => void;
  onRun: () => Promise<void>;
};
const previewResult = (reason = 'Current evaluation.'): DryRunResult => ({
  status: 'matched',
  reason,
  steps: [],
  facets: {},
  plan: { video: { encoder: 'libx265' } },
  argv: ['ffmpeg', '-i', 'input.mkv'],
  stream_log: [],
});
const savedFlow = (id: number, version: number): FlowRow => ({
  id,
  name: `Flow ${id}`,
  version,
  definition: { entry: `stage-${id}-${version}`, nodes: [], edges: [] },
  created_at: '',
  updated_at: '',
});

const file = (id: number, library_id = 1): MediaFileRow => ({
  id,
  library_id,
  path: `/media/${id}.mkv`,
  active_job_id: null,
  size: 1024,
  container: 'mkv',
  video_codec: 'h264',
  width: 1920,
  height: 1080,
  duration_s: 60,
  bit_rate: 8000,
  medium: 'movie',
  modified_at: '2026-01-01T00:00:00Z',
  cooldown_until: null,
  created_at: '',
  updated_at: '',
  transcode_status: 'pending',
  health_status: 'healthy',
  active_job_kind: null,
  active_job_status: null,
  active_job_progress: null,
  active_job_updated_at: null,
});
const library = (id: number, healthcheck_enabled: boolean) =>
  ({
    id,
    name: `Library ${id}`,
    healthcheck_enabled,
    flow_id: 2,
  }) as LibraryRow;

const handler = async (
  options: {
    libraries?: LibraryRow[];
    confirmed?: boolean;
    result?: BulkFileResult<{ file_id: number }>;
    settingsError?: boolean;
    dialog?: () => Promise<{ status: boolean; value: null }>;
    submit?: (url: string, init?: RequestInit) => Promise<Response>;
    flows?: FlowRow[];
    flowError?: string;
    reloadFlows?: () => Promise<void>;
    autoPick?: boolean;
    evaluate?: () => Promise<DryRunResult>;
    ensurePlugins?: () => Promise<void>;
  } = {},
) => {
  const calls: { url: string; body: unknown }[] = [];
  const dialogs: ConfirmOptions[] = [];
  const messages: { kind: string; text: string }[] = [];
  const previewCalls: { fileId: number; definition: FlowRow['definition'] }[] = [];
  const previews: { props: PreviewProps; closed: boolean }[] = [];
  const previewOverlay = {
    create: (_component: unknown, { props }: { props: PreviewProps }) => {
      let resolve!: () => void;
      const result = new Promise<void>((done) => (resolve = done));
      const modal = {
        props,
        closed: false,
        close: () => {
          modal.closed = true;
          resolve();
        },
        open: () => ({ result }),
        patch: (props: PreviewProps) => (modal.props = props),
      };
      previews.push(modal);
      return modal;
    },
  };
  const settings = Vue.ref(options.libraries ?? [library(1, true)]);
  const flows = Vue.ref(options.flows ?? [savedFlow(2, 4), savedFlow(3, 7)]);
  const flowError = Vue.ref<{ detail: string } | null>(null);
  let flowReloads = 0;
  const modals: { props: PickerProps; ready: Promise<void> }[] = [];
  const overlay = {
    create: (_component: unknown, { props }: { props: PickerProps }) => {
      let resolve!: (flow: PickerProps['flows'][number] | null) => void;
      const result = new Promise<PickerProps['flows'][number] | null>((done) => (resolve = done));
      let ready!: () => void;
      const loaded = new Promise<void>((done) => (ready = done));
      let picked = false;
      const modal = {
        props,
        ready: loaded,
        close: (flow: PickerProps['flows'][number] | null) => resolve(flow),
        open: () => ({ result }),
        patch: (props: PickerProps) => {
          modal.props = props;
          if (!props.loading) ready();
          if (options.autoPick !== false && !props.loading && !picked) {
            picked = true;
            if (options.confirmed === false) props['onUpdate:open'](false);
            else props.onRun();
          }
        },
      };
      modals.push(modal);
      return modal;
    },
  };
  let reloads = 0;
  const source = (await Bun.file('app/composables/useFiles.ts').text()).replace(
    'import.meta.client',
    'false',
  );
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
    },
  }).outputText;
  const modules: Record<string, unknown> = {
    vue: Vue,
    '~/utils': {
      request: async (url: string, init?: RequestInit) => {
        calls.push({
          url,
          body: init?.body ? JSON.parse(String(init.body)) : null,
        });
        if (options.submit) return options.submit(url, init);
        const kind =
          url.endsWith('/healthcheck') ||
          (url === '/api/files/bulk' && JSON.parse(String(init?.body)).action === 'healthcheck')
            ? 'healthcheck'
            : 'transcode';
        const accepted = {
          job_id: 10,
          kind,
          status: kind === 'healthcheck' ? 'queued' : 'pending',
        };
        return Response.json(
          url === '/api/files/bulk'
            ? (options.result ?? {
                accepted: [{ file_id: 1, ...accepted }],
                failed: [],
              })
            : accepted,
          { status: 202 },
        );
      },
      parse_api_error: async (body: { detail?: string } | null) => body?.detail,
      formatBytes: (value: number) => `${value} bytes`,
      formatTime: (value: number) => `${value} seconds`,
      formatBitrate: (value: number) => `${value} bps`,
    },
    './useLibraries': {
      useLibraries: () => ({
        items: settings,
        lastError: Vue.ref(options.settingsError ? new Error('unavailable') : null),
        reload: async () => {
          reloads++;
        },
      }),
    },
    '~/composables/useLibraries': { useLibraries: () => ({ items: settings }) },
    '~/components/FileTranscodeModal.vue': {},
    '~/components/FlowDryRunModal.vue': {},
    '~/composables/usePlugins': {
      usePlugins: () => ({
        items: Vue.ref([]),
        ensure: options.ensurePlugins ?? (async () => {}),
      }),
    },
    '~/utils/location': { libraryLocation: () => 'Movies' },
    '~/composables/useFlows': {
      useFlows: () => ({
        items: flows,
        lastError: flowError,
        reload: async () => {
          flowReloads++;
          await options.reloadFlows?.();
          flowError.value = options.flowError ? { detail: options.flowError } : null;
        },
        dryRun: (fileId: number, definition: FlowRow['definition']) => {
          previewCalls.push({ fileId, definition });
          return options.evaluate?.() ?? Promise.resolve(previewResult());
        },
      }),
    },
    './useDialog': {
      useDialog: () => ({
        confirmDialog: async (dialog: ConfirmOptions) => {
          dialogs.push(dialog);
          expect(calls).toHaveLength(0);
          return options.dialog
            ? options.dialog()
            : { status: options.confirmed ?? true, value: null };
        },
      }),
    },
    './useNotification': {
      useNotification: () =>
        Object.fromEntries(
          ['success', 'info', 'warning', 'error'].map((kind) => [
            kind,
            (text: string) => messages.push({ kind, text }),
          ]),
        ),
    },
    '~/composables/useApiErrorMessage': {
      useApiErrorMessage: () => ({
        messageFor: (message?: string, fallback?: string) => message ?? fallback ?? 'test-fallback',
      }),
    },
    './stateHelpers': stateHelpers,
    '~/utils/status': status,
  };
  const previewExports: { useFileDryRun?: typeof useFileDryRun } = {};
  const previewCompiled = ts.transpileModule(
    await Bun.file('app/composables/useFileDryRun.ts').text(),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
  ).outputText;
  new Function('require', 'exports', 'useOverlay', previewCompiled)(
    (name: string) => modules[name] ?? {},
    previewExports,
    () => previewOverlay,
  );
  modules['~/composables/useFileDryRun'] = previewExports;
  const pickerExports: { useTranscodeFlow?: typeof useTranscodeFlow } = {};
  const pickerCompiled = ts.transpileModule(
    await Bun.file('app/composables/useTranscodeFlow.ts').text(),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
  ).outputText;
  new Function('require', 'exports', 'useOverlay', pickerCompiled)(
    (name: string) => modules[name] ?? {},
    pickerExports,
    () => overlay,
  );
  modules['./useTranscodeFlow'] = pickerExports;
  const exports: {
    canSkipCooldown?: typeof canSkipCooldown;
    useFiles?: typeof useFiles;
    refreshMetadataPrerequisite?: typeof refreshMetadataPrerequisite;
    notifyJobRequestResult?: typeof notifyJobRequestResult;
  } = {};
  new Function('require', 'exports', compiled)((name: string) => modules[name] ?? {}, exports);
  const scope = Vue.effectScope();
  scopes.push(scope);
  return {
    ...scope.run(() => exports.useFiles!())!,
    canSkipCooldown: exports.canSkipCooldown!,
    refreshMetadataPrerequisite: exports.refreshMetadataPrerequisite!,
    notifyOutcome: exports.notifyJobRequestResult!,
    calls,
    dialogs,
    messages,
    reloads: () => reloads,
    flowReloads: () => flowReloads,
    picker: () => modals.at(-1)!.props,
    pickerReady: () => modals.at(-1)!.ready,
    flows,
    scope,
    preview: () => previews.at(-1)!,
    previewCalls,
  };
};

test.each(['healthcheck', 'skip_cooldown'] as const)('bulk %s page', async (action) => {
  const files = Array.from({ length: 999 }, (_, index) => file(index + 1));
  const result: BulkFileResult = {
    accepted: files.map((file) => ({
      file_id: file.id,
      job_id: file.id,
      kind: 'healthcheck',
      status: 'queued',
    })),
    failed: [],
  };
  const state = await handler({ result });
  expect(await state.queueBulk(action, files)).toEqual(result);
  expect(state.calls).toEqual([
    {
      url: '/api/files/bulk',
      body: { action, file_ids: files.map((file) => file.id) },
    },
  ]);
  expect(state.dialogs).toHaveLength(1);
});

test('metadata refresh queues', async () => {
  const state = await handler({
    submit: async (_url, init) => {
      expect(init?.method).toBe('POST');
      return Response.json({ job_id: 10, kind: 'probe', status: 'queued' }, { status: 202 });
    },
  });
  expect(await state.refreshMetadata({ id: 23 })).toBe(true);
  expect(state.calls).toEqual([{ url: '/api/files/23/probe', body: null }]);
  expect(state.reloads()).toBe(0);
  expect(state.dialogs).toHaveLength(0);
  expect(state.messages.map(({ kind }) => kind)).toEqual(['success']);
  expect(state.actionBusy.value).toBe(false);
  expect(state.refreshingMetadata.value).toBeNull();
});

test('metadata refresh errors', async () => {
  for (const [submit, text] of [
    [
      async () => Response.json({ detail: 'Scanner is paused.' }, { status: 409 }),
      'Scanner is paused.',
    ],
    [async () => new Response('Unavailable', { status: 503 }), 'test-fallback'],
    [
      async () => {
        throw new Error('Network unavailable');
      },
      'Network unavailable',
    ],
  ] as const) {
    const state = await handler({ submit });
    expect(await state.refreshMetadata(file(1))).toBe(false);
    expect(state.messages).toEqual([{ kind: 'error', text }]);
    expect(state.actionBusy.value).toBe(false);
    expect(state.refreshingMetadata.value).toBeNull();
  }
});

test('metadata refresh guards', async () => {
  let release!: (response: Response) => void;
  const state = await handler({
    submit: () => new Promise((resolve) => (release = resolve)),
  });
  const pending = state.refreshMetadata(file(1));
  try {
    expect(state.actionBusy.value).toBe(true);
    expect(state.refreshingMetadata.value).toBe(1);
    expect(await state.refreshMetadata(file(2))).toBe(false);
    expect(await state.queueHealthCheck(file(2))).toBe(false);
    expect(await state.queueTranscode(file(2))).toBe(false);
    expect(await state.skipCooldown(file(2))).toBe(false);
    expect(state.calls).toHaveLength(1);
  } finally {
    release(Response.json({ job_id: 10, kind: 'probe', status: 'queued' }, { status: 202 }));
    await pending;
  }
  expect(state.actionBusy.value).toBe(false);
  expect(state.refreshingMetadata.value).toBeNull();
});

test('cooldown skip eligibility', async () => {
  const state = await handler();
  const now = Date.parse('2026-10-14T12:00:00Z');
  const cooling = {
    cooldown_until: '2026-10-14T13:00:00Z',
    active_job_id: null,
    active_job_status: null,
    transcode_status: 'pending' as const,
    health_status: 'healthy',
  };
  expect(state.canSkipCooldown(cooling, now)).toBe(true);
  expect(state.canSkipCooldown({ ...cooling, cooldown_until: null }, now)).toBe(false);
  expect(state.canSkipCooldown(cooling, Date.parse(cooling.cooldown_until))).toBe(false);
  expect(state.canSkipCooldown({ ...cooling, transcode_status: 'transcoded' }, now)).toBe(false);
  expect(
    state.canSkipCooldown(
      { ...cooling, transcode_status: 'transcoded', health_status: 'pending' },
      now,
    ),
  ).toBe(true);
  expect(
    state.canSkipCooldown(
      { ...cooling, transcode_status: 'hold', active_job_id: 10, active_job_status: 'hold' },
      now,
    ),
  ).toBe(true);
  for (const active_job_status of ['pending', 'queued', 'running']) {
    expect(state.canSkipCooldown({ ...cooling, active_job_id: 10, active_job_status }, now)).toBe(
      false,
    );
  }
});

test('cooldown skip guards', async () => {
  let release!: (response: Response) => void;
  const state = await handler({
    submit: (_url, init) => {
      expect(init?.method).toBe('POST');
      return new Promise((resolve) => (release = resolve));
    },
  });
  const pending = state.skipCooldown(file(23));
  try {
    expect(state.actionBusy.value).toBe(true);
    expect(await state.skipCooldown(file(24))).toBe(false);
    expect(await state.refreshMetadata(file(24))).toBe(false);
    expect(await state.queueHealthCheck(file(24))).toBe(false);
    expect(state.calls).toEqual([{ url: '/api/files/23/skip-cooldown', body: null }]);
  } finally {
    release(Response.json({ id: 23 }));
    expect(await pending).toBe(true);
  }
  expect(state.dialogs).toHaveLength(0);
  expect(state.reloads()).toBe(0);
  expect(state.messages.map(({ kind }) => kind)).toEqual(['success']);
  expect(state.actionBusy.value).toBe(false);
});

test('cooldown skip errors', async () => {
  for (const [submit, text] of [
    [
      async () => Response.json({ detail: 'File already has an active job.' }, { status: 409 }),
      'File already has an active job.',
    ],
    [async () => new Response('Unavailable', { status: 503 }), 'test-fallback'],
    [
      async () => {
        throw new Error('Network unavailable');
      },
      'Network unavailable',
    ],
  ] as const) {
    const state = await handler({ submit });
    expect(await state.skipCooldown(file(1))).toBe(false);
    expect(state.messages).toEqual([{ kind: 'error', text }]);
    expect(state.actionBusy.value).toBe(false);
  }
});

test('bulk cooldown failures', async () => {
  let release!: (response: Response) => void;
  let submitted!: () => void;
  const waiting = new Promise<void>((resolve) => (submitted = resolve));
  const state = await handler({
    submit: async () => {
      submitted();
      return new Promise((resolve) => (release = resolve));
    },
  });
  const pending = state.queueBulk('skip_cooldown', [file(1), file(2), file(3)]);
  const result = {
    accepted: [{ file_id: 1 }],
    failed: [
      { file_id: 2, status: 409, detail: 'File has active work.' },
      { file_id: 3, status: 404, detail: 'file not found' },
    ],
  };
  try {
    await waiting;
    expect(state.actionBusy.value).toBe(true);
    expect(await state.skipCooldown(file(5))).toBe(false);
    expect(await state.refreshMetadata(file(5))).toBe(false);
  } finally {
    release(Response.json(result, { status: 202 }));
    expect(await pending).toEqual(result);
  }
  expect(state.calls).toEqual([
    { url: '/api/files/bulk', body: { action: 'skip_cooldown', file_ids: [1, 2, 3] } },
  ]);
  expect(state.dialogs).toHaveLength(1);
  expect(state.reloads()).toBe(0);
  expect(state.messages.map(({ kind }) => kind)).toEqual(['error']);
  expect(state.messages[0]?.text).toContain('#2');
  expect(state.messages[0]?.text).toContain('#3');
  expect(state.actionBusy.value).toBe(false);
});

test('bulk cooldown cancellation', async () => {
  const state = await handler({ confirmed: false });

  expect(await state.queueBulk('skip_cooldown', [file(1), file(2)])).toBeNull();
  expect(state.calls).toHaveLength(0);
  expect(state.actionBusy.value).toBe(false);
});

test('metadata refresh prerequisites', async () => {
  const state = await handler();
  const settings = [library(1, true)];
  expect(state.refreshMetadataPrerequisite(file(1), settings, false)).toBeNull();
  expect(state.refreshMetadataPrerequisite(file(1), settings, true)).not.toBeNull();
  expect(
    state.refreshMetadataPrerequisite(file(1), [{ ...settings[0]!, suspended: true }], false),
  ).not.toBeNull();
  expect(
    state.refreshMetadataPrerequisite({ ...file(1), active_job_id: 10 }, settings, false),
  ).not.toBeNull();
  expect(state.refreshMetadataPrerequisite(file(1), [], false)).not.toBeNull();
});

test('single enabled healthcheck', async () => {
  const state = await handler();
  expect(await state.queueHealthCheck(file(1))).toBe(true);
  expect(state.dialogs).toHaveLength(0);
  expect(state.calls[0]?.url).toBe('/api/files/1/healthcheck');
  expect(state.reloads()).toBe(1);
  expect(state.messages.map(({ kind }) => kind)).toEqual(['success']);
});

test('health pending acknowledgment', async () => {
  const state = await handler({
    submit: async () =>
      Response.json({ job_id: 10, kind: 'healthcheck', status: 'pending' }, { status: 202 }),
  });
  expect(await state.queueHealthCheck(file(1))).toBe(true);
  expect(state.messages.map(({ kind }) => kind)).toEqual(['success']);
});

test.each(['healthcheck', 'transcode', 'bulk'] as const)(
  'request acknowledges %s',
  async (action) => {
    const accepted = {
      job_id: 10,
      kind: action === 'healthcheck' ? 'healthcheck' : 'transcode',
      status: action === 'healthcheck' ? 'queued' : 'pending',
    };
    const bulk = { accepted: [{ file_id: 1, ...accepted }], failed: [] };
    const state = await handler({
      submit: async (url) => {
        if (url.startsWith('/api/files?'))
          return Response.json({
            items: [],
            pagination: { limit: 50, offset: 0, total: 0 },
          });
        return Response.json(url === '/api/files/bulk' ? bulk : accepted, {
          status: 202,
        });
      },
    });
    await state.load({ limit: 50, offset: 0 });
    state.calls.length = 0;
    if (action === 'bulk') expect(await state.queueBulk('transcode', [file(1)])).toEqual(bulk);
    else if (action === 'healthcheck') expect(await state.queueHealthCheck(file(1))).toBe(true);
    else expect(await state.queueTranscode(file(1))).toBe(true);
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0]?.url).toBe(
      action === 'bulk' ? '/api/files/bulk' : `/api/files/1/${action}`,
    );
    expect(state.actionBusy.value).toBe(false);
    expect(state.isLoading.value).toBe(false);
  },
);

test.each([
  ['not_queued', 'info'],
  ['failed', 'error'],
  ['cancelled', 'info'],
] as const)('request reports %s', async (outcome, kind) => {
  const state = await handler();
  state.notifyOutcome({
    job_id: 10,
    file_id: 1,
    kind: 'transcode',
    outcome,
    reason: 'Already HEVC.',
  });
  expect(state.messages.map(({ kind }) => kind)).toEqual([kind]);
  expect(state.messages[0]?.text).toContain('Already HEVC.');
  expect(state.calls).toHaveLength(0);
});

test('health request failure', async () => {
  const state = await handler();
  state.notifyOutcome({
    job_id: 10,
    file_id: 1,
    kind: 'healthcheck',
    outcome: 'failed',
    reason: 'Source missing.',
  });
  expect(state.messages.map(({ kind }) => kind)).toEqual(['error']);
  expect(state.messages[0]?.text).toContain('Source missing.');
});

test('queued acknowledges once', async () => {
  const state = await handler();
  await state.queueTranscode(file(1));
  state.notifyOutcome({
    job_id: 10,
    file_id: 1,
    kind: 'transcode',
    outcome: 'queued',
    reason: 'Ready.',
  });
  expect(state.messages.map(({ kind }) => kind)).toEqual(['success']);
});

test('transcode stale retry', async () => {
  let attempts = 0;
  const state = await handler({
    submit: async () =>
      ++attempts === 1
        ? Response.json({ detail: 'Flow changed. Reload and try again.' }, { status: 409 })
        : Response.json({ job_id: 10, kind: 'transcode', status: 'pending' }, { status: 202 }),
  });
  expect(await state.queueTranscode(file(1))).toBe(false);
  expect(state.messages).toEqual([
    { kind: 'error', text: 'Flow changed. Reload and try again.' },
  ]);
  expect(state.calls).toHaveLength(1);
  expect(state.actionBusy.value).toBe(false);
  state.flows.value = [savedFlow(2, 5)];
  expect(await state.queueTranscode(file(1))).toBe(true);
  expect(state.flowReloads()).toBe(2);
  expect(state.calls.map(({ body }) => body)).toEqual([
    { flow_id: 2, expected_version: 4 },
    { flow_id: 2, expected_version: 5 },
  ]);
  expect(state.actionBusy.value).toBe(false);
});

test('transcode network error', async () => {
  const state = await handler({
    submit: async () => {
      throw new Error('Network unavailable');
    },
  });
  expect(await state.queueTranscode(file(1))).toBe(false);
  expect(state.messages).toEqual([{ kind: 'error', text: 'Network unavailable' }]);
  expect(state.actionBusy.value).toBe(false);
});

test('single override warning', async () => {
  const state = await handler({ libraries: [library(1, false)] });
  expect(await state.queueHealthCheck(file(1))).toBe(true);
  expect(state.dialogs).toHaveLength(1);
});

test('override cancellation', async () => {
  const state = await handler({
    libraries: [library(1, false)],
    confirmed: false,
  });
  expect(await state.queueHealthCheck(file(1))).toBe(false);
  expect(state.calls).toHaveLength(0);
  expect(state.messages).toHaveLength(0);
  expect(state.actionBusy.value).toBe(false);
});

test('mixed batch warning', async () => {
  const state = await handler({
    libraries: [library(1, false), library(2, true), library(3, false)],
  });
  await state.queueBulk('healthcheck', [file(1), file(2), file(3, 2), file(4, 3)]);
  expect(state.dialogs).toHaveLength(1);
  expect(state.calls).toEqual([
    {
      url: '/api/files/bulk',
      body: { action: 'healthcheck', file_ids: [1, 2, 3, 4] },
    },
  ]);
  expect(state.reloads()).toBe(1);
});

test('batch confirms submission', async () => {
  const state = await handler();
  await state.queueBulk('transcode', [file(1), file(2)]);
  expect(state.dialogs).toHaveLength(1);
  expect(state.calls).toEqual([
    { url: '/api/files/bulk', body: { action: 'transcode', file_ids: [1, 2] } },
  ]);
});

test('batch submits all', async () => {
  const state = await handler({
    libraries: [
      library(1, true),
      { ...library(2, true), flow_id: null },
      { ...library(3, true), suspended: true },
    ],
  });
  await state.queueBulk('transcode', [
    file(1),
    file(2, 2),
    file(3, 3),
    { ...file(4), active_job_id: 12 },
  ]);
  expect(state.calls[0]?.body).toEqual({
    action: 'transcode',
    file_ids: [1, 2, 3, 4],
  });
});

test('batch cancellation', async () => {
  const state = await handler({ confirmed: false });
  expect(await state.queueBulk('transcode', [file(1)])).toBeNull();
  expect(state.calls).toHaveLength(0);
  expect(state.messages).toHaveLength(0);
});

test('batch summarizes acceptance', async () => {
  const result: BulkFileResult = {
    accepted: [
      { file_id: 1, job_id: 10, kind: 'transcode', status: 'pending' },
      { file_id: 2, job_id: 11, kind: 'transcode', status: 'pending' },
    ],
    failed: [{ file_id: 3, status: 409, detail: 'active job' }],
  };
  const state = await handler({ result });
  expect(await state.queueBulk('transcode', [file(1), file(2), file(3)])).toEqual(result);
  expect(state.messages.map(({ kind }) => kind)).toEqual(['error']);
});

test('batch acceptance summary', async () => {
  const state = await handler({
    result: {
      accepted: [{ file_id: 1, job_id: 10, kind: 'transcode', status: 'pending' }],
      failed: [],
    },
  });
  await state.queueBulk('transcode', [file(1)]);
  expect(state.messages.map(({ kind }) => kind)).toEqual(['success']);
});

test('batch rejection summary', async () => {
  const result: BulkFileResult = {
    accepted: [],
    failed: [{ file_id: 1, status: 404, detail: 'Source missing.' }],
  };
  const state = await handler({ result });
  expect(await state.queueBulk('transcode', [file(1)])).toEqual(result);
  expect(state.messages.map(({ kind }) => kind)).toEqual(['error']);
});

test('settings unavailable blocks', async () => {
  for (const options of [
    { settingsError: true },
    { libraries: [] },
    { libraries: [{ id: 1 } as LibraryRow] },
  ]) {
    const state = await handler(options);
    expect(await state.queueHealthCheck(file(1))).toBe(false);
    expect(state.calls).toHaveLength(0);
    expect(state.dialogs).toHaveLength(0);
    expect(state.messages[0]?.kind).toBe('error');
  }
});

test('confirmation blocks duplicates', async () => {
  let resolve!: (value: { status: boolean; value: null }) => void;
  let entered!: () => void;
  const started = new Promise<void>((done) => {
    entered = done;
  });
  const state = await handler({
    dialog: () => {
      entered();
      return new Promise((done) => {
        resolve = done;
      });
    },
  });
  const pending = state.queueBulk('transcode', [file(1)]);
  await started;
  try {
    expect(state.actionBusy.value).toBe(true);
    expect(await state.queueHealthCheck(file(2))).toBe(false);
    expect(await state.refreshMetadata(file(2))).toBe(false);
    expect(await state.queueBulk('healthcheck', [file(2)])).toBeNull();
  } finally {
    resolve({ status: false, value: null });
    await pending;
  }
  expect(state.calls).toHaveLength(0);
  expect(state.actionBusy.value).toBe(false);
});

test('single transcode selects', async () => {
  const state = await handler();
  expect(await state.queueTranscode(file(1))).toBe(true);
  expect(state.dialogs).toHaveLength(0);
  expect(state.calls[0]?.url).toBe('/api/files/1/transcode');
  expect(state.calls[0]?.body).toEqual({ flow_id: 2, expected_version: 4 });
  expect(state.messages.map(({ kind }) => kind)).toEqual(['success']);
});

test('transcode selection snapshot', async () => {
  let loaded!: () => void;
  const loading = new Promise<void>((resolve) => (loaded = resolve));
  const state = await handler({ autoPick: false, reloadFlows: () => loading });
  const pending = state.queueTranscode(file(1));
  try {
    expect(state.picker().loading).toBe(true);
    state.picker().onRun();
    await state.picker().onDryRun();
    expect(state.previewCalls).toHaveLength(0);
    expect(state.calls).toHaveLength(0);
    expect(await state.queueTranscode(file(2))).toBe(false);
    expect(await state.queueHealthCheck(file(2))).toBe(false);
    expect(await state.queueBulk('transcode', [file(2)])).toBeNull();
    loaded();
    await state.pickerReady();
    expect(state.picker().selectedFlowId).toBe(2);
    state.picker()['onUpdate:selectedFlowId'](3);
    await Vue.nextTick();
    expect(state.calls).toHaveLength(0);
    expect(state.picker().flows.find(({ id }) => id === 3)?.version).toBe(7);
    state.flows.value[1]!.version = 8;
    state.flows.value[1]!.definition.entry = 'changed';
    await state.picker().onDryRun();
    expect(state.previewCalls).toEqual([
      { fileId: 1, definition: savedFlow(3, 7).definition },
    ]);
    expect(state.preview().props.flows.map(({ id }) => id)).toEqual([3]);
    expect(state.preview().props.filePath).toBe('/media/1.mkv');
    expect(state.preview().props.facts).toContain('1920×1080');
    expect(state.preview().props.onOpenFile).toBeUndefined();
    expect(state.flowReloads()).toBe(1);
    state.picker().onRun();
    expect(state.calls).toHaveLength(0);
    expect(state.actionBusy.value).toBe(true);
    state.preview().props['onUpdate:open'](false);
    await Vue.nextTick();
    expect(state.picker().selectedFlowId).toBe(3);
    state.picker().onRun();
    state.picker().onRun();
    expect(await pending).toBe(true);
    expect(state.calls).toEqual([
      { url: '/api/files/1/transcode', body: { flow_id: 3, expected_version: 7 } },
    ]);
    expect(state.actionBusy.value).toBe(false);
  } finally {
    loaded();
    state.scope.stop();
    await pending;
  }
});

test('transcode without default', async () => {
  const state = await handler({ libraries: [{ ...library(1, true), flow_id: null }] });
  expect(await state.queueTranscode(file(1))).toBe(true);
  expect(state.calls).toEqual([
    { url: '/api/files/1/transcode', body: { flow_id: 2, expected_version: 4 } },
  ]);
});

test('transcode cancellation reloads', async () => {
  let loaded!: () => void;
  const loading = new Promise<void>((resolve) => (loaded = resolve));
  const state = await handler({ autoPick: false, reloadFlows: () => loading });
  const pending = state.queueTranscode(file(1));
  state.picker()['onUpdate:open'](false);
  expect(await pending).toBe(false);
  expect(state.actionBusy.value).toBe(false);
  expect(state.calls).toHaveLength(0);
  loaded();
  await loading;
  state.flows.value = [savedFlow(3, 9)];
  const reopened = state.queueTranscode(file(1));
  try {
    await state.pickerReady();
    expect(state.flowReloads()).toBe(2);
    expect(state.picker().selectedFlowId).toBe(3);
    state.picker().onRun();
    expect(await reopened).toBe(true);
    expect(state.calls[0]?.body).toEqual({ flow_id: 3, expected_version: 9 });
  } finally {
    state.scope.stop();
    await reopened;
  }
});

test.each(['failure', 'empty'] as const)('transcode flow %s', async (condition) => {
  const state = await handler({
    autoPick: false,
    flows: condition === 'empty' ? [] : undefined,
    flowError: condition === 'failure' ? 'Saved flows are unavailable.' : undefined,
  });
  const pending = state.queueTranscode(file(1));
  try {
    await state.pickerReady();
    expect(state.picker().loading).toBe(false);
    if (condition === 'failure') expect(state.picker().error).toBe('Saved flows are unavailable.');
    else expect(state.picker().flows).toHaveLength(0);
    state.picker().onRun();
    expect(state.calls).toHaveLength(0);
    state.picker()['onUpdate:open'](false);
    expect(await pending).toBe(false);
    expect(state.actionBusy.value).toBe(false);
  } finally {
    state.scope.stop();
    await pending;
  }
});

test('transcode disposal cancels', async () => {
  let finish!: (result: DryRunResult) => void;
  let started!: () => void;
  const evaluating = new Promise<void>((resolve) => (started = resolve));
  const state = await handler({
    autoPick: false,
    evaluate: () => {
      started();
      return new Promise((resolve) => (finish = resolve));
    },
  });
  const pending = state.queueTranscode(file(1));
  await state.pickerReady();
  const previewing = state.picker().onDryRun();
  await evaluating;
  state.scope.stop();
  finish(previewResult('After disposal.'));
  await previewing;
  expect(await pending).toBe(false);
  expect(state.preview().closed).toBe(true);
  expect(state.preview().props.result).toBeNull();
  expect(state.calls).toHaveLength(0);
  expect(state.actionBusy.value).toBe(false);
  expect(await state.queueTranscode(file(1))).toBe(false);
});

test('preview ignores stale', async () => {
  let finish!: (result: DryRunResult) => void;
  let started!: () => void;
  const evaluating = new Promise<void>((resolve) => (started = resolve));
  let evaluations = 0;
  const state = await handler({
    autoPick: false,
    evaluate: () => {
      if (++evaluations > 1) return Promise.resolve(previewResult('Selected flow.'));
      started();
      return new Promise((resolve) => (finish = resolve));
    },
  });
  const pending = state.queueTranscode(file(12));
  let earlier: Promise<void> | undefined;
  try {
    await state.pickerReady();
    earlier = state.picker().onDryRun();
    await evaluating;
    state.picker()['onUpdate:selectedFlowId'](3);
    expect(state.preview().closed).toBe(true);
    await state.picker().onDryRun();
    finish(previewResult('Stale flow.'));
    await earlier;
    await Vue.nextTick();
    expect(state.preview().props.result?.reason).toBe('Selected flow.');
    expect(state.preview().props.flows[0]?.id).toBe(3);
    expect(state.previewCalls).toEqual([
      { fileId: 12, definition: savedFlow(2, 4).definition },
      { fileId: 12, definition: savedFlow(3, 7).definition },
    ]);
    state.picker()['onUpdate:open'](false);
    expect(await pending).toBe(false);
    expect(state.preview().closed).toBe(true);
    expect(state.calls).toHaveLength(0);
    expect(state.actionBusy.value).toBe(false);
  } finally {
    finish?.(previewResult());
    state.scope.stop();
    await earlier;
    await pending;
  }
});

test('preview opening cancels', async () => {
  let loaded!: () => void;
  const loading = new Promise<void>((resolve) => (loaded = resolve));
  const state = await handler({ autoPick: false, ensurePlugins: () => loading });
  const pending = state.queueTranscode(file(1));
  let previewing: Promise<void> | undefined;
  try {
    await state.pickerReady();
    previewing = state.picker().onDryRun();
    await state.picker().onDryRun();
    state.picker().onRun();
    expect(state.calls).toHaveLength(0);
    state.picker()['onUpdate:open'](false);
    expect(await pending).toBe(false);
    loaded();
    await previewing;
    expect(state.preview()).toBeUndefined();
    expect(state.previewCalls).toHaveLength(0);
    expect(state.actionBusy.value).toBe(false);
  } finally {
    loaded();
    state.scope.stop();
    await previewing;
    await pending;
  }
});

test('preview failure returns', async () => {
  const state = await handler({
    autoPick: false,
    evaluate: async () => {
      throw { detail: 'Source missing.' };
    },
  });
  const pending = state.queueTranscode(file(1));
  try {
    await state.pickerReady();
    await state.picker().onDryRun();
    expect(state.preview().props.error).toBe('Source missing.');
    expect(state.preview().props.running).toBe(false);
    expect(state.calls).toHaveLength(0);
    state.preview().props['onUpdate:open'](false);
    state.picker().onRun();
    expect(await pending).toBe(true);
    expect(state.calls[0]?.body).toEqual({ flow_id: 2, expected_version: 4 });
  } finally {
    state.scope.stop();
    await pending;
  }
});

test('transcode prerequisites block', async () => {
  for (const options of [
    { libraries: [] },
    { libraries: [{ ...library(1, true), suspended: true }] },
    {},
  ]) {
    const state = await handler(options);
    const selected = { ...file(1), active_job_id: 'libraries' in options ? null : 10 };
    expect(await state.queueTranscode(selected)).toBe(false);
    expect(state.calls).toHaveLength(0);
    expect(state.flowReloads()).toBe(0);
    expect(state.messages[0]?.kind).toBe('error');
    expect(state.actionBusy.value).toBe(false);
  }
});

test('submission blocks duplicates', async () => {
  let release!: (response: Response) => void;
  let entered!: () => void;
  const started = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const state = await handler({
    submit: () => {
      entered();
      return new Promise((resolve) => {
        release = resolve;
      });
    },
  });
  const pending = state.queueBulk('healthcheck', [file(1)]);
  await started;
  try {
    expect(state.actionBusy.value).toBe(true);
    expect(await state.queueTranscode(file(2))).toBe(false);
    expect(state.calls).toHaveLength(1);
  } finally {
    release(
      Response.json(
        {
          accepted: [{ file_id: 1, job_id: 10, kind: 'healthcheck', status: 'queued' }],
          failed: [],
        },
        { status: 202 },
      ),
    );
    await pending;
  }
  expect(state.actionBusy.value).toBe(false);
});
