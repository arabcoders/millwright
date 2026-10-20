import { computed, ref } from 'vue';
import { ensure_api_success, request, to_api_error } from '~/utils';
import { diagnostic } from '~/utils/diagnostic';

export type OidcLoginResponse =
  | { ok: true; authorize_url: string }
  | { ok: false; status: number; code?: string; detail?: string; cause: unknown };

export type MeResponse = {
  id: number | null;
  username: string;
  auth_method: 'session' | 'api_key';
};

export type AuthStatus = {
  setup_required: boolean;
  authenticated: boolean;
  user: { id: number; username: string } | null;
  oidc_available: boolean;
};

export const AUTH_CHANGE_KEY = 'mw_auth_change';
const status = ref<MeResponse | null>(null);
let generation = 0;
export const getAuthGeneration = (): number => generation;
const lastError = ref<string | null>(null);
let probePromise: Promise<MeResponse | null> | null = null;
let probeVersion = 0;

export const invalidateAuth = (): void => {
  generation += 1;
  probeVersion += 1;
  probePromise = null;
  status.value = null;
  lastError.value = null;
};

const announceChange = (): void => {
  if (import.meta.client) localStorage.setItem(AUTH_CHANGE_KEY, String(Math.random()));
};

export const useAuth = () => {
  // Only a 401 clears cached sign-in state. Server and network errors leave it unchanged.
  const probe = async (force: boolean = false): Promise<MeResponse | null> => {
    if (!force && status.value) {
      return status.value;
    }

    if (!force && probePromise) {
      return probePromise;
    }

    const version = ++probeVersion;
    const currentResult = (): MeResponse | null | Promise<MeResponse | null> =>
      probePromise && probePromise !== operation ? probePromise : status.value;
    const operation = (async (): Promise<MeResponse | null> => {
      try {
        const response = await request('/api/auth/me');
        if (response.status === 401) {
          if (version === probeVersion) {
            invalidateAuth();
            return null;
          }
          return currentResult();
        }
        await ensure_api_success(response);
        const value = (await response.json()) as MeResponse;
        if (version !== probeVersion) return currentResult();
        status.value = value;
        lastError.value = null;
        return value;
      } catch (error) {
        diagnostic('debug', { event: 'auth.probe_unavailable', component: 'auth' });
        if (version !== probeVersion) return currentResult();
        lastError.value = 'Couldn’t verify your sign-in. Try again.';
        throw error;
      }
    })();
    probePromise = operation;

    try {
      return await operation;
    } finally {
      if (probePromise === operation) {
        probePromise = null;
      }
    }
  };

  const logout = async (): Promise<void> => {
    const version = generation;
    const response = await request('/api/auth/logout', { method: 'POST' });
    if (response.status !== 401) await ensure_api_success(response);
    if (generation === version) {
      invalidateAuth();
      announceChange();
    }
  };

  const checkStatus = async (): Promise<AuthStatus> => {
    const response = await request('/api/auth/status');
    await ensure_api_success(response);
    return response.json();
  };

  const login = async (username: string, password: string, setup = false): Promise<void> => {
    const response = await request(setup ? '/api/auth/setup' : '/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    await ensure_api_success(response);
    const me = (await response.json()) as MeResponse;
    invalidateAuth();
    status.value = me;
    announceChange();
  };

  const refreshSession = async (): Promise<MeResponse | null> => {
    // Another tab changed the cookie. Ignore pending responses but retain drafts on outages.
    generation += 1;
    probeVersion += 1;
    probePromise = null;
    return probe(true);
  };

  const changePassword = async (currentPassword: string, password: string): Promise<void> => {
    const response = await request('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, password }),
    });
    await ensure_api_success(response);
    const me = (await response.json()) as MeResponse;
    invalidateAuth();
    status.value = me;
    announceChange();
  };

  const oidcLogin = async (): Promise<OidcLoginResponse> => {
    try {
      const response = await request('/api/auth/oidc/login');
      await ensure_api_success(response);
      const { authorize_url } = (await response.json()) as { authorize_url: string };
      return { ok: true, authorize_url };
    } catch (err) {
      const error = to_api_error(err);
      return {
        ok: false,
        status: error.status,
        code: error.payload?.code,
        detail: typeof error.payload?.detail === 'string' ? error.payload.detail : undefined,
        cause: err,
      };
    }
  };

  return {
    status: computed(() => status.value),
    lastError: computed(() => lastError.value),
    probe,
    checkStatus,
    login,
    changePassword,
    refreshSession,
    announceChange,
    logout,
    oidcLogin,
  };
};
