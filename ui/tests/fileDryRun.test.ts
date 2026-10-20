import { expect, test } from 'bun:test';
import * as Vue from 'vue';
import ts from 'typescript';
import type { DryRunResult, FlowRow, MediaFileRow } from '../app/types/api';
import type { useFileDryRun } from '../app/composables/useFileDryRun';

const source = await Bun.file(
  new URL('../app/composables/useFileDryRun.ts', import.meta.url),
).text();
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

const flow = (id: number): FlowRow => ({
  id,
  name: `Flow ${id}`,
  version: 4,
  created_at: '',
  updated_at: '',
  definition: { entry: `stage-${id}`, nodes: [], edges: [] },
});
const file: MediaFileRow = {
  id: 12,
  library_id: 7,
  path: '/media/Movies/Drama/Film.mkv',
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
  active_job_id: null,
  active_job_status: null,
  active_job_progress: null,
  active_job_updated_at: null,
};
const result = (reason = 'Current evaluation.'): DryRunResult => ({
  status: 'matched',
  reason,
  steps: [],
  facets: {},
  plan: { video: { encoder: 'libx265' } },
  argv: ['ffmpeg', '-i', 'input.mkv'],
  stream_log: [],
});
const pending = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
};
type PreviewProps = {
  open: boolean;
  selectedFlowId?: number;
  filePath: string;
  flows: FlowRow[];
  location: string;
  facts: string;
  running: boolean;
  result: DryRunResult | null;
  error: string;
  argvPreview: string;
  'onUpdate:open': (open: boolean) => void;
  'onUpdate:selectedFlowId': (id: number | undefined) => void;
  onOpenFile: () => void;
  onRun: () => Promise<void>;
};

const setup = () => {
  const flows = Vue.ref([flow(1), flow(3)]);
  const flowError = Vue.ref<{ detail: string } | null>(null);
  const libraries = Vue.ref([{ id: 7, name: 'Movies', roots: ['/media/Movies'], flow_id: 1 }]);
  const calls: Array<{ id: number; definition: FlowRow['definition'] }> = [];
  const openedFiles: number[] = [];
  let reloadCount = 0;
  let reload = async (): Promise<void> => {};
  let evaluate = async (): Promise<DryRunResult> => result();
  const modals: Array<{
    props: PreviewProps;
    closes: number;
    close: () => void;
    patch: (props: PreviewProps) => void;
    open: () => { result: Promise<void> };
  }> = [];
  const overlay = {
    create: (_component: unknown, options: { props: PreviewProps }) => {
      const done = pending<undefined>();
      const modal = {
        props: options.props,
        closes: 0,
        close: () => {
          modal.closes++;
          done.resolve(undefined);
        },
        patch: (props: PreviewProps) => (modal.props = props),
        open: () => ({ result: done.promise }),
      };
      modals.push(modal);
      return modal;
    },
  };
  const modules: Record<string, unknown> = {
    vue: Vue,
    '~/components/FlowDryRunModal.vue': {},
    '~/composables/useFlows': {
      useFlows: () => ({
        items: flows,
        lastError: flowError,
        reload: () => {
          reloadCount++;
          return reload();
        },
        dryRun: (id: number, definition: FlowRow['definition']) => {
          calls.push({ id, definition });
          return evaluate();
        },
      }),
    },
    '~/composables/usePlugins': {
      usePlugins: () => ({ items: Vue.ref([]), ensure: async () => {} }),
    },
    '~/composables/useLibraries': { useLibraries: () => ({ items: libraries }) },
    '~/composables/useApiErrorMessage': {
      useApiErrorMessage: () => ({
        messageFor: (detail: string, fallback: string) => detail || fallback,
      }),
    },
    '~/utils': {
      formatBytes: (value: number) => `${value} bytes`,
      formatTime: (value: number) => `${value} seconds`,
      formatBitrate: (value: number) => `${value} bps`,
    },
    '~/utils/location': { libraryLocation: () => 'Movies/Drama' },
  };
  const exports: { useFileDryRun?: typeof useFileDryRun } = {};
  new Function('require', 'exports', 'useOverlay', compiled)(
    (name: string) => {
      if (!(name in modules)) throw new Error(`Unexpected import: ${name}`);
      return modules[name];
    },
    exports,
    () => overlay,
  );
  const scope = Vue.effectScope();
  const preview = scope.run(() => exports.useFileDryRun!((id) => openedFiles.push(id)))!;
  return {
    preview,
    scope,
    flows,
    flowError,
    libraries,
    calls,
    modals,
    openedFiles,
    props: () => modals.at(-1)!.props,
    reloadCount: () => reloadCount,
    reload: (next: typeof reload) => (reload = next),
    evaluate: (next: typeof evaluate) => (evaluate = next),
  };
};

