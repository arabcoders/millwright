import { parse_api_error, request } from '~/utils';
import type { BackupPreview } from '~/types/api';

type Outcome<T> =
  | { ok: true; data: T }
  | { ok: false; status: number | null; reason: 'http' | 'network' | 'decode'; detail?: string };

const failure = async (response: Response): Promise<Outcome<never>> => {
  const payload = await response.json().catch(() => null);
  return {
    ok: false,
    status: response.status,
    reason: 'http',
    detail: payload ? await parse_api_error(payload) : undefined,
  };
};

export const useBackup = () => {
  const reset = async (keep: string[]): Promise<Outcome<null>> => {
    try {
      const response = await request('/api/backups/reset', {
        method: 'POST',
        body: JSON.stringify({ keep }),
      });
      if (!response.ok) return await failure(response);
      try {
        const data = await response.json();
        if (data?.reset !== true) throw new Error('Invalid reset response.');
        return { ok: true, data: null };
      } catch {
        return { ok: false, status: null, reason: 'decode' };
      }
    } catch {
      return { ok: false, status: null, reason: 'network' };
    }
  };

  const exportBackup = async (
    password: string,
  ): Promise<Outcome<{ blob: Blob; filename: string }>> => {
    try {
      const response = await request('/api/backups/export', {
        method: 'POST',
        body: JSON.stringify({ password }),
      });
      if (!response.ok) return await failure(response);
      return { ok: true, data: { blob: await response.blob(), filename: 'millwright.mwbackup' } };
    } catch {
      return { ok: false, status: null, reason: 'network' };
    }
  };

  const preview = async (file: File, password: string): Promise<Outcome<BackupPreview>> => {
    try {
      const body = new FormData();
      body.append('file', file);
      body.append('password', password);
      const response = await request('/api/backups/preview', { method: 'POST', body });
      if (!response.ok) return await failure(response);
      try {
        const data = await response.json();
        if (
          !data ||
          !Array.isArray(data.flows) ||
          !Array.isArray(data.libraries) ||
          !Array.isArray(data.notifications) ||
          !Array.isArray(data.webhooks) ||
          !Array.isArray(data.plugins) ||
          !Array.isArray(data.nodes) ||
          typeof data.config !== 'boolean' ||
          typeof data.settings !== 'boolean'
        )
          throw new Error('Invalid backup preview.');
        return { ok: true, data: data as BackupPreview };
      } catch {
        return { ok: false, status: null, reason: 'decode' };
      }
    } catch {
      return { ok: false, status: null, reason: 'network' };
    }
  };

  const restore = async (
    file: File,
    password: string,
    selection: {
      flows: string[];
      libraries: string[];
      notifications: string[];
      webhooks: string[];
      plugins: string[];
      nodes: string[];
      config: boolean;
      settings: boolean;
      replace: boolean;
      roots: Record<string, string[]>;
    },
  ): Promise<Outcome<{ restartRequired: boolean }>> => {
    try {
      const body = new FormData();
      body.append('file', file);
      body.append('password', password);
      body.append('selection', JSON.stringify(selection));
      const response = await request('/api/backups/restore', { method: 'POST', body });
      if (!response.ok) return await failure(response);
      try {
        const result = await response.json();
        if (typeof result?.restart_required !== 'boolean')
          throw new Error('Invalid restore response.');
        return { ok: true, data: { restartRequired: result.restart_required } };
      } catch {
        return { ok: false, status: null, reason: 'decode' };
      }
    } catch {
      return { ok: false, status: null, reason: 'network' };
    }
  };

  return { reset, exportBackup, preview, restore };
};
