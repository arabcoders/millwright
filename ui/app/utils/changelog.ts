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

const RELEASE_LIMIT = 10;

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
  const visible = groups.slice(0, RELEASE_LIMIT);
  const search = query.trim().toLowerCase();
  if (!search) return visible;

  return visible.flatMap((group) => {
    if (group.tag.toLowerCase().includes(search)) return [group];
    const commits = group.commits.filter((commit) =>
      [commit.message, commit.author, commit.full_sha].some((value) =>
        value.toLowerCase().includes(search),
      ),
    );
    return commits.length ? [{ ...group, commits }] : [];
  });
};

export const isInstalledGroup = (group: ChangelogGroup, installedSha: string): boolean => {
  if (!installedSha) return false;
  return (
    group.full_sha.startsWith(installedSha) ||
    group.commits.some((commit) => commit.full_sha.startsWith(installedSha))
  );
};

const compareVersions = (left: string, right: string): number => {
  const leftParts = left
    .replace(/^v/i, '')
    .split('.')
    .map((part) => Number.parseInt(part, 10) || 0);
  const rightParts = right
    .replace(/^v/i, '')
    .split('.')
    .map((part) => Number.parseInt(part, 10) || 0);

  for (let index = 0; index < Math.max(leftParts.length, rightParts.length); index += 1) {
    const difference = (leftParts[index] ?? 0) - (rightParts[index] ?? 0);
    if (difference !== 0) return difference;
  }
  return 0;
};

export const initialChangelogReleases = (
  groups: ChangelogGroup[],
  installedVersion: string,
  installedSha: string,
): string[] => {
  const visible = groups.slice(0, RELEASE_LIMIT);
  const installedIndex = visible.findIndex((group) => isInstalledGroup(group, installedSha));
  return (
    installedIndex >= 0
      ? visible.slice(0, installedIndex + 1)
      : visible.filter((group) => compareVersions(group.tag, installedVersion) >= 0)
  ).map((group) => group.tag);
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
