import { DOCUMENTATION } from './documentation';

export type SectionId = 'overview' | 'media' | 'processing' | 'configuration' | 'documentation';

type NavSection = {
  id: SectionId;
  label: string;
};

type NavDefinition = {
  id: string;
  section: SectionId;
  group: string;
  label: string;
  description: string;
  icon: string;
  to: string;
  matchPath?: string;
  sidebarVisible?: boolean;
};

export type NavItem = NavDefinition & {
  sectionLabel: string;
  pageLabel: string;
  matchPath: string;
  sidebarVisible: boolean;
};

export type PageShell = {
  icon: string;
  sectionLabel: string;
  pageLabel: string;
  description: string;
};

type LocationPath = {
  path: string;
  hash?: string;
};

const SECTIONS: Array<NavSection> = [
  { id: 'overview', label: 'Overview' },
  { id: 'media', label: 'Media' },
  { id: 'processing', label: 'Processing' },
  { id: 'configuration', label: 'Configuration' },
  { id: 'documentation', label: 'Documentation' },
];

const NavItems: Array<NavDefinition> = [
  {
    id: 'dashboard',
    section: 'overview',
    group: 'overview',
    label: 'Dashboard',
    description: 'Current server activity.',
    icon: 'i-lucide-layout-dashboard',
    to: '/',
    matchPath: '/',
  },
  {
    id: 'stats',
    section: 'overview',
    group: 'overview',
    label: 'Statistics',
    description: 'Disk space saved by transcodes, by day and overall.',
    icon: 'i-lucide-chart-column',
    to: '/stats',
    matchPath: '/stats',
  },
  {
    id: 'files',
    section: 'media',
    group: 'media',
    label: 'Files',
    description: 'Scanned media, health findings, and transcode status.',
    icon: 'i-lucide-folder-tree',
    to: '/files',
    matchPath: '/files',
  },
  {
    id: 'flows',
    section: 'processing',
    group: 'processing',
    label: 'Flows',
    description: 'Steps and conditions that decide how files are processed.',
    icon: 'i-lucide-workflow',
    to: '/flows',
    matchPath: '/flows',
  },
  {
    id: 'jobs',
    section: 'processing',
    group: 'processing',
    label: 'Jobs',
    description: 'Track probes, MKV statistics, health checks, and transcodes from queue to done.',
    icon: 'i-lucide-list-todo',
    to: '/jobs',
    matchPath: '/jobs',
  },
  {
    id: 'nodes',
    section: 'processing',
    group: 'processing',
    label: 'Nodes',
    description: 'Workers, limits, and encoders.',
    icon: 'i-lucide-server',
    to: '/nodes',
    matchPath: '/nodes',
  },
  {
    id: 'workspaces',
    section: 'processing',
    group: 'processing',
    label: 'Workspaces',
    description: 'Inspect job workspaces and remove unused temporary files.',
    icon: 'i-lucide-folder-tree',
    to: '/workspaces',
    matchPath: '/workspaces',
  },
  {
    id: 'libraries',
    section: 'media',
    group: 'media',
    label: 'Libraries',
    description: 'Folders scanned for media.',
    icon: 'i-lucide-folders',
    to: '/libraries',
    matchPath: '/libraries',
  },
  {
    id: 'trash',
    section: 'media',
    group: 'media',
    label: 'Trash',
    description: 'Browse trash and restore files to their original paths.',
    icon: 'i-lucide-trash-2',
    to: '/trash',
    matchPath: '/trash',
  },
  {
    id: 'settings',
    section: 'configuration',
    group: 'configuration',
    label: 'Settings',
    description: 'Processing, scanning, and playback.',
    icon: 'i-lucide-settings',
    to: '/settings',
  },
  {
    id: 'plugins',
    section: 'configuration',
    group: 'configuration',
    label: 'Plugins',
    description: 'Processing steps for flows.',
    icon: 'i-lucide-puzzle',
    to: '/plugins',
    matchPath: '/plugins',
  },
  {
    id: 'notifications',
    section: 'configuration',
    group: 'configuration',
    label: 'Notifications',
    description: 'Delivery targets for job notifications.',
    icon: 'i-lucide-bell',
    to: '/notifications',
    matchPath: '/notifications',
  },
  {
    id: 'webhooks',
    section: 'configuration',
    group: 'configuration',
    label: 'Incoming webhooks',
    description:
      'Receive file and folder updates from other apps and scan the affected library paths.',
    icon: 'i-lucide-webhook',
    to: '/webhooks',
  },
  {
    id: 'logs',
    section: 'configuration',
    group: 'configuration',
    label: 'Logs',
    description: 'Browse server logs.',
    icon: 'i-lucide-scroll-text',
    to: '/logs',
    matchPath: '/logs',
  },
  {
    id: 'system-operations',
    section: 'configuration',
    group: 'configuration',
    label: 'System operations',
    description: 'Export or restore configuration backups and reset server data.',
    icon: 'i-lucide-hard-drive-download',
    to: '/system-operations',
    matchPath: '/system-operations',
  },
  ...DOCUMENTATION.map((entry) => ({
    id: entry.id,
    section: 'documentation' as const,
    group: 'documentation',
    label: entry.title,
    description: entry.description,
    icon: entry.icon,
    to: entry.route,
    sidebarVisible: entry.sidebarVisible ?? false,
  })),
  {
    id: 'changelog',
    section: 'documentation',
    group: 'documentation',
    label: 'Changelog',
    description: 'Commit history grouped by release, with the installed build marked.',
    icon: 'i-lucide-history',
    to: '/changelog',
  },
];

