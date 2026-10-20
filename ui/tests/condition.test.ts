import { expect, test } from 'bun:test';
import {
  conditionFromWhen,
  emptyCondition,
  conditionProblem,
  conditionToWhen,
  isNumericOperand,
  type ConditionRow,
} from '../app/utils/condition';

const types = new Map([
  ['depth', 'int'],
  ['rate', 'float'],
  ['codec', 'str'],
  ['enabled', 'bool'],
]);

test('converts declared operands', () => {
  for (const op of ['eq', 'ne'] as const) {
    const rows: ConditionRow[] = [
      { uid: 1, key: 'depth', op, value: '10' },
      { uid: 2, key: 'rate', op, value: '10.5' },
      { uid: 3, key: 'codec', op, value: '10' },
      { uid: 4, key: 'enabled', op, value: 'false' },
    ];
    const condition = { mode: 'all' as const, rows, custom: '' };
    const operands = { depth: 10, rate: 10.5, codec: '10', enabled: false };
    expect(conditionProblem('step', condition, types)).toBe('');
    expect(conditionToWhen(condition, types)).toEqual(
      op === 'eq'
        ? operands
        : Object.fromEntries(Object.entries(operands).map(([key, value]) => [key, { ne: value }])),
    );
    expect(rows.map((row) => isNumericOperand(row, types.get(row.key)))).toEqual([
      true,
      true,
      false,
      false,
    ]);
    rows[2]!.value = 10;
    rows[3]!.value = 'true';
    expect(conditionToWhen(condition, types)).toEqual(
      op === 'eq'
        ? { ...operands, enabled: true }
        : { depth: { ne: 10 }, rate: { ne: 10.5 }, codec: { ne: '10' }, enabled: { ne: true } },
    );
  }
});

test('preserves untouched literals', () => {
  const saved = { depth: '10', rate: { ne: 10.5 }, enabled: true, codec: ['10', '20'] };
  const condition = conditionFromWhen(saved);
  expect(conditionToWhen(condition, types)).toEqual(saved);
  condition.rows[0]!.value = '12';
  condition.rows[2]!.value = 'false';
  expect(conditionToWhen(condition, types)).toEqual({ ...saved, depth: 12, enabled: false });
});

test('validates typed operands', () => {
  for (const op of ['eq', 'ne'] as const) {
    for (const key of ['depth', 'rate', 'enabled']) {
      const condition = {
        mode: 'all' as const,
        rows: [{ uid: 1, key, op, value: 'invalid' }],
        custom: '',
      };
      expect(conditionProblem('step', condition, types)).not.toBe('');
    }
  }
});

test('describes stored rules', () => {
  const saved = {
    any: [
      { 'media.video.count': { gt: 0 } },
      { enabled: false },
      { codecs: ['hevc', 'h264'] },
      { languages: [] },
    ],
  };
  const condition = conditionFromWhen(saved);
  expect(condition.mode).toBe('any');
  expect(conditionToWhen(condition)).toEqual(saved);
});

test('roundtrips entry conditions', () => {
  const saved = {
    all: [
      { any: [{ 'media.audio.count': { gt: 0 } }, { enabled: true }] },
      { codecs: ['aac', 'flac'] },
    ],
  };
  const condition = conditionFromWhen(saved);
  expect(condition.mode).toBe('custom');
  expect(conditionProblem('condition', condition, types)).toBe('');
  expect(conditionToWhen(condition)).toEqual(saved);
  expect(conditionToWhen(emptyCondition())).toBeNull();
  const audio = conditionFromWhen({ 'media.audio.count': { gt: 0 } });
  audio.rows[0]!.value = '1';
  expect(conditionProblem('Start', audio)).toBe('');
  expect(conditionToWhen(audio)).toEqual({ 'media.audio.count': { gt: 1 } });
});

test('validates custom JSON', () => {
  for (const custom of ['{', '', '{"all": [}']) {
    expect(conditionProblem('node "gate"', { mode: 'custom', rows: [], custom })).not.toBe('');
  }
  for (const custom of ['null', '[]', 'true', '1', '"codec"']) {
    expect(conditionProblem('node "gate"', { mode: 'custom', rows: [], custom })).not.toBe('');
  }
});
