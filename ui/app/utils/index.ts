import type { ApiErrorPayload } from '~/types/responses';
import { getAuthGeneration, invalidateAuth } from '~/composables/useAuth';
import { diagnostic } from '~/utils/diagnostic';

const APP_TITLE = 'Millwright';

const iTrim = (str: string, delim: string, position: 'start' | 'end' | 'both' = 'both'): string => {
  if (!str) {
    return str;
  }

  if (!delim) {
    throw new Error('Delimiter is required');
  }

  // Escape special regex characters for use in a character class: \ ] ^ -
  const escapedDelim = delim.replace(/[\\^\-\]]/g, '\\$&');

  if (['both', 'start'].includes(position)) {
    str = str.replace(new RegExp(`^[${escapedDelim}]+`, 'g'), '');
  }

  if (['both', 'end'].includes(position)) {
    str = str.replace(new RegExp(`[${escapedDelim}]+$`, 'g'), '');
  }

  return str;
};

const eTrim = (str: string, delim: string): string => iTrim(str, delim, 'end');

const sTrim = (str: string, delim: string): string => iTrim(str, delim, 'start');

const copyText = (str: string, notify: boolean = true, store: boolean = false): void => {
  const toast = useNotification();

  if (navigator.clipboard) {
    navigator.clipboard
      .writeText(str)
      .then(() => {
        if (notify) toast.success('Copied to clipboard.');
      })
      .catch((error) => {
        console.error('Failed to copy.', error);
        if (notify) toast.error('Failed to copy.');
      });
    return;
  }

  const el = document.createElement('textarea');
  el.value = str;
  document.body.appendChild(el);
  el.select();
  document.execCommand('copy');
  document.body.removeChild(el);

  if (notify) {
    toast.success('Copied to clipboard.', { store });
  }
};

const jobNumber = (id: number): string => `#${String(id).padStart(4, '0')}`;