test('opens batch flow', async () => {
  const env = setup();
  try {
    env.reload(async () => {
      env.flows.value = [
        flow(1),
        { ...flow(3), definition: { entry: 'saved', nodes: [], edges: [] } },
      ];
    });
    await env.preview.open(file, 3);
    await Vue.nextTick();
    expect(env.props().selectedFlowId).toBe(3);
    expect(env.calls).toEqual([{ id: file.id, definition: env.flows.value[1]!.definition }]);
    expect(env.reloadCount()).toBe(1);
    expect(env.props().result).toEqual(result());
    expect(env.props().argvPreview).toBe('ffmpeg -i input.mkv');
    expect(env.props().filePath).toBe(file.path);
    expect(env.props().location).toBe('Movies/Drama');
    expect(env.props().facts).toContain('1920×1080');
    env.props().onOpenFile();
    expect(env.openedFiles).toEqual([file.id]);
  } finally {
    env.scope.stop();
  }
});

test('captured flow fixed', async () => {
  const env = setup();
  const captured = { ...flow(3), definition: { entry: 'captured', nodes: [], edges: [] } };
  try {
    env.flowError.value = { detail: 'Unrelated reload failed.' };
    await env.preview.open(file, captured);
    expect(env.calls).toEqual([{ id: file.id, definition: captured.definition }]);
    expect(env.props().flows).toEqual([captured]);
    expect(env.props().error).toBe('');
    expect(env.reloadCount()).toBe(0);
    env.preview.close();
    env.flowError.value = null;
    await env.preview.open(file, 1);
    expect(env.calls.at(-1)).toEqual({ id: file.id, definition: flow(1).definition });
    expect(env.reloadCount()).toBe(1);
    expect(env.props().flows.map(({ id }) => id)).toEqual([1, 3]);
  } finally {
    env.scope.stop();
  }
});

test('selects library flow', async () => {
  const env = setup();
  try {
    env.libraries.value[0]!.flow_id = 3;
    await env.preview.open(file);
    expect(env.props().selectedFlowId).toBe(3);
    env.libraries.value = [];
    await env.preview.open(file);
    expect(env.props().selectedFlowId).toBe(1);
  } finally {
    env.scope.stop();
  }
});

test('selection clears result', async () => {
  const env = setup();
  const evaluation = pending<DryRunResult>();
  try {
    await env.preview.open(file, 3);
    env.evaluate(() => evaluation.promise);
    env.props()['onUpdate:selectedFlowId'](1);
    await Vue.nextTick();
    expect(env.props().selectedFlowId).toBe(1);
    expect(env.props().result).toBeNull();
    expect(env.props().running).toBe(true);
    expect(env.props().argvPreview).toBe('');
    evaluation.resolve(result('New selection.'));
    await evaluation.promise;
    await Vue.nextTick();
    expect(env.props().result?.reason).toBe('New selection.');
  } finally {
    env.scope.stop();
  }
});

test('selection ignores stale', async () => {
  const env = setup();
  const stale = pending<DryRunResult>();
  try {
    await env.preview.open(file, 3);
    env.evaluate(() => stale.promise);
    const earlier = env.props().onRun();
    env.evaluate(async () => result('Selected flow.'));
    env.props()['onUpdate:selectedFlowId'](1);
    await Vue.nextTick();
    stale.reject({ detail: 'Stale failure.' });
    await earlier;
    await Vue.nextTick();
    expect(env.props().result?.reason).toBe('Selected flow.');
    expect(env.props().error).toBe('');
    expect(env.props().running).toBe(false);
  } finally {
    env.scope.stop();
  }
});

test('cleared selection stops', async () => {
  const env = setup();
  try {
    await env.preview.open(file, 3);
    env.props()['onUpdate:selectedFlowId'](undefined);
    await Vue.nextTick();
    expect(env.calls).toHaveLength(1);
    expect(env.props().result).toBeNull();
    expect(env.props().running).toBe(false);
  } finally {
    env.scope.stop();
  }
});

