type DiagnosticFields = {
  event: string;
  component: string;
  class_name?: string;
  request_id?: string;
  method?: string;
  status?: number;
  duration_ms?: number;
};

const diagnostic = (level: 'debug' | 'warn' | 'error', fields: DiagnosticFields): void => {
  if (!import.meta.dev && level === 'debug') return;
  const safe: DiagnosticFields = {
    event: fields.event.slice(0, 80),
    component: fields.component.slice(0, 80),
  };
  if (fields.class_name && /^[A-Za-z][\w$]{0,63}$/u.test(fields.class_name))
    safe.class_name = fields.class_name;
  if (fields.request_id && /^[\w-]{1,128}$/u.test(fields.request_id))
    safe.request_id = fields.request_id;
  if (fields.method) safe.method = fields.method.slice(0, 12);
  if (typeof fields.status === 'number' && Number.isInteger(fields.status))
    safe.status = fields.status;
  if (typeof fields.duration_ms === 'number' && Number.isFinite(fields.duration_ms)) {
    safe.duration_ms = Math.max(0, Math.round(fields.duration_ms));
  }
  const message = JSON.stringify(safe);
  if (level === 'debug') console.debug(message);
  else if (level === 'warn') console.warn(message);
  else console.error(message);
};

export { diagnostic };
