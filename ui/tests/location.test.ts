import { expect, test } from 'bun:test';
import { relativeDirectory } from '../app/utils/location';

test('Relative folders', () => {
  expect(relativeDirectory('/media/shows/Season 1/episode.mkv', ['/media/shows'])).toBe('Season 1');
  expect(
    relativeDirectory('/archive/shows/Season 2/episode.mkv', ['/media/shows', '/archive/shows/']),
  ).toBe('Season 2');
});

test('Root files', () => {
  expect(relativeDirectory('/media/shows/episode.mkv', ['/media/shows'])).toBe('');
});

test('Windows folders', () => {
  expect(relativeDirectory('D:\\Shows\\Season 1\\episode.mkv', ['D:\\Shows\\'])).toBe('Season 1');
});

test('Unmatched roots', () => {
  expect(relativeDirectory('/media/shows-other/episode.mkv', ['/media/shows'])).toBe('');
  expect(relativeDirectory('/media/shows/episode.mkv', [])).toBe('');
});
