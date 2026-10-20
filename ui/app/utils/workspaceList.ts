import type { LocationQueryRaw } from 'vue-router';
import { LIST_PAGE_SIZES } from './listPagination';

export const workspaceSortOptions = [
  { label: 'Workspace', value: 'workspace_id' },
  { label: 'Filename', value: 'filename' },
  { label: 'Status', value: 'status' },
  { label: 'Node', value: 'node_name' },
  { label: 'Size used', value: 'size_bytes' },
  { label: 'Last activity', value: 'last_activity_at' },
];
export const workspaceQueryKeys = [
  'query',
  'eligibility',
  'offset',
  'limit',
  'sort_by',
  'sort_dir',
];

export const workspaceRouteFilters = (source: Record<string, unknown>): Record<string, string> => {
  const values: Record<string, string> = {};
  const { query, eligibility, offset, limit, sort_by: sort, sort_dir: direction } = source;
  if (typeof query === 'string' && query.trim()) values.query = query.trim();
  if (eligibility === 'unused' || eligibility === 'protected') values.eligibility = eligibility;
  if (typeof offset === 'string' && /^\d+$/.test(offset)) {
    const normalized = offset.replace(/^0+(?=\d)/, '');
    if (normalized !== '0') values.offset = normalized;
  }
  if (
    typeof limit === 'string' &&
    LIST_PAGE_SIZES.some((size) => String(size) === limit) &&
    limit !== '50'
  )
    values.limit = limit;
  if (typeof sort === 'string' && workspaceSortOptions.some((option) => option.value === sort))
    values.sort_by = sort;
  if (direction === 'asc' || direction === 'desc') values.sort_dir = direction;
  return values;
};

export const updateWorkspaceRoute = (
  source: LocationQueryRaw,
  changes: Record<string, string>,
): LocationQueryRaw => {
  const next = { ...source, ...changes };
  if (workspaceQueryKeys.some((key) => key !== 'offset' && key in changes)) delete next.offset;
  return {
    ...Object.fromEntries(
      Object.entries(next).filter(([key]) => !workspaceQueryKeys.includes(key)),
    ),
    ...workspaceRouteFilters(next),
  };
};
