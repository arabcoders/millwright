import type { ApiErrorPayload } from '~/types/responses';

export const useApiErrorMessage = () => {
  const detailText = (detail: ApiErrorPayload['detail']): string => {
    if (typeof detail === 'string') {
      return detail;
    }

    if (!Array.isArray(detail)) {
      return '';
    }

    return detail
      .map((err) => {
        const field = Array.isArray(err.loc) ? err.loc.at(-1) : undefined;
        if (field && err.msg) {
          return `${field}: ${err.msg}`;
        }
        return err.msg || err.code || '';
      })
      .filter(Boolean)
      .join(', ');
  };

  const messageFor = (
    payload: ApiErrorPayload | string | null | undefined,
    fallback = 'Unknown error',
  ): string => {
    if (typeof payload === 'string') {
      return payload || fallback;
    }

    if (!payload) {
      return fallback;
    }

    if (payload.error) {
      const detail = detailText(payload.detail);
      return detail ? `${payload.error} - ${detail}` : payload.error;
    }

    if (payload.message) {
      return payload.message;
    }

    const detail = detailText(payload.detail);
    if (detail) {
      return detail;
    }

    return fallback;
  };

  return {
    messageFor,
  };
};
