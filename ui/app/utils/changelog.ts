export type ChangelogCommit = {
  sha: string;
  full_sha: string;
  message: string;
  author: string;
  date: string;
};

export type ChangelogGroup = {
  tag: string;
  full_sha: string;
  date: string;
  commits: ChangelogCommit[];
};

export const loadChangelog = async (
  version: string,
  bundledUrl: string,
): Promise<ChangelogGroup[]> => {
  try {
    const response = await fetch(
      `https://arabcoders.github.io/millwright/CHANGELOG.json?version=${encodeURIComponent(version)}`,
    );
    if (!response.ok) {
      throw new Error(`Published commit history request failed (HTTP ${response.status}).`);
    }
    return (await response.json()) as ChangelogGroup[];
  } catch (cause) {
    const response = await fetch(bundledUrl);
    // A source build can serve the SPA entry point when no bundled history exists.
    if (
      response.status === 404 ||
      (response.ok && !response.headers.get('Content-Type')?.includes('application/json'))
    ) {
      throw cause;
    }
    if (!response.ok) {
      throw new Error(`Bundled commit history request failed (HTTP ${response.status}).`, {
        cause,
      });
    }
    return (await response.json()) as ChangelogGroup[];
  }
};

export const filterChangelog = (groups: ChangelogGroup[], query: string): ChangelogGroup[] => {
  const search = query.trim().toLowerCase();
  if (!search) return groups;

  return groups.flatMap((group) => {
    if (group.tag.toLowerCase().includes(search)) return [group];
    const commits = group.commits.filter((commit) =>
      [commit.message, commit.author, commit.full_sha].some((value) =>
        value.toLowerCase().includes(search),
      ),
    );
    return commits.length ? [{ ...group, commits }] : [];
  });
};

export const matchesSha = (value: string, installed: string): boolean => {
  const candidate = value.trim().toLowerCase();
  const current = installed.trim().toLowerCase();
  return (
    current !== '' &&
    current !== 'unknown' &&
    candidate !== '' &&
    candidate !== 'unknown' &&
    (candidate.startsWith(current) || current.startsWith(candidate))
  );
};

export const isInstalledGroup = (group: ChangelogGroup, installedSha: string): boolean =>
  matchesSha(group.full_sha, installedSha) ||
  group.commits.some(
    (commit) => matchesSha(commit.full_sha, installedSha) || matchesSha(commit.sha, installedSha),
  );

export const initialChangelogReleases = (
  groups: ChangelogGroup[],
  installedSha: string,
): string[] => {
  const installedIndex = groups.findIndex((group) => isInstalledGroup(group, installedSha));
  return groups.slice(0, Math.max(0, installedIndex) + 1).map((group) => group.tag);
};

export const formatCommitMessage = (message: string): { title: string; description: string[] } => {
  const lines = message
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const title = lines.shift() ?? '';
  return {
    title: `${(title.charAt(0).toUpperCase() + title.slice(1)).replace(/\.$/, '')}.`,
    description: lines,
  };
};
