import { expect, test } from 'bun:test';
import { computed, ref } from 'vue';
import type { DryRunResult, FlowDefinition } from '../app/types/api';
import { tracePath, tracePort } from '../app/utils/flowTrace';

const definition: FlowDefinition = {
  entry: 'notify',
  nodes: [
    { id: 'notify', type: 'plugin', plugin: 'action.notify', inputs: {} },
    { id: 'gate', type: 'condition', when: { 'media.video.count': { gt: 0 } } },
    { id: 'enc', type: 'plugin', plugin: 'transcode.video', inputs: {} },
    { id: 'post', type: 'plugin', plugin: 'action.keep_dates', inputs: {} },
    { id: 'done', type: 'terminal', outcome: 'done' },
    { id: 'skip', type: 'terminal', outcome: 'skip' },
  ],
  edges: [
    { src: 'notify', port: 'next', dst: 'gate' },
    { src: 'gate', port: 'true', dst: 'enc' },
    { src: 'gate', port: 'false', dst: 'skip' },
    { src: 'enc', port: 'plan', dst: 'post' },
    { src: 'enc', port: 'skip', dst: 'skip' },
    { src: 'post', port: 'next', dst: 'done' },
    { src: 'skip', port: 'next', dst: 'done' },
  ],
};

test('updates dryrun paths', () => {
  const result = ref<DryRunResult | null>(null);
  const path = computed(() => tracePath(definition, result.value?.steps ?? []));
  expect([...path.value.taken]).toEqual([]);
  result.value = {
    status: 'matched',
    reason: '',
    steps: [
      { node: 'notify', step: 'action.notify', outcome: 'actions' },
      { node: 'gate', outcome: 'true' },
      { node: 'enc', step: 'transcode.video', outcome: 'plan' },
      {
        node: 'post',
        step: 'action.keep_dates',
        outcome: 'deferred',
        reason: 'awaits the verified output probe',
      },
    ],
    facets: {},
    plan: null,
    argv: null,
    stream_log: [],
  };
  expect([...path.value.taken]).toEqual(['notify:next:gate', 'gate:true:enc', 'enc:plan:post']);
  expect([...path.value.reached]).toEqual(['notify', 'gate', 'enc', 'post']);
  result.value = {
    ...result.value,
    status: 'skipped',
    steps: [
      { node: 'notify', step: 'action.notify', outcome: 'actions' },
      { node: 'gate', outcome: 'false' },
      { node: 'skip', outcome: 'skip' },
      { node: 'done', outcome: 'done' },
    ],
  };
  expect([...path.value.taken]).toEqual(['notify:next:gate', 'gate:false:skip', 'skip:next:done']);
  expect([...path.value.reached]).toEqual(['notify', 'gate', 'skip', 'done']);
  result.value = null;
  expect([...path.value.taken]).toEqual([]);
  expect([...path.value.reached]).toEqual([]);
});

test('stops unevaluated routes', () => {
  const post = definition.nodes.find((node) => node.id === 'post');
  expect(tracePort(post, { node: 'post', outcome: 'deferred' })).toBeNull();
  expect(tracePort(post, { node: 'post', outcome: 'error', error: 'invalid actions' })).toBeNull();
  expect(tracePath(definition, [{ node: 'post', outcome: 'deferred' }]).taken.size).toBe(0);
  expect(tracePath(definition, [{ node: 'post', outcome: 'error' }]).taken.size).toBe(0);
});
