export type DocumentationEntry = {
  id: string;
  title: string;
  description: string;
  file: string;
  route: string;
  icon: string;
  sidebarVisible?: boolean;
};

export const DOCUMENTATION: DocumentationEntry[] = [
  {
    id: 'docs-index',
    title: 'Documentation',
    description: 'Installation, operation, and references for Millwright.',
    file: 'docs/README.md',
    route: '/docs',
    icon: 'i-lucide-book-open',
    sidebarVisible: true,
  },
  {
    id: 'docs-overview',
    title: 'Overview',
    description: 'What Millwright does and how to get started.',
    file: 'README.md',
    route: '/docs/overview',
    icon: 'i-lucide-info',
  },
  {
    id: 'docs-installation',
    title: 'Installation',
    description: 'Deploy Millwright and configure storage, nodes, and hardware access.',
    file: 'docs/installation.md',
    route: '/docs/installation',
    icon: 'i-lucide-download',
  },
  {
    id: 'docs-authentication',
    title: 'Authentication',
    description: 'Create an account, configure OIDC, and change or reset a password.',
    file: 'docs/authentication.md',
    route: '/docs/authentication',
    icon: 'i-lucide-lock-keyhole',
  },
  {
    id: 'docs-usage',
    title: 'Using Millwright',
    description: 'Manage libraries, files, flows, and processing jobs.',
    file: 'docs/usage.md',
    route: '/docs/usage',
    icon: 'i-lucide-clapperboard',
    sidebarVisible: true,
  },
  {
    id: 'docs-recipes',
    title: 'Flow recipes',
    description: 'Build and preview common media processing workflows.',
    file: 'docs/recipes.md',
    route: '/docs/recipes',
    icon: 'i-lucide-workflow',
  },
  {
    id: 'docs-recovery',
    title: 'Recovery and troubleshooting',
    description: 'Retain originals, restore files, manage workspaces, and diagnose failures.',
    file: 'docs/recovery.md',
    route: '/docs/recovery',
    icon: 'i-lucide-archive-restore',
  },
  {
    id: 'docs-webhooks',
    title: 'Incoming webhooks',
    description: 'Connect Sonarr, Radarr, and other apps to library discovery.',
    file: 'docs/webhooks.md',
    route: '/docs/webhooks',
    icon: 'i-lucide-webhook',
  },
  {
    id: 'docs-plugins',
    title: 'Plugin authoring',
    description: 'Create custom flow steps.',
    file: 'docs/plugins.md',
    route: '/docs/plugins',
    icon: 'i-lucide-puzzle',
  },
  {
    id: 'docs-events',
    title: 'Live API updates',
    description: 'Receive job progress and file, library, and node updates.',
    file: 'docs/events.md',
    route: '/docs/events',
    icon: 'i-lucide-network',
  },
  {
    id: 'docs-contributing',
    title: 'Contributing',
    description: 'Contribution policy, source development, and verification.',
    file: 'CONTRIBUTING.md',
    route: '/docs/contributing',
    icon: 'i-lucide-git-pull-request',
  },
  {
    id: 'docs-development',
    title: 'Development guide',
    description: 'Container builds, local OIDC testing, migrations, and documentation publishing.',
    file: 'docs/development.md',
    route: '/docs/development',
    icon: 'i-lucide-code',
  },
  {
    id: 'docs-security',
    title: 'Security',
    description: 'Deployment boundaries and vulnerability reporting.',
    file: 'SECURITY.md',
    route: '/docs/security',
    icon: 'i-lucide-shield-check',
  },
];

const REPOSITORY = 'https://github.com/arabcoders/millwright/blob/dev/';
const SOURCE_ORIGIN = 'https://millwright.invalid';

export const getDocumentationEntry = (slug?: string | string[]): DocumentationEntry | undefined => {
  const path = (Array.isArray(slug) ? slug : slug ? [slug] : []).join('/');
  return DOCUMENTATION.find((entry) => entry.route === `/docs${path ? `/${path}` : ''}`);
};

export const resolveDocumentationLink = (
  href: string,
  source = 'README.md',
  currentPath = '/docs',
): string => {
  if (!href) return href;
  if (href.startsWith('#')) return `${currentPath}${href}`;
  if (/^(?:[a-z][a-z\d+.-]*:|\/)/i.test(href)) return href;
  const url = new URL(href, `${SOURCE_ORIGIN}/${source}`);
  const entry = DOCUMENTATION.find((item) => `/${item.file}` === url.pathname);
  if (entry) return `${entry.route}${url.search}${url.hash}`;
  return `${REPOSITORY}${url.pathname.slice(1)}${url.search}${url.hash}`;
};

export const resolveDocumentationImage = (href: string, source = 'README.md'): string => {
  if (/^(?:[a-z][a-z\d+.-]*:|\/)/i.test(href)) return href;
  const url = new URL(href, `${SOURCE_ORIGIN}/${source}`);
  if (url.pathname.startsWith('/ui/public/')) {
    return `${url.pathname.slice('/ui/public'.length)}${url.search}${url.hash}`;
  }
  return `/api/docs${url.pathname}${url.search}${url.hash}`;
};