const formatBytes = (bytes: number, decimals: number = 2): string => {
  if (!+bytes) {
    return '0 Bytes';
  }
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['Bytes', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB', 'ZiB', 'YiB'];
  const magnitude = Math.abs(bytes);
  const i = Math.floor(Math.log(magnitude) / Math.log(k));
  return `${bytes < 0 ? '-' : ''}${parseFloat((magnitude / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
};

const formatTime = (seconds: number): string => {
  const hrs = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  const pad = (n: number): string => n.toString().padStart(2, '0');

  if (hrs > 0) {
    return `${pad(hrs)}:${pad(mins)}:${pad(secs)}`;
  }

  if (mins > 0) {
    return `${pad(mins)}:${pad(secs)}`;
  }

  return `${secs}`;
};

const formatBitrate = (bitrate: number | null | undefined): string => {
  if (!bitrate) return 'Unknown';
  const unit = bitrate >= 1_000_000 ? 'Mbps' : 'kbps';
  const value = bitrate >= 1_000_000 ? bitrate / 1_000_000 : bitrate / 1_000;
  return `${value.toFixed(2).replace(/\.?0+$/, '')} ${unit}`;
};

const uri = (u: string): string => {
  const runtimeConfig = useRuntimeConfig();

  if (!u || '/' === runtimeConfig.app.baseURL || !u.startsWith('/')) {
    return u;
  }

  if (u.startsWith(runtimeConfig.app.baseURL)) {
    return u;
  }

  return `${eTrim(runtimeConfig.app.baseURL, '/')}/${sTrim(u, '/')}`;
};

const parse_api_error = async (json: unknown): Promise<string> => {
  if (json && typeof json === 'object' && 'then' in json && typeof json.then === 'function') {
    json = await (json as Promise<unknown>);
  }

  if (!json || 'object' !== typeof json) {
    return 'An unknown error occurred.';
  }

  const payload = json as {
    error?: string;
    message?: string;
    detail?: string | Array<{ loc: string[]; msg: string; type: string }>;
  };

  let extra_detail = '';

  if (Array.isArray(payload.detail)) {
    const errors = payload.detail.map((err: any) => {
      if ('object' === typeof err && err.loc && err.msg) {
        const field = Array.isArray(err.loc) ? err.loc[err.loc.length - 1] : 'unknown';
        return `${field}: ${err.msg}`;
      }
      return String(err);
    });
    extra_detail = errors.join(', ');
  } else if (typeof payload.detail === 'string' && payload.detail.trim()) {
    extra_detail = payload.detail.trim();
  }

  const message = payload.message || payload.error || '';

  const details: string[] = [];
  const addDetail = (value: string | undefined): void => {
    const detail = value?.trim();
    if (detail && detail !== message && !details.includes(detail)) {
      details.push(detail);
    }
  };

  addDetail(payload.error);
  addDetail(extra_detail);

  if (message) {
    return [message, ...details].join(' - ');
  }

  if (details.length > 0) {
    return details.join(' - ');
  }

  return 'An unknown error occurred.';
};

class ApiError extends Error {
  readonly status: number;
  readonly payload: ApiErrorPayload | null;
  readonly fields: Record<string, string>;
  readonly request_id: string | null;

  constructor(
    message: string,
    options: {
      status?: number;
      payload?: ApiErrorPayload | null;
      cause?: unknown;
      request_id?: string | null;
    } = {},
  ) {
    super(message, { cause: options.cause });
    this.name = 'ApiError';
    this.status = options.status ?? 0;
    this.payload = options.payload ?? null;
    this.fields = {};
    this.request_id = options.request_id ?? null;

    if (Array.isArray(this.payload?.detail)) {
      for (const item of this.payload.detail) {
        const field = item.loc?.at(-1);
        if (field !== undefined && item.msg) {
          this.fields[String(field)] = item.msg;
        }
      }
    }
  }
}

const to_api_error = (error: unknown): ApiError => {
  if (error instanceof ApiError) {
    return error;
  }

  const message = error instanceof Error ? error.message : 'An unknown error occurred.';
  return new ApiError(message, { cause: error });
};

const ensure_api_success = async (response: Response): Promise<void> => {
  if (response.ok) {
    return;
  }

  let payload: ApiErrorPayload | null = null;
  try {
    payload = (await response.clone().json()) as ApiErrorPayload;
  } catch {
    // The status still provides useful context when the response has no JSON body.
  }

  throw new ApiError(await parse_api_error(payload), {
    status: response.status,
    payload,
    request_id: response.headers.get('X-Request-ID'),
  });
};

const formatPageTitle = (title?: string | null): string => {
  const normalized = title?.trim();

  if (!normalized || normalized === APP_TITLE) {
    return APP_TITLE;
  }

  return `${normalized} | ${APP_TITLE}`;
};

const request = (
  url: string,
  options: RequestInit & { timeout?: number } = {},
): Promise<Response> => {
  const { timeout, ...fetchOptions } = options;

  fetchOptions.method = fetchOptions.method || 'GET';
  const headers = new Headers(fetchOptions.headers);
  fetchOptions.headers = headers;

  if (!headers.has('Content-Type')) {
    if (!(options?.body instanceof FormData)) {
      headers.set('Content-Type', 'application/json');
    }
  }

  if (!headers.has('Accept')) {
    headers.set('Accept', 'application/json');
  }

  if (url.startsWith('/')) {
    fetchOptions.credentials = 'same-origin';
  }

  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(fetchOptions.method.toUpperCase())) {
    headers.set('X-Millwright-Request', '1');
  }
  const generation = getAuthGeneration();

  let controller: AbortController | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let timedOut = false;

  if (typeof timeout === 'number' && timeout > 0) {
    controller = new AbortController();
    fetchOptions.signal = controller.signal;
    timer = setTimeout(() => {
      timedOut = true;
      controller!.abort();
    }, timeout * 1000);
  }

  const started = performance.now();
  const method = String(fetchOptions.method ?? 'GET').toUpperCase();
  return fetch(url.startsWith('/') ? uri(url) : url, fetchOptions)
    .then((response) => {
      const requestId = response.headers.get('X-Request-ID');
      if (!response.ok && response.status !== 401) {
        diagnostic(response.status >= 500 ? 'error' : 'debug', {
          event: 'request.failed',
          component: 'request',
          request_id: requestId ?? undefined,
          method,
          status: response.status,
          duration_ms: Math.round(performance.now() - started),
        });
      }
      const path = url.split('?')[0] ?? '';
      if (
        import.meta.client &&
        response.status === 401 &&
        getAuthGeneration() === generation &&
        ![
          '/api/auth/me',
          '/api/auth/logout',
          '/api/auth/login',
          '/api/auth/setup',
          '/api/auth/status',
        ].includes(path)
      ) {
        invalidateAuth();
        void navigateTo('/login');
      }
      return response;
    })
    .catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError' && !timedOut) {
        diagnostic('debug', { event: 'request.cancelled', component: 'request', method });
        throw error;
      }
      diagnostic('error', {
        event: timedOut ? 'request.timeout' : 'request.network_failed',
        component: 'request',
        method,
        duration_ms: Math.round(performance.now() - started),
      });
      throw error;
    })
    .finally(() => {
      if (timer) {
        clearTimeout(timer);
      }
    });
};

export {
  copyText,
  jobNumber,
  formatBytes,
  formatTime,
  formatBitrate,
  uri,
  parse_api_error,
  ApiError,
  to_api_error,
  ensure_api_success,
  formatPageTitle,
  request,
};
