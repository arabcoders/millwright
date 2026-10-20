import type { JobDetail, ProbeData } from '~/types/api';

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const number = (value: unknown): boolean => typeof value === 'number' && Number.isFinite(value);
const text = (value: unknown): boolean => typeof value === 'string';
const optional = (value: unknown, valid: (value: unknown) => boolean): boolean =>
  value == null || valid(value);
const texts = (value: unknown): boolean => Array.isArray(value) && value.every(text);
const fields = (
  value: Record<string, unknown>,
  names: string[],
  valid: (value: unknown) => boolean,
): boolean => names.every((name) => optional(value[name], valid));
export const validProbeSnapshot = (value: unknown): value is NonNullable<ProbeData> =>
  record(value) &&
  fields(value, ['container', 'file_path'], text) &&
  fields(value, ['duration_s', 'size_bytes'], number) &&
  Array.isArray(value.streams) &&
  value.streams.every(
    (stream) =>
      record(stream) &&
      fields(
        stream,
        [
          'codec',
          'codec_type',
          'language',
          'title',
          'channel_layout',
          'pix_fmt',
          'profile',
          'filename',
          'mimetype',
        ],
        text,
      ) &&
      fields(
        stream,
        ['index', 'channels', 'width', 'height', 'fps', 'sample_rate', 'bit_rate'],
        number,
      ) &&
      fields(
        stream,
        [
          'disposition_default',
          'disposition_forced',
          'disposition_commentary',
          'disposition_attached_pic',
        ],
        (flag) => typeof flag === 'boolean',
      ),
  ) &&
  optional(value.raw, record);

export const validJobExport = (value: unknown): value is JobDetail => {
  if (!record(value) || !number(value.id) || !text(value.status) || !text(value.kind)) return false;
  if (
    !fields(
      value,
      ['created_at', 'updated_at', 'started_at', 'finished_at', 'warning', 'error'],
      text,
    ) ||
    !optional(value.progress, record)
  )
    return false;
  if (
    value.file != null &&
    (!record(value.file) ||
      !text(value.file.path) ||
      !number(value.file.size) ||
      !fields(
        value.file,
        ['container', 'video_codec', 'audio_codec', 'audio_channel_layout'],
        text,
      ) ||
      !fields(
        value.file,
        [
          'width',
          'height',
          'duration_s',
          'video_fps',
          'video_bit_rate',
          'audio_channels',
          'audio_bit_rate',
        ],
        number,
      ))
  )
    return false;
  if (
    !Array.isArray(value.attempts) ||
    !value.attempts.every(
      (attempt) =>
        record(attempt) &&
        number(attempt.id) &&
        number(attempt.attempt_number) &&
        fields(
          attempt,
          [
            'status',
            'result',
            'node_name',
            'error',
            'log_data',
            'log_path',
            'claimed_at',
            'finished_at',
          ],
          text,
        ) &&
        optional(attempt.argv, texts) &&
        fields(attempt, ['source_size', 'output_size'], number) &&
        optional(attempt.health, record) &&
        optional(attempt.input_probe, validProbeSnapshot) &&
        optional(attempt.output_probe, validProbeSnapshot) &&
        optional(
          attempt.action_results,
          (actions) =>
            Array.isArray(actions) &&
            actions.every(
              (action) =>
                record(action) &&
                text(action.kind) &&
                typeof action.ok === 'boolean' &&
                optional(action.error, text) &&
                optional(action.artifacts, texts),
            ),
        ),
    )
  )
    return false;
  if (
    !Array.isArray(value.events) ||
    !value.events.every(
      (event) =>
        record(event) &&
        number(event.id) &&
        text(event.type) &&
        text(event.ts) &&
        optional(event.attempt_id, number) &&
        optional(event.payload, record),
    )
  )
    return false;
  if (
    !Array.isArray(value.notification_deliveries) ||
    !value.notification_deliveries.every(
      (delivery) =>
        record(delivery) &&
        number(delivery.id) &&
        text(delivery.target_name) &&
        ['pending', 'sent', 'failed'].includes(String(delivery.status)) &&
        number(delivery.attempts) &&
        optional(delivery.error, text),
    )
  )
    return false;
  if (
    !optional(
      value.decision_log,
      (steps) =>
        Array.isArray(steps) &&
        steps.every(
          (step) =>
            record(step) &&
            text(step.node) &&
            text(step.outcome) &&
            fields(step, ['step', 'reason', 'error'], text),
        ),
    )
  )
    return false;
  if (
    !optional(
      value.flow_snapshot,
      (snapshot) => record(snapshot) && text(snapshot.name) && number(snapshot.version),
    )
  )
    return false;
  return true;
};