const normalizePath = (value?: string | null): string => {
  if (!value || value === '/') {
    return '/';
  }

  const trimmed = value.replace(/\/+$/, '');
  return trimmed === '' ? '/' : trimmed;
};

const getSectionLabel = (sectionId: SectionId): string => {
  const section = SECTIONS.find((item) => item.id === sectionId);
  return section?.label ?? sectionId;
};

const resolveEntry = (entry: NavDefinition): NavItem => ({
  ...entry,
  sidebarVisible: entry.sidebarVisible ?? true,
  sectionLabel: getSectionLabel(entry.section),
  pageLabel: entry.label,
  matchPath: normalizePath(entry.matchPath ?? (entry.to.split(/[?#]/)[0] || '/')),
});

const resolvedNavigation = NavItems.map((entry) => resolveEntry(entry));

const isPathActive = (path: string, route: LocationPath): boolean => {
  const current = normalizePath(route.path);
  const target = normalizePath(path);

  if (target === '/') {
    return current === '/';
  }

  return current === target || current.startsWith(`${target}/`);
};

export const getNavItems = (): Array<NavItem> => resolvedNavigation;

export const getNavSections = (): Array<NavSection> => SECTIONS;

export const getNavItemById = (id: string): NavItem | undefined =>
  resolvedNavigation.find((entry) => entry.id === id);

export const getActiveNavItem = (
  route: LocationPath,
  items: Array<NavItem> = getNavItems(),
): NavItem | undefined =>
  items
    .filter((entry) => isPathActive(entry.matchPath, route))
    .sort((left, right) => right.matchPath.length - left.matchPath.length)[0];

export const getPageShell = (id: string): PageShell | undefined => {
  const entry = getNavItemById(id);
  if (!entry) {
    return undefined;
  }

  return {
    icon: entry.icon,
    sectionLabel: entry.sectionLabel,
    pageLabel: entry.pageLabel,
    description: entry.description,
  };
};

export const requirePageShell = (id: string): PageShell => {
  const shell = getPageShell(id);

  if (!shell) {
    throw new Error(`Missing top-level navigation shell for '${id}'`);
  }

  return shell;
};
