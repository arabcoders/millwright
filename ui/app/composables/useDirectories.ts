import { request } from '~/utils';
import type { DirectoryListing } from '~/types/api';

export type DirectoryLoadResult =
  | { ok: true; data: DirectoryListing }
  | { ok: false; status?: number };

export const useDirectories = () => {
  const load = async (path: string): Promise<DirectoryLoadResult> => {
    try {
      const response = await request(`/api/directories?path=${encodeURIComponent(path)}`);
      if (!response.ok) return { ok: false, status: response.status };
      return { ok: true, data: (await response.json()) as DirectoryListing };
    } catch {
      return { ok: false, status: 0 };
    }
  };

  return { load };
};
