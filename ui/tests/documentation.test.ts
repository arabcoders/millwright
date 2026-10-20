import { expect, test } from 'bun:test';
import {
  getDocumentationEntry,
  resolveDocumentationImage,
  resolveDocumentationLink,
} from '../app/utils/documentation';
import { renderDocumentation } from '../app/utils/documentationMarkdown';
import { getActiveNavItem, getNavItems, getPageShell } from '../app/utils/topLevelNavigation';

test('guide links resolve', () => {
  expect(resolveDocumentationLink('../README.md#first-start', 'docs/README.md')).toBe(
    '/docs/overview#first-start',
  );
  expect(resolveDocumentationLink('authentication.md', 'docs/installation.md')).toBe(
    '/docs/authentication',
  );
  expect(resolveDocumentationLink('../SECURITY.md', 'docs/recovery.md')).toBe('/docs/security');
  expect(resolveDocumentationLink('#storage', 'docs/installation.md', '/docs/installation')).toBe(
    '/docs/installation#storage',
  );
  expect(resolveDocumentationLink('/api/docs', 'docs/usage.md')).toBe('/api/docs');
  expect(resolveDocumentationLink('https://example.org/guide#one', 'docs/usage.md')).toBe(
    'https://example.org/guide#one',
  );
  expect(resolveDocumentationLink('../compose.yml', 'docs/installation.md')).toBe(
    'https://github.com/arabcoders/millwright/blob/dev/compose.yml',
  );
});

test('guide routes select', () => {
  const entry = getDocumentationEntry(['installation']);
  expect(entry?.file).toBe('docs/installation.md');
  const active = getActiveNavItem({ path: '/docs/installation', hash: '#storage' });
  expect(active?.id).toBe(entry?.id);
  expect(getPageShell(active!.id)?.pageLabel).toBe(entry?.title);
  expect(getDocumentationEntry(['missing'])).toBeUndefined();
  const sidebar = getNavItems().filter((item) => item.sidebarVisible);
  expect(getActiveNavItem({ path: '/docs/usage' }, sidebar)?.id).toBe('docs-usage');
  expect(getActiveNavItem({ path: '/docs/installation', hash: '#storage' }, sidebar)?.id).toBe(
    'docs-index',
  );
  expect(getActiveNavItem({ path: '/docs/authentication' }, sidebar)?.id).toBe('docs-index');
  expect(getActiveNavItem({ path: '/changelog' }, sidebar)?.id).toBe('changelog');
});

test('markdown renders guides', () => {
  const markdown =
    '# First start\n\n## Same heading\n\n## Same heading\n\n> [!WARNING]\n> Keep backups.\n\n| Tool | Role |\n| --- | --- |\n| FFmpeg | Encode |\n\n[Sign in](authentication.md#configure-oidc)\n\n```text\n[Sign in](authentication.md)\n```';
  const html = renderDocumentation(markdown, 'docs/installation.md', '/docs/installation');
  expect(html).toContain('id="first-start"');
  expect(html).toContain('id="same-heading-1"');
  expect(html).toContain('markdown-alert-warning');
  expect(html).toContain('<div class="docs-table"><table>');
  expect(html).toContain('href="/docs/authentication#configure-oidc"');
  expect(html).toContain('[Sign in](authentication.md)');
  expect(renderDocumentation('# First start')).toContain('id="first-start"');
});

test('markdown preserves attributes', () => {
  const html = renderDocumentation(
    '[Example](https://example.org/?a=1&b=2 "A & B")\n\n<details>\n<summary>More</summary>\n\nExtra text.\n\n</details>',
  );
  expect(html).toContain('target="_blank" rel="noopener noreferrer"');
  expect(html).toContain('href="https://example.org/?a=1&amp;b=2"');
  expect(html).toContain('title="A &amp; B"');
  expect(html).toContain('<details>');
  expect(html).toContain('<summary>More</summary>');
  expect(resolveDocumentationImage('../ui/public/images/logo.png', 'docs/README.md')).toBe(
    '/images/logo.png',
  );
});