test('open ignores earlier', async () => {
  const env = setup();
  const loading = pending<undefined>();
  try {
    env.reload(() => loading.promise);
    const earlier = env.preview.open(file, 3);
    env.reload(async () => {});
    await env.preview.open({ ...file, id: 99 }, 1);
    loading.resolve(undefined);
    await earlier;
    expect(env.preview.fileId.value).toBe(99);
    expect(env.calls.map(({ id }) => id)).toEqual([99]);
    expect(env.modals).toHaveLength(1);
  } finally {
    env.scope.stop();
  }
});

test('close stops opening', async () => {
  const env = setup();
  const loading = pending<undefined>();
  try {
    env.reload(() => loading.promise);
    const opening = env.preview.open(file, 3);
    env.preview.close();
    loading.resolve(undefined);
    await opening;
    expect(env.preview.isOpen.value).toBe(false);
    expect(env.modals).toHaveLength(0);
    expect(env.calls).toHaveLength(0);
  } finally {
    env.scope.stop();
  }
});

test('close ignores evaluation', async () => {
  const env = setup();
  const evaluation = pending<DryRunResult>();
  try {
    await env.preview.open(file, 3);
    env.evaluate(() => evaluation.promise);
    const running = env.props().onRun();
    env.props()['onUpdate:open'](false);
    evaluation.resolve(result('Stale result.'));
    await running;
    expect(env.preview.isOpen.value).toBe(false);
    expect(env.preview.fileId.value).toBeUndefined();
    expect(env.modals[0]!.closes).toBe(1);
    env.evaluate(async () => result('Reopened.'));
    await env.preview.open(file, 3);
    await Vue.nextTick();
    expect(env.modals).toHaveLength(2);
    expect(env.props().result?.reason).toBe('Reopened.');
  } finally {
    env.scope.stop();
  }
});

test('dispose stops opening', async () => {
  const env = setup();
  const loading = pending<undefined>();
  env.reload(() => loading.promise);
  const opening = env.preview.open(file, 3);
  env.scope.stop();
  loading.resolve(undefined);
  await opening;
  await env.preview.open(file, 3);
  expect(env.preview.isOpen.value).toBe(false);
  expect(env.modals).toHaveLength(0);
  expect(env.calls).toHaveLength(0);
});

test('dispose closes evaluation', async () => {
  const env = setup();
  const evaluation = pending<DryRunResult>();
  await env.preview.open(file, 3);
  env.evaluate(() => evaluation.promise);
  const running = env.props().onRun();
  env.scope.stop();
  evaluation.resolve(result());
  await running;
  expect(env.preview.isOpen.value).toBe(false);
  expect(env.preview.fileId.value).toBeUndefined();
  expect(env.modals[0]!.closes).toBe(1);
});

test('reports evaluation failure', async () => {
  const env = setup();
  try {
    env.evaluate(async () => {
      throw { detail: 'File is unavailable.' };
    });
    await env.preview.open(file, 3);
    await Vue.nextTick();
    expect(env.props().error).toBe('File is unavailable.');
    expect(env.props().running).toBe(false);
    env.evaluate(async () => result());
    await env.props().onRun();
    await Vue.nextTick();
    expect(env.props().error).toBe('');
    expect(env.props().result).toEqual(result());
  } finally {
    env.scope.stop();
  }
});

test('missing flow preserved', async () => {
  const env = setup();
  try {
    env.flows.value = [flow(1)];
    await env.preview.open(file, 3);
    await Vue.nextTick();
    expect(env.props().selectedFlowId).toBe(3);
    expect(env.props().error).not.toBe('');
    expect(env.calls).toHaveLength(0);
  } finally {
    env.scope.stop();
  }
});

test('reload failure blocks', async () => {
  const env = setup();
  try {
    env.reload(async () => {
      env.flowError.value = { detail: 'Could not refresh flows.' };
    });
    await env.preview.open(file, 3);
    await Vue.nextTick();
    expect(env.props().error).toBe('Could not refresh flows.');
    expect(env.calls).toHaveLength(0);
  } finally {
    env.scope.stop();
  }
});
