import { parse_api_error, request, to_api_error } from '~/utils';
import type { DirectoryListing } from '~/types/api';

export type DirectoryLoadResult =
  | { ok: true; data: DirectoryListing }
  | { ok: false; status: number; error: string };

export const useDirectories = () => {
  const load = async (path: string): Promise<DirectoryLoadResult> => {
    try {
      const response = await request(`/api/directories?path=${encodeURIComponent(path)}`);
      if (!response.ok)
        return {
          ok: false,
          status: response.status,
          error: await parse_api_error(await response.json().catch(() => null)),
        };
      return { ok: true, data: (await response.json()) as DirectoryListing };
    } catch (cause) {
      return { ok: false, status: 0, error: to_api_error(cause).message };
    }
  };

  return { load };
};
