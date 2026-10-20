import type { LocationQueryRaw } from 'vue-router';
import { LIST_PAGE_SIZES } from './listPagination';

export const TRASH_FILTER_ALL = '__all__';
export const trashSortOptions = [
  { label: 'Trashed', value: 'trashed_at' },
  { label: 'Purges', value: 'purge_at' },
  { label: 'File size', value: 'size_bytes' },
  { label: 'Name / Path', value: 'original_path' },
];
export const trashQueryKeys = ['query', 'library_id', 'offset', 'limit', 'sort_by', 'sort_dir'];

export const trashDirectory = (path: string): string => {
  const normalized = path.replaceAll('\\', '/');
  const parent = normalized.slice(0, normalized.lastIndexOf('/') + 1);
  const parts = parent.split('/').filter(Boolean);
  if (parts.length > 2) return `.../${parts.slice(-2).join('/')}`;
  return parent.replace(/\/$/, '') || '/';
};

const queryValue = (value: unknown): string | undefined =>
  typeof value === 'string' ? value : undefined;

export const trashRouteFilters = (source: Record<string, unknown>): Record<string, string> => {
  const values: Record<string, string> = {};
  const search = queryValue(source.query)?.trim();
  const library = queryValue(source.library_id);
  const offset = queryValue(source.offset);
  const limit = queryValue(source.limit);
  const sort = queryValue(source.sort_by);
  const direction = queryValue(source.sort_dir);
  if (search) values.query = search;
  if (library && /^[1-9]\d*$/.test(library)) values.library_id = library;
  if (offset && /^\d+$/.test(offset)) {
    const normalized = offset.replace(/^0+(?=\d)/, '');
    if (normalized !== '0') values.offset = normalized;
  }
  if (limit && LIST_PAGE_SIZES.some((size) => String(size) === limit) && limit !== '50')
    values.limit = limit;
  if (sort && trashSortOptions.some((option) => option.value === sort)) values.sort_by = sort;
  if (direction === 'asc' || direction === 'desc') values.sort_dir = direction;
  return values;
};

export const updateTrashRoute = (
  source: LocationQueryRaw,
  changes: Record<string, string>,
): LocationQueryRaw => {
  const next = { ...source, ...changes };
  if (trashQueryKeys.some((key) => key !== 'offset' && key in changes)) delete next.offset;
  return {
    ...Object.fromEntries(Object.entries(next).filter(([key]) => !trashQueryKeys.includes(key))),
    ...trashRouteFilters(next),
  };
};
