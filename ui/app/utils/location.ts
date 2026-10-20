import type { LibraryRow } from '~/types/api';

export const relativeDirectory = (path: string, roots: string[]): string => {
  const normalizedPath = path.replaceAll('\\', '/');
  const root = roots
    .map((candidate) => candidate.replaceAll('\\', '/').replace(/\/+$/, ''))
    .find(
      (candidate) => normalizedPath === candidate || normalizedPath.startsWith(`${candidate}/`),
    );
  const parent = normalizedPath.split('/').slice(0, -1).join('/') || '/';
  return root ? parent.slice(root.length).replace(/^\/+/, '') : '';
};

export const libraryLocation = (
  path: string,
  libraryId: number,
  libraries: LibraryRow[],
): string => {
  const library = libraries.find((lib) => lib.id === libraryId);
  const relative = relativeDirectory(path, library?.roots ?? []);
  return [library?.name ?? `Library #${libraryId}`, relative].filter(Boolean).join('/');
};
