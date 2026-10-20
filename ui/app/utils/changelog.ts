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

export const splitCommitMessage = (message: string): { title: string; description: string } => {
  const [title = '', ...lines] = message.split(/\r?\n/);
  return { title, description: lines.join('\n').replace(/^\n+|\n+$/g, '') };
};
