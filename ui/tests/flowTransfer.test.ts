import { expect, test } from 'bun:test';
import type { FlowDefinition, PluginRow } from '../app/types/api';
import {
  decodeFlow,
  encodeFlow,
  flowExport,
  hasFlowPasswords,
  importedFlow,
} from '../app/utils/flowTransfer';

const definition: FlowDefinition = {
  entry: 'remux',
  nodes: [
    {
      id: 'remux',
      type: 'plugin',
      plugin: 'transcode.remux',
      config_version: 2,
      inputs: { container: 'mkv' },
    },
    { id: 'done', type: 'terminal', outcome: 'done' },
  ],
  edges: [{ src: 'remux', port: 'plan', dst: 'done' }],
};
const flow = { name: 'Films 🎬', definition };

const plugin: PluginRow = {
  manifest: {
    id: 'action.webhook',
    kind: 'action',
    name: 'Webhook',
    description: '',
    author: '',
    url: '',
    help: null,
    version: '1.0.0',
    config_version: 1,
    migrations: [],
    facets: {},
    requires: [],
    require_plugins: [],
  },
  state: 'ready',
  source: 'bundled',
  inputs: [
    {
      name: 'token',
      type: 'password',
      default: '',
      required: false,
      nullable: true,
      min: null,
      max: null,
      values: null,
      help: null,
    },
  ],
};

test('detects populated passwords', () => {
  const withPassword: FlowDefinition = {
    ...definition,
    nodes: [
      ...definition.nodes,
      { id: 'notify', type: 'plugin', plugin: plugin.manifest.id, inputs: { token: 'example-key' } },
    ],
  };
  expect(hasFlowPasswords({ definition: withPassword }, [plugin])).toBe(true);
  expect(hasFlowPasswords(flow, [plugin])).toBe(false);
});

test('ignores empty passwords', () => {
  for (const inputs of [{}, { token: null }, { token: '' }]) {
    const withoutPassword: FlowDefinition = {
      ...definition,
      nodes: [{ id: 'notify', type: 'plugin', plugin: plugin.manifest.id, inputs }],
    };
    expect(hasFlowPasswords({ definition: withoutPassword }, [plugin])).toBe(false);
  }
});

test('ignores ordinary fields', () => {
  const ordinary: PluginRow = {
    ...plugin,
    inputs: [{ ...plugin.inputs[0]!, name: 'api_key', type: 'str' }],
  };
  const withText: FlowDefinition = {
    ...definition,
    nodes: [
      { id: 'notify', type: 'plugin', plugin: plugin.manifest.id, inputs: { api_key: 'example' } },
    ],
  };
  expect(hasFlowPasswords({ definition: withText }, [ordinary])).toBe(false);
});

test('imports current export', () => {
  const exported = flowExport(flow);
  expect(exported).toEqual({ _type: 'flow', _version: 1, ...flow });
  expect(importedFlow(JSON.parse(JSON.stringify(exported)))).toEqual({
    ...flow,
    id: 0,
    version: 0,
    created_at: '',
    updated_at: '',
  });
});

test('roundtrips Unicode shares', () => {
  expect(importedFlow(decodeFlow(encodeFlow(flow)))?.definition).toEqual(definition);
  expect(importedFlow(decodeFlow(encodeFlow(flow)))?.name).toBe(flow.name);
});

test('rejects invalid exports', () => {
  for (const exported of [
    null,
    [],
    flow,
    { _type: 'flow', ...flow },
    { _version: 1, ...flow },
    { _type: 'flow', _version: 2, ...flow },
    { _type: 'flow', _version: '1', ...flow },
    { _type: 'job', _version: 1, ...flow },
  ]) {
    expect(importedFlow(exported)).toBeNull();
  }
});

test('rejects invalid graphs', () => {
  for (const invalid of [
    null,
    [],
    { ...definition, entry: null },
    { ...definition, nodes: null },
    { ...definition, edges: null },
    { ...definition, nodes: [{ id: 'done' }] },
    { ...definition, edges: [{ src: 'remux', dst: 'done' }] },
  ]) {
    expect(importedFlow({ ...flowExport(flow), definition: invalid })).toBeNull();
  }
});
