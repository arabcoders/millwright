import type { FacetFilter, FacetValue, WhenFacets } from '~/types/api';

export type ConditionOp = 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'contains' | 'not_contains';

export type ConditionMode = 'all' | 'any' | 'custom';

export type ConditionRow = {
  uid: number;
  key: string;
  op: ConditionOp;
  value: string | number;
  // Original literal from a stored condition. Re-emitted until the value is edited.
  literal?: FacetValue;
};

export type DraftCondition = {
  mode: ConditionMode;
  rows: ConditionRow[];
  // Raw JSON while mode is custom. Ignored otherwise.
  custom: string;
};

export const OPERATORS: { label: string; value: ConditionOp }[] = [
  { label: '==', value: 'eq' },
  { label: '!=', value: 'ne' },
  { label: '>', value: 'gt' },
  { label: '>=', value: 'gte' },
  { label: '<', value: 'lt' },
  { label: '<=', value: 'lte' },
  { label: 'contains', value: 'contains' },
  { label: 'does not contain', value: 'not_contains' },
];

export const ORDER_OPS: ReadonlySet<string> = new Set(['gt', 'gte', 'lt', 'lte']);
export const MEMBERSHIP_OPS: ReadonlySet<string> = new Set(['contains', 'not_contains']);
export const isNumericOperand = (row: ConditionRow, type?: string): boolean =>
  ORDER_OPS.has(row.op) ||
  (MEMBERSHIP_OPS.has(row.op) && type === 'list[int]') ||
  ((row.op === 'eq' || row.op === 'ne') && (type === 'int' || type === 'float'));
const FILTER_OPS: ReadonlySet<string> = new Set([
  'ne',
  'gt',
  'gte',
  'lt',
  'lte',
  'contains',
  'not_contains',
]);

let uidCounter = 0;

export const emptyCondition = (): DraftCondition => ({ mode: 'all', rows: [], custom: '' });

const FACT_LABELS: Record<string, string> = {
  'media.video.count': 'Video streams',
  'media.audio.count': 'Audio streams',
  'media.subtitle.count': 'Subtitle streams',
  'media.video.codec_family': 'Video codec',
  'media.file.size_bytes': 'File size',
  'media.video.bit_depth': 'Video bit depth',
  'media.video.bitrate_kbps': 'Video bitrate',
  'media.file.container': 'Container',
};
const OP_LABELS: Record<ConditionOp, string> = {
  eq: 'is',
  ne: 'is not',
  gt: '>',
  gte: '≥',
  lt: '<',
  lte: '≤',
  contains: 'contains',
  not_contains: 'does not contain',
};
export const readableFact = (key: string): string => {
  const label = FACT_LABELS[key] ?? key.replace(/^media\./, '').replace(/[._]/g, ' ');
  return label.charAt(0).toUpperCase() + label.slice(1);
};
const readableValue = (row: ConditionRow): string => {
  if (row.literal !== undefined && displayLiteral(row.literal) === String(row.value)) {
    if (typeof row.literal === 'boolean') return row.literal ? 'Yes' : 'No';
    if (Array.isArray(row.literal))
      return row.literal.length ? row.literal.join(', ') : 'Empty list';
  }
  if (typeof row.value !== 'number') return String(row.value);
  if (row.key.endsWith('_bytes')) {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let scaled = row.value;
    let unit = 0;
    while (Math.abs(scaled) >= 1024 && unit < units.length - 1) {
      scaled /= 1024;
      unit += 1;
    }
    return `${Number.isInteger(scaled) ? scaled : scaled.toFixed(1)} ${units[unit]}`;
  }
  if (row.key.endsWith('_kbps')) return `${row.value} kbps`;
  if (row.key.endsWith('_bit_depth')) return `${row.value}-bit`;
  return String(row.value);
};
export const conditionRule = (row: ConditionRow): string =>
  `${readableFact(row.key)} ${OP_LABELS[row.op]} ${readableValue(row)}`;

export const conditionSummary = (condition: DraftCondition): string => {
  if (condition.mode === 'custom') return 'Custom nested rule';
  if (!condition.rows.length) return 'Not configured';
  const rules = condition.rows.slice(0, 2).map(conditionRule);
  const more = condition.rows.length - rules.length;
  return `${condition.mode === 'all' ? 'All' : 'Any'}: ${rules.join('; ')}${more ? ` +${more} more` : ''}`;
};

const displayLiteral = (value: FacetValue): string =>
  Array.isArray(value) ? JSON.stringify(value) : String(value);

const isFacetLiteral = (value: unknown): value is FacetValue =>
  typeof value === 'string' ||
  typeof value === 'number' ||
  typeof value === 'boolean' ||
  (Array.isArray(value) &&
    (value.every((item) => typeof item === 'string') ||
      value.every((item) => typeof item === 'number' && Number.isInteger(item))));

