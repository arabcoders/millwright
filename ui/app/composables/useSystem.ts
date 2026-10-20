import { request } from '~/utils';
import type { SystemInfo } from '~/types/api';
import { configureDates } from '~/utils/date';

export const useSystem = () => {
  const load = async (): Promise<SystemInfo | null> => {
    try {
      const response = await request('/api/system');
      if (!response.ok) return null;
      const info = (await response.json()) as SystemInfo;
      configureDates(info.timezone);
      return info;
    } catch {
      return null;
    }
  };

  return { load };
};
