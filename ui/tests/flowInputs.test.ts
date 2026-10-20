import { expect, test } from 'bun:test';
import { reactive } from 'vue';
import { buildInputs, initializeInputs, newPluginNode } from '../app/utils/stepDraft';
import type { PluginInputField } from '../app/types/api';

test('preserves input edits', () => {
  const saved = reactive({ count: 0, enabled: false, tags: [], future: { nested: 'value' } });
  const node = reactive(newPluginNode());
  node.rawInputs = JSON.parse(JSON.stringify(saved));
  initializeInputs(node, []);
  expect(buildInputs(node, [])).toEqual(saved);
  const field = (
    name: string,
    type: PluginInputField['type'],
    value: unknown,
  ): PluginInputField => ({
    name,
    type,
    default: value,
    required: false,
    nullable: false,
    min: null,
    max: null,
    values: null,
    help: null,
  });
  const schema = [
    field('count', 'int', 9),
    field('enabled', 'bool', true),
    field('tags', 'strings', ['default']),
  ];
  initializeInputs(node, schema);
  expect(buildInputs(node, schema)).toEqual(saved);
  node.inputs.count = 7;
  initializeInputs(node, [...schema, field('later', 'str', 'new')]);
  expect(buildInputs(node, schema)).toEqual({ ...saved, count: 7 });
  expect(saved.count).toBe(0);
  initializeInputs(node, [field('tags', 'bool', true)]);
  expect(buildInputs(node, [field('tags', 'bool', true)]).tags).toEqual([]);
  const optional = newPluginNode();
  const optionalFields = [field('encoder', 'str', null)];
  initializeInputs(optional, optionalFields);
  expect(buildInputs(optional, optionalFields)).toEqual({});
});

test('argv preserves tokens', () => {
  const field: PluginInputField = {
    name: 'extras',
    type: 'strings',
    default: [],
    required: false,
    nullable: false,
    min: null,
    max: null,
    values: null,
    help: null,
  };
  const node = reactive(newPluginNode());
  const original = ['-metadata', 'title=A,B', ' spaces stay ', ''];
  node.rawInputs = { extras: [...original], future: { kept: true } };
  initializeInputs(node, [field]);
  expect(buildInputs(node, [field])).toEqual(node.rawInputs);
  const args = node.inputs.extras;
  if (!Array.isArray(args)) throw new Error('Expected argv draft');
  args[1] = 'title=A,C';
  expect(buildInputs(node, [field])).toEqual({
    extras: ['-metadata', 'title=A,C', ' spaces stay ', ''],
    future: { kept: true },
  });
  expect(node.rawInputs.extras).toEqual(original);
  args[1] = 'title=A,B';
  expect(buildInputs(node, [field]).extras).toEqual(original);
});

test('clear nullable strings', () => {
  const encoder: PluginInputField = {
    name: 'encoder',
    type: 'str',
    default: null,
    required: false,
    nullable: true,
    min: null,
    max: null,
    values: null,
    help: null,
  };
  const node = newPluginNode();
  node.rawInputs = { encoder: 'libx265', encoder_priority: ['hevc_nvenc'] };
  initializeInputs(node, [encoder]);
  node.inputs.encoder = '';
  expect(buildInputs(node, [encoder])).toEqual({
    encoder: null,
    encoder_priority: ['hevc_nvenc'],
  });

  for (const required of [false, true]) {
    const field = { ...encoder, required, default: 'fast' };
    for (const value of [null, '', 'slow', { unknown: true }]) {
      const untouched = newPluginNode();
      untouched.rawInputs = { encoder: value };
      initializeInputs(untouched, [field]);
      expect(buildInputs(untouched, [field])).toEqual({ encoder: value });
    }
    const defaults = newPluginNode();
    initializeInputs(defaults, [field]);
    expect(buildInputs(defaults, [field])).toEqual({});
    defaults.inputs.encoder = '';
    expect(buildInputs(defaults, [field])).toEqual({ encoder: null });
  }
  const text = { ...encoder, name: 'label', nullable: false };
  const label = newPluginNode();
  label.rawInputs = { label: 'prior' };
  initializeInputs(label, [text]);
  label.inputs.label = '';
  expect(buildInputs(label, [text])).toEqual({ label: '' });
});

test('lists preserve values', () => {
  const field: PluginInputField = {
    name: 'targets',
    type: 'strings',
    default: ['a,b'],
    required: false,
    nullable: false,
    min: null,
    max: null,
    values: null,
    help: null,
  };
  for (const raw of [null, 'unrecognized', [5, null], { unknown: true }]) {
    const node = newPluginNode();
    node.rawInputs = { targets: raw };
    initializeInputs(node, [field]);
    expect(buildInputs(node, [field])).toEqual({ targets: raw });
    node.inputs.targets = ['mail,personal', 'codec with space', ''];
    expect(buildInputs(node, [field]).targets).toEqual(['mail,personal', 'codec with space', '']);
  }
  const defaults = newPluginNode();
  initializeInputs(defaults, [field]);
  expect(defaults.inputs.targets).toEqual(['a,b']);
  expect(buildInputs(defaults, [field])).toEqual({});
});