const rowFromEntry = (key: string, value: unknown): ConditionRow | null => {
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    const filter = Object.entries(value);
    const opEntry = filter.length === 1 ? filter[0] : undefined;
    if (opEntry === undefined) return null;
    const [op, operand] = opEntry;
    if (op === undefined || !FILTER_OPS.has(op) || !isFacetLiteral(operand)) return null;
    return {
      uid: ++uidCounter,
      key,
      op: op as ConditionOp,
      value: typeof operand === 'number' ? operand : displayLiteral(operand),
      literal: operand,
    };
  }
  if (!isFacetLiteral(value)) return null;
  return { uid: ++uidCounter, key, op: 'eq', value: displayLiteral(value), literal: value };
};

// Accepts a flat dict or a single any/all group of single-key dicts. Anything
// else needs the custom JSON editor.
const parseWhen = (when: unknown): { mode: 'all' | 'any'; rows: ConditionRow[] } | null => {
  if (when === null || typeof when !== 'object' || Array.isArray(when)) return null;
  const entries = Object.entries(when);
  if (!entries.length) return { mode: 'all', rows: [] };
  const head = entries.length === 1 ? entries[0] : undefined;
  if (head !== undefined) {
    const [name, group] = head;
    if ((name === 'all' || name === 'any') && Array.isArray(group)) {
      const rows: ConditionRow[] = [];
      for (const entry of group) {
        if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) return null;
        const keys = Object.keys(entry);
        const key = keys.length === 1 ? keys[0] : undefined;
        if (key === undefined) return null;
        const row = rowFromEntry(key, (entry as Record<string, unknown>)[key]);
        if (row === null) return null;
        rows.push(row);
      }
      return { mode: name, rows };
    }
  }
  const rows: ConditionRow[] = [];
  for (const [key, value] of entries) {
    const row = rowFromEntry(key, value);
    if (row === null) return null;
    rows.push(row);
  }
  return { mode: 'all', rows };
};

export const conditionFromWhen = (when: unknown): DraftCondition => {
  if (when === null || when === undefined) return emptyCondition();
  const parsed = parseWhen(when);
  if (parsed === null) {
    return { mode: 'custom', rows: [], custom: JSON.stringify(when, null, 2) };
  }
  return { mode: parsed.mode, rows: parsed.rows, custom: '' };
};

export const addConditionRow = (condition: DraftCondition): void => {
  condition.rows.push({ uid: ++uidCounter, key: '', op: 'eq', value: '' });
};

// A stored literal is re-emitted until the user edits the displayed value,
// so untouched rows round-trip numbers, booleans, and lists exactly.
const conditionOperand = (
  row: ConditionRow,
  facetTypes: ReadonlyMap<string, string>,
): FacetValue => {
  if (row.literal !== undefined && displayLiteral(row.literal) === String(row.value)) {
    return row.literal;
  }
  const type = facetTypes.get(row.key);
  if (isNumericOperand(row, type)) return Number(row.value);
  if ((row.op === 'eq' || row.op === 'ne') && type === 'str') return String(row.value);
  if ((row.op === 'eq' || row.op === 'ne') && type === 'bool') return row.value === 'true';
  return row.value;
};

export const conditionToWhen = (
  condition: DraftCondition,
  facetTypes: ReadonlyMap<string, string> = new Map(),
): WhenFacets | null => {
  if (condition.mode === 'custom') return JSON.parse(condition.custom) as WhenFacets;
  if (!condition.rows.length) return null;
  const entry = (row: ConditionRow): Record<string, FacetValue | FacetFilter> => ({
    [row.key]:
      row.op === 'eq'
        ? conditionOperand(row, facetTypes)
        : { [row.op]: conditionOperand(row, facetTypes) },
  });
  if (condition.mode === 'any') return { any: condition.rows.map(entry) };
  if (new Set(condition.rows.map((row) => row.key)).size !== condition.rows.length) {
    return { all: condition.rows.map(entry) };
  }
  const flat: Record<string, FacetValue | FacetFilter> = {};
  for (const row of condition.rows) {
    flat[row.key] =
      row.op === 'eq'
        ? conditionOperand(row, facetTypes)
        : { [row.op]: conditionOperand(row, facetTypes) };
  }
  return flat;
};

export const conditionProblem = (
  subject: string,
  condition: DraftCondition,
  facetTypes: ReadonlyMap<string, string> = new Map(),
): string => {
  if (condition.mode === 'custom') {
    try {
      const parsed: unknown = JSON.parse(condition.custom);
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        return `Custom condition on ${subject} must be a JSON object.`;
      }
    } catch {
      return `Custom condition on ${subject} is not valid JSON.`;
    }
    return '';
  }
  for (const row of condition.rows) {
    if (!row.key.trim()) return 'Every condition row needs a facet key.';
    if (row.value === '') return `Condition on "${row.key}" needs a value.`;
    const type = facetTypes.get(row.key);
    if (isNumericOperand(row, type)) {
      if (!Number.isFinite(Number(row.value))) {
        return `Condition on "${row.key}" needs a numeric value.`;
      }
    }
    if (
      (row.op === 'eq' || row.op === 'ne') &&
      type === 'bool' &&
      row.value !== 'true' &&
      row.value !== 'false'
    ) {
      return `Condition on "${row.key}" needs true or false.`;
    }
  }
  return '';
};
