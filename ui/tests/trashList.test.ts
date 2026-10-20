import { expect, test } from 'bun:test';
import {
  TRASH_FILTER_ALL,
  trashDirectory,
  trashRouteFilters,
  trashSortOptions,
  updateTrashRoute,
} from '../app/utils/trashList';

test('trash directory shortens', () => {
  expect(trashDirectory('/fixtures/archive/data/library/test-videos/movie.mkv')).toBe(
    '.../library/test-videos',
  );
  expect(trashDirectory('/media/movie.mkv')).toBe('/media');
  expect(trashDirectory('/movie.mkv')).toBe('/');
  expect(trashDirectory('C:\\archive\\library\\movies\\movie.mkv')).toBe('.../library/movies');
});

test('trash defaults normalize', () => {
  expect(trashRouteFilters({})).toEqual({});
  expect(
    trashRouteFilters({ query: '  ', library_id: TRASH_FILTER_ALL, offset: '000', limit: '50' }),
  ).toEqual({});
  expect(
    updateTrashRoute({ library_id: '2', offset: '50' }, { library_id: TRASH_FILTER_ALL }),
  ).toEqual({});
});

test('trash preserves choices', () => {
  const choices = {
    query: ' movie ',
    library_id: '2',
    offset: '000100',
    limit: '999',
    sort_by: 'purge_at',
    sort_dir: 'asc',
  };
  expect(trashRouteFilters(choices)).toEqual({ ...choices, query: 'movie', offset: '100' });
  for (const { value } of trashSortOptions) {
    expect(trashRouteFilters({ sort_by: value })).toEqual({ sort_by: value });
  }
  for (const limit of ['50', '100', '200', '300', '500', '750', '999']) {
    expect(trashRouteFilters({ limit })).toEqual(limit === '50' ? {} : { limit });
  }
});

test('trash rejects invalid', () => {
  expect(
    trashRouteFilters({
      query: ['movie'],
      library_id: '-2',
      offset: '-1',
      limit: '400',
      sort_by: 'unknown',
      sort_dir: 'unknown',
    }),
  ).toEqual({});
  for (const library_id of ['0', '02', 'invalid', ['2']]) {
    expect(trashRouteFilters({ library_id })).toEqual({});
  }
  expect(trashRouteFilters({ limit: '1000' })).toEqual({});
});

test('trash filters reset', () => {
  for (const changes of [
    { query: 'movie' },
    { library_id: '2' },
    { limit: '100' },
    { limit: '999' },
    { sort_by: 'original_path' },
    { sort_dir: 'asc' },
  ]) {
    expect(updateTrashRoute({ offset: '50', extra: 'keep' }, changes)).toEqual({
      ...changes,
      extra: 'keep',
    });
  }
  expect(
    updateTrashRoute(
      { query: 'movie', library_id: '2', sort_by: 'size_bytes', sort_dir: 'asc', limit: '999' },
      { offset: '999' },
    ),
  ).toEqual({
    query: 'movie',
    library_id: '2',
    sort_by: 'size_bytes',
    sort_dir: 'asc',
    limit: '999',
    offset: '999',
  });
});
