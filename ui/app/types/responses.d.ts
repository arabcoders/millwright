export type ApiErrorPayload = {
  error?: string;
  message?: string;
  code?: string;
  params?: Record<string, string | number | boolean | null>;
  detail?:
    | string
    | Array<{
        loc?: Array<string | number>;
        msg?: string;
        type?: string;
        code?: string;
      }>;
  technical_message?: string;
};
