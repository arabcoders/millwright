import type { PluginInputField } from '~/types/api';
import { emptyCondition, type DraftCondition } from '~/utils/condition';

export type DraftInput = string | number | boolean | string[];

export type DraftPluginNode = {
  kind: 'plugin';
  uid: string;
  plugin: string;
  configVersion: number;
  inputs: Record<string, DraftInput>;
  rawInputs: Record<string, unknown>;
  initialInputs: Record<string, DraftInput>;
};
export type DraftConditionNode = { kind: 'condition'; uid: string; condition: DraftCondition };
export type DraftTerminalNode = {
  kind: 'terminal';
  uid: string;
  outcome: 'done' | 'skip' | 'veto';
};
export type DraftNode = DraftPluginNode | DraftConditionNode | DraftTerminalNode;

export type CanvasSelection = { kind: 'node'; id: string };

let uidCounter = 0;

const nextUid = (used: Set<string> = new Set()): string => {
  let uid: string;
  do uid = `node-${++uidCounter}`;
  while (used.has(uid));
  return uid;
};

export const newPluginNode = (used?: Set<string>): DraftPluginNode => ({
  uid: nextUid(used),
  kind: 'plugin',
  plugin: '',
  configVersion: 1,
  inputs: {},
  rawInputs: {},
  initialInputs: {},
});

export const newConditionNode = (used?: Set<string>): DraftConditionNode => ({
  uid: nextUid(used),
  kind: 'condition',
  condition: emptyCondition(),
});
export const newTerminalNode = (used?: Set<string>): DraftTerminalNode => ({
  uid: nextUid(used),
  kind: 'terminal',
  outcome: 'done',
});

export const inputDraft = (field: PluginInputField): DraftInput => {
  if (field.type === 'strings') {
    return Array.isArray(field.default) ? field.default.map(String) : [];
  }
  if (field.type === 'bool') return field.default === true;
  if (field.default === null || field.default === undefined) return '';
  if (typeof field.default === 'string' || typeof field.default === 'number') return field.default;
  return '';
};

export const toDraft = (field: PluginInputField, value: unknown): DraftInput => {
  if (field.type === 'strings') {
    return Array.isArray(value) ? value.map(String) : [];
  }
  if (field.type === 'bool') return value === true;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return value;
  }
  return '';
};

export const initializeInputs = (node: DraftPluginNode, fields: PluginInputField[]): void => {
  for (const field of fields) {
    if (field.name in node.inputs) continue;
    if (field.name in node.rawInputs) {
      const value = toDraft(field, node.rawInputs[field.name]);
      node.inputs[field.name] = value;
      node.initialInputs[field.name] = Array.isArray(value) ? [...value] : value;
    } else if (!field.required) {
      const value = inputDraft(field);
      node.inputs[field.name] = value;
      node.initialInputs[field.name] = Array.isArray(value) ? [...value] : value;
    }
  }
};

export const buildInputs = (
  node: DraftPluginNode,
  fields: PluginInputField[],
): Record<string, unknown> => {
  const out = { ...node.rawInputs };
  for (const field of fields) {
    const raw = node.inputs[field.name];
    if (raw === undefined) continue;
    if (JSON.stringify(raw) === JSON.stringify(node.initialInputs[field.name])) continue;
    if (field.type === 'strings') out[field.name] = raw;
    else if (field.type === 'int' || field.type === 'float') {
      if (raw === '') Reflect.deleteProperty(out, field.name);
      else out[field.name] = Number(raw);
    } else out[field.name] = field.type === 'str' && field.nullable && raw === '' ? null : raw;
  }
  return out;
};
