import { expect, test } from 'bun:test';
import type { JobEventRow, ProbeData } from '../app/types/api';
import { jobSnapshots } from '../app/utils/jobSnapshots';

const input: NonNullable<ProbeData> = {
  file_path: '/workspace/source.mkv',
  container: 'mkv',
  duration_s: 60,
  size_bytes: 4000,
  streams: [
    { index: 0, codec_type: 'video', codec: 'h264', language: null, title: null, channels: null },
    {
      index: 1,
      codec_type: 'audio',
      codec: 'aac',
      language: 'eng',
      title: 'Main',
      channels: 2,
      disposition_default: true,
    },
    {
      index: 2,
      codec_type: 'subtitle',
      codec: 'subrip',
      language: 'eng',
      title: null,
      channels: null,
      disposition_forced: true,
    },
  ],
  raw: { format: { tags: { title: 'Original' } } },
};
const cleaned: NonNullable<ProbeData> = {
  ...input,
  size_bytes: 3500,
  streams: input.streams.filter((stream) => stream.codec_type !== 'subtitle'),
};
const output: NonNullable<ProbeData> = {
  ...cleaned,
  size_bytes: 2000,
  streams: cleaned.streams.map((stream) =>
    stream.codec_type === 'video' ? { ...stream, codec: 'hevc' } : stream,
  ),
};
const attempt = {
  id: 10,
  input_probe: input,
  output_probe: output,
  source_size: 4000,
  output_size: 2000,
  result: 'succeeded',
};
const event = (
  id: number,
  attempt_id: number,
  payload: JobEventRow['payload'],
  type = 'stage',
): JobEventRow => ({
  id,
  attempt_id,
  payload,
  type,
  ts: '2026-10-16T10:00:00Z',
  created_at: '2026-10-16T10:00:00Z',
  updated_at: '2026-10-16T10:00:00Z',
});

test('isolates attempt stages', () => {
  const events = [
    event(22, 10, { number: 2, probe: cleaned, output_probe: output, result: 'succeeded' }),
    event(11, 9, { number: 1, probe: output, output_probe: output }),
    event(21, 10, { number: 1, probe: input, output_probe: cleaned, result: 'succeeded' }),
    event(23, 10, { probe: output }, 'progress'),
  ];
  const snapshots = jobSnapshots(attempt, events);
  expect(snapshots.map((snapshot) => snapshot.label)).toEqual([
    'Whole attempt',
    'Stage 1',
    'Stage 2',
  ]);
  expect(snapshots[0]).toMatchObject({ input, output, sourceSize: 4000, outputSize: 2000 });
  expect(snapshots[1]?.input?.streams.map((stream) => stream.codec)).toEqual([
    'h264',
    'aac',
    'subrip',
  ]);
  expect(snapshots[1]?.output?.streams.map((stream) => stream.codec)).toEqual(['h264', 'aac']);
  expect(snapshots[1]?.input?.raw).toEqual(input.raw);
  expect(snapshots[2]?.input?.size_bytes).toBe(3500);
  expect(snapshots[2]?.output?.streams[0]?.codec).toBe('hevc');
  expect(jobSnapshots({ ...attempt, id: 9 }, events).map((snapshot) => snapshot.value)).toEqual([
    'attempt',
    'stage-11',
  ]);
});

test('retains failed probes', () => {
  const snapshots = jobSnapshots(
    {
      ...attempt,
      input_probe: null,
      result: 'failed',
    },
    [
      event(21, 10, {
        number: 1,
        source_size: 4000,
        output_size: 2000,
        output_probe: output,
        result: 'failed',
      }),
      event(22, 10, { number: 2, probe: cleaned }),
    ],
  );
  expect(snapshots[0]).toMatchObject({ input: null, output, result: 'failed' });
  expect(snapshots[1]).toMatchObject({
    input: null,
    output,
    sourceSize: 4000,
    outputSize: 2000,
    result: 'failed',
  });
  expect(snapshots[2]).toMatchObject({
    input: cleaned,
    output: null,
    outputSize: null,
    result: null,
  });
});
