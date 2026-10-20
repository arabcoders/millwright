import { Marked, Renderer } from 'marked';
import markedAlert from 'marked-alert';
import { gfmHeadingId } from 'marked-gfm-heading-id';
import { resolveDocumentationImage, resolveDocumentationLink } from './documentation';

export const renderDocumentation = (
  markdown: string,
  source?: string,
  currentPath = '/docs',
): string => {
  const parser = new Marked({ gfm: true, async: false });
  parser.use(gfmHeadingId(), markedAlert(), {
    renderer: {
      link(token) {
        const href = resolveDocumentationLink(token.href, source, currentPath);
        const html = Renderer.prototype.link.call(this, { ...token, href });
        return /^(?:https?:)?\/\//i.test(href)
          ? html.replace('<a ', '<a target="_blank" rel="noopener noreferrer" ')
          : html;
      },
      image(token) {
        return Renderer.prototype.image
          .call(this, { ...token, href: resolveDocumentationImage(token.href, source) })
          .replace('<img ', '<img loading="lazy" ');
      },
      table(token) {
        return `<div class="docs-table">${Renderer.prototype.table.call(this, token)}</div>`;
      },
    },
  });
  return parser.parse(markdown, { async: false });
};
