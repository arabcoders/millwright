import { describe, expect, spyOn, test } from 'bun:test';
import {
  filterChangelog,
  formatCommitMessage,
  initialChangelogReleases,
  isInstalledGroup,
  loadChangelog,
  matchesSha,
  type ChangelogGroup,
} from '../app/utils/changelog';

const groups: ChangelogGroup[] = [
  {
    tag: 'Unreleased',
    full_sha: 'aabbccdd11223344',
    date: '2026-01-03T12:00:00+00:00',
    commits: [
      {
        sha: 'aabbccdd',
        full_sha: 'aabbccdd11223344',
        message: 'fix: playback\n\nKeep subtitle indentation.',
        author: 'Sam',
        date: '2026-01-03T12:00:00+00:00',
      },
      {
        sha: '11223344',
        full_sha: '11223344aabbccdd',
        message: 'feat: add libraries',
        author: 'Morgan',
        date: '2026-01-02T12:00:00+00:00',
      },
    ],
  },
  {
    tag: 'v0.1.0-alpha.1',
    full_sha: 'abcdef0123456789',
    date: '2026-01-01T12:00:00+00:00',
    commits: [
      {
        sha: 'abcdef01',
        full_sha: 'abcdef0123456789',
        message: 'feat: initial media support',
        author: 'Morgan',
        date: '2026-01-01T12:00:00+00:00',
      },
    ],
  },
];

test('prefers published history', async () => {
  const urls: string[] = [];
  const fetch = spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
    urls.push(String(url));
    return Response.json(groups);
  });
  try {
    expect(await loadChangelog('dev/example build', '/instance/CHANGELOG.json')).toEqual(groups);
    expect(urls).toEqual([
      'https://arabcoders.github.io/millwright/CHANGELOG.json?version=dev%2Fexample%20build',
    ]);
  } finally {
    fetch.mockRestore();
  }
});

for (const [reason, published] of [
  ['HTTP', async () => Response.json([], { status: 503 })],
  ['network', async () => Promise.reject(new TypeError('Network unavailable.'))],
  ['JSON', async () => new Response('Invalid JSON')],
] as const) {
  test(`fallback handles ${reason}`, async () => {
    const urls: string[] = [];
    const fetch = spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      urls.push(String(url));
      return urls.length === 1 ? published() : Response.json(groups);
    });
    try {
      expect(await loadChangelog('dev', '/instance/CHANGELOG.json')).toEqual(groups);
      expect(urls).toEqual([
        'https://arabcoders.github.io/millwright/CHANGELOG.json?version=dev',
        '/instance/CHANGELOG.json',
      ]);
    } finally {
      fetch.mockRestore();
    }
  });
}

test('reports missing history', async () => {
  const fetch = spyOn(globalThis, 'fetch').mockImplementation(async (url) =>
    String(url).startsWith('https:')
      ? new Response(null, { status: 503 })
      : new Response('<html>Millwright</html>', { headers: { 'Content-Type': 'text/html' } }),
  );
  try {
    await expect(loadChangelog('dev', '/CHANGELOG.json')).rejects.toThrow(
      'Published commit history request failed (HTTP 503).',
    );
  } finally {
    fetch.mockRestore();
  }
});

describe('commit history filtering', () => {
  test('matches commit fields', () => {
    for (const query of [' PLAYBACK ', 'SUBTITLE', 'sam', 'CCDD1122']) {
      const result = filterChangelog(groups, query);
      expect(result.map((group) => group.commits.map((commit) => commit.sha))).toEqual([
        ['aabbccdd'],
      ]);
    }
    expect(filterChangelog(groups, 'morgan').map((group) => group.commits.length)).toEqual([1, 1]);
    expect(filterChangelog(groups, 'missing')).toEqual([]);
    expect(groups[0]!.commits.length).toBe(2);
  });

  test('matches release tags', () => {
    expect(filterChangelog(groups, 'UNRELEASED')).toEqual([groups[0]!]);
    expect(filterChangelog(groups, 'alpha.1')).toEqual([groups[1]!]);
    expect(filterChangelog(groups, '  ')).toEqual(groups);
  });

  test('searches older releases', () => {
    const history = Array.from({ length: 11 }, (_, index) => ({
      ...groups[0]!,
      tag: `v0.1.${10 - index}`,
    }));
    expect(filterChangelog(history, '')).toEqual(history);
    expect(filterChangelog(history, 'v0.1.0')).toEqual([history.at(-1)!]);
  });
});

test('opens installed releases', () => {
  expect(initialChangelogReleases(groups, '11223344')).toEqual(['Unreleased']);
  expect(initialChangelogReleases(groups, 'abcdef')).toEqual(['Unreleased', 'v0.1.0-alpha.1']);
});

test('matches shortened hashes', () => {
  expect(matchesSha(' AABBCCDD ', 'aabbccdd11223344')).toBe(true);
  expect(matchesSha('aabbccdd11223344', ' AABBCCDD ')).toBe(true);
  expect(matchesSha('aabbccdd11223344', '11223344')).toBe(false);
  expect(isInstalledGroup({ ...groups[1]!, commits: [] }, ' ABCDEF01 ')).toBe(true);
});

test('opens containing release', () => {
  const release = {
    ...groups[1]!,
    full_sha: '9988776655443322',
  };
  expect(isInstalledGroup(release, ' ABCDEF0123456789 ')).toBe(false);
  expect(initialChangelogReleases([groups[0]!, release], ' ABCDEF0123456789 ')).toEqual([
    'Unreleased',
    'v0.1.0-alpha.1',
  ]);
});

test('excludes unreleased heading', () => {
  expect(isInstalledGroup(groups[0]!, 'aabbccdd')).toBe(false);
});

test('opens older matches', () => {
  const history = [
    ...Array.from({ length: 10 }, (_, index) => ({ ...groups[0]!, tag: `build-${index}` })),
    groups[1]!,
  ];
  expect(initialChangelogReleases(history, 'abcdef01')).toEqual(history.map((group) => group.tag));
});

test('opens newest unmatched', () => {
  expect(initialChangelogReleases(groups, '99887766')).toEqual(['Unreleased']);
  expect(initialChangelogReleases([], '99887766')).toEqual([]);
});

test('rejects unavailable hashes', () => {
  for (const unavailable of ['', '  ', ' UnKnOwN ']) {
    expect(matchesSha(unavailable, 'aabbccdd')).toBe(false);
    expect(matchesSha('aabbccdd', unavailable)).toBe(false);
    expect(matchesSha(unavailable, unavailable)).toBe(false);
    expect(isInstalledGroup(groups[1]!, unavailable)).toBe(false);
    expect(initialChangelogReleases(groups, unavailable)).toEqual(['Unreleased']);
  }
});

test('formats commit messages', () => {
  expect(
    formatCommitMessage(
      '\r\nfix: preserve text.\r\n\r\n    indented code\r\n\r\n- First\r\n  - Nested\r\n\r\n```\r\n  code\r\n```\r\n',
    ),
  ).toEqual({
    title: 'Fix: preserve text.',
    description: ['indented code', '- First', '- Nested', '```', 'code', '```'],
  });
  expect(formatCommitMessage('feat: add libraries')).toEqual({
    title: 'Feat: add libraries.',
    description: [],
  });
});
