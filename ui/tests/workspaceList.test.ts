import { expect, test } from 'bun:test';
import { workspaceRouteFilters, updateWorkspaceRoute } from '../app/utils/workspaceList';

test('normalizes workspace query', () => {
  for (const limit of ['50', '100', '200', '300', '500', '750', '999']) {
    expect(workspaceRouteFilters({ limit })).toEqual(limit === '50' ? {} : { limit });
  }
  expect(
    workspaceRouteFilters({
      query: '  Movie  ',
      eligibility: 'unused',
      offset: '00050',
      limit: '999',
      sort_by: 'size_bytes',
      sort_dir: 'asc',
    }),
  ).toEqual({
    query: 'Movie',
    eligibility: 'unused',
    offset: '50',
    limit: '999',
    sort_by: 'size_bytes',
    sort_dir: 'asc',
  });
  expect(
    workspaceRouteFilters({
      query: ['movie'],
      eligibility: 'wrong',
      offset: '-1',
      limit: '201',
      sort_by: 'wrong',
      sort_dir: 'wrong',
    }),
  ).toEqual({});
  expect(workspaceRouteFilters({ eligibility: 'all', offset: '0', limit: '50' })).toEqual({});
});

test('filter changes reset pagination', () => {
  expect(
    updateWorkspaceRoute(
      { offset: '100', limit: '100', eligibility: 'unused', unrelated: 'keep' },
      { query: 'movie' },
    ),
  ).toEqual({ limit: '100', eligibility: 'unused', unrelated: 'keep', query: 'movie' });
  expect(updateWorkspaceRoute({ query: 'movie', limit: '999' }, { offset: '999' })).toEqual({
    query: 'movie',
    limit: '999',
    offset: '999',
  });
  expect(updateWorkspaceRoute({ offset: '100', limit: '100' }, { limit: '50' })).toEqual({});
});
