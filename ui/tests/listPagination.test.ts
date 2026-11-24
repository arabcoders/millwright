import { expect, test } from 'bun:test';
import { nearbyPages } from '../app/utils/listPagination';

test('centers large lists', () => {
  expect(nearbyPages(7120, 16764)).toEqual([7117, 7118, 7119, 7120, 7121, 7122, 7123]);
});

test('fills both ends', () => {
  expect(nearbyPages(3, 21)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  expect(nearbyPages(19, 21)).toEqual([15, 16, 17, 18, 19, 20, 21]);
});

test('shows short lists', () => {
  expect(nearbyPages(3, 4)).toEqual([1, 2, 3, 4]);
});
