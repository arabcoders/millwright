import { expect, test } from 'bun:test';
import { jobMetadata } from '../app/utils/mediaMetadata';
import type { JobFile, JobRow, ProbeSummary, ProbeStream } from '../app/types/api';

const stream = (codec_type: string, codec: string): ProbeStream => ({
  index: 0,
  codec_type,
  codec,
  language: null,
  title: null,
  channels: null,
});
const input: NonNullable<ProbeSummary> = {
  container: 'mkv',
  duration_s: 60,
  size_bytes: 1024 ** 3 * 4,
  streams: [
    { ...stream('video', 'h264'), width: 1920, height: 960, fps: 24, bit_rate: 10_000_000 },
    { ...stream('audio', 'aac'), channels: 2, channel_layout: 'stereo', bit_rate: 128_000 },
    stream('subtitle', 'subrip'),
    stream('subtitle', 'subrip'),
    stream('subtitle', 'subrip'),
    stream('subtitle', 'subrip'),
  ],
};
const output: NonNullable<ProbeSummary> = {
  ...input,
  size_bytes: 1024 ** 3 * 2,
  streams: input.streams.map((value) =>
    value.codec_type === 'video' ? { ...value, codec: 'hevc', bit_rate: 5_000_000 } : value,
  ),
};
const file: JobFile = {
  id: 1,
  library_id: 1,
  path: '/library/film.mkv',
  size: 1024 ** 3,
  container: 'mp4',
  video_codec: 'av1',
  width: 1280,
  height: 720,
  duration_s: 90,
  video_fps: 30,
  video_bit_rate: 3_000_000,
  audio_codec: 'opus',
  audio_channels: 6,
  audio_channel_layout: '5.1',
  audio_bit_rate: 192_000,
};

test('merges changed metadata', () => {
  const rows = jobMetadata(
    file,
    { input_probe: input, output_probe: output, result: 'succeeded' },
    {
      kind: 'transcode',
      status: 'succeeded',
      decision_log: [
        {
          node: 'encode',
          outcome: 'plan',
          reason: 'Transcode video stream 0 (und, h264): encode libx265.',
        },
      ],
    },
  );
  expect(rows.find((row) => row.key === 'size')).toMatchObject({
    value: '4 GiB',
    after: '2 GiB',
    color: 'text-success',
  });
  expect(rows.find((row) => row.key === 'video_codec')).toMatchObject({
    value: 'h264',
    after: 'hevc',
    color: 'text-info',
  });
  expect(rows.find((row) => row.key === 'bit_rate')).toMatchObject({
    value: '572.66 Mbps',
    after: '286.33 Mbps',
    color: 'text-success',
  });
  expect(rows.find((row) => row.key === 'video_bit_rate')).toMatchObject({
    value: '10 Mbps',
    after: '5 Mbps',
    color: 'text-info',
  });
});

test('shows unchanged values once', () => {
  const rows = jobMetadata(file, { input_probe: input, output_probe: output, result: 'succeeded' });
  for (const [key, value] of [
    ['resolution', '1920×960'],
    ['duration', '01:00'],
    ['fps', '24 fps'],
    ['audio_codec', 'aac'],
    ['audio_layout', 'stereo'],
    ['audio_bit_rate', '128 kbps'],
    ['subtitle', '4'],
  ]) {
    expect(rows.find((row) => row.key === key)).toMatchObject({
      value,
      after: null,
      tooltip: null,
    });
  }
  expect(new Set(rows.map((row) => row.key)).size).toBe(rows.length);
});

test('retains pending input metadata', () => {
  const rows = jobMetadata(file, { input_probe: input, output_probe: output, result: null });
  expect(rows.find((row) => row.key === 'size')?.value).toBe('4 GiB');
  expect(rows.find((row) => row.key === 'container')?.value).toBe('mkv');
  expect(rows.every((row) => row.after === null)).toBe(true);
});

test('ignores unsuccessful output', () => {
  for (const result of ['failed', 'cancelled', 'rejected']) {
    const rows = jobMetadata(file, { input_probe: input, output_probe: output, result });
    expect(rows.find((row) => row.key === 'video_codec')?.value).toBe('h264');
    expect(rows.every((row) => row.after === null)).toBe(true);
  }
});

test('uses file before snapshots', () => {
  const rows = jobMetadata(file, null);
  expect(rows.find((row) => row.key === 'size')?.value).toBe('1 GiB');
  expect(rows.find((row) => row.key === 'video_codec')?.value).toBe('av1');
  expect(rows.find((row) => row.key === 'audio_layout')?.value).toBe('5.1');
  expect(rows.every((row) => row.after === null)).toBe(true);
  const incomplete = jobMetadata(file, {
    input_probe: null,
    output_probe: output,
    result: 'succeeded',
  });
  expect(incomplete).toEqual(rows);
});

test('shows stream changes inline', () => {
  const rows = jobMetadata(file, {
    input_probe: input,
    output_probe: {
      ...output,
      size_bytes: input.size_bytes! * 2,
      streams: [
        ...output.streams
          .filter((value) => value.codec_type !== 'subtitle')
          .map((value) =>
            value.codec_type === 'video' ? { ...value, width: 1280, height: 640, fps: 30 } : value,
          ),
        { ...stream('audio', 'aac'), index: 2 },
      ],
    },
    result: 'succeeded',
  });
  expect(rows.find((row) => row.key === 'size')).toMatchObject({
    value: '4 GiB',
    after: '8 GiB',
    color: 'text-error',
  });
  expect(rows.find((row) => row.key === 'resolution')).toMatchObject({
    value: '1920×960',
    after: '1280×640',
    color: 'text-info',
  });
  expect(rows.find((row) => row.key === 'fps')).toMatchObject({ value: '24 fps', after: '30 fps' });
  expect(rows.find((row) => row.key === 'subtitle')).toMatchObject({
    value: '4',
    after: '0',
    color: 'text-success',
  });
  expect(rows.find((row) => row.key === 'audio')).toMatchObject({
    value: '1',
    after: '2',
    color: 'text-error',
  });
});

test('explains planned removals', () => {
  const source = {
    ...input,
    streams: [
      { ...stream('video', 'h264'), index: 0 },
      { ...stream('audio', 'aac'), index: 1, language: 'eng' },
      { ...stream('audio', 'flac'), index: 6, language: 'ita' },
      { ...stream('subtitle', 'subrip'), index: 4, language: 'eng' },
      { ...stream('subtitle', 'subrip'), index: 5, language: 'ita' },
      { ...stream('subtitle', 'subrip'), index: 13, language: 'bul' },
    ],
  };
  const result = {
    ...source,
    streams: [source.streams[0]!, source.streams[1]!, { ...source.streams[3]!, index: 2 }],
  };
  const job: Pick<JobRow, 'kind' | 'status' | 'decision_log'> = {
    kind: 'transcode',
    status: 'running',
    decision_log: [
      {
        node: 'cleanup',
        outcome: 'plan',
        reason:
          'Drop audio stream 6 (ita, flac): language not in keep list. Drop subtitle stream 5 (ita, subrip): language not in keep list. Drop subtitle stream 13 (bul, subrip): language not in keep list.',
      },
      {
        node: 'encode',
        outcome: 'plan',
        reason: 'Transcode video stream 0 (und, h264): encode libx265.',
      },
    ],
  };
  const attempt = { input_probe: source, output_probe: null, result: null };
  const planned = jobMetadata(file, attempt, job);
  expect(planned.find((row) => row.key === 'subtitle')).toMatchObject({
    value: '3',
    after: '1',
    expected: true,
    color: 'text-info',
  });
  expect(planned.find((row) => row.key === 'audio')).toMatchObject({
    value: '2',
    after: '1',
    expected: true,
  });
  expect(planned.find((row) => row.key === 'video_codec')).toMatchObject({
    after: null,
    expected: true,
  });
  const cancelled = jobMetadata(
    file,
    { ...attempt, result: 'cancelled' },
    { ...job, status: 'cancelled' },
  );
  expect(cancelled.find((row) => row.key === 'subtitle')).toMatchObject({
    value: '3',
    after: '1',
    expected: true,
    color: 'text-info',
  });
  const finished = jobMetadata(
    file,
    { ...attempt, output_probe: result, result: 'succeeded' },
    { ...job, status: 'succeeded' },
  );
  expect(finished.find((row) => row.key === 'subtitle')).toMatchObject({
    value: '3',
    after: '1',
    expected: false,
    color: 'text-success',
  });
  for (const inactive of [
    { ...job, status: 'pending' as const },
    { ...job, status: 'failed' as const },
    { ...job, kind: 'healthcheck' },
  ]) {
    expect(
      jobMetadata(file, attempt, inactive).every(
        (row) => row.after === null && row.tooltip === null,
      ),
    ).toBe(true);
  }
  expect(
    jobMetadata(file, { ...attempt, input_probe: null }, job).some((row) => row.key === 'subtitle'),
  ).toBe(false);
});

test('works without library file', () => {
  const rows = jobMetadata(null, { input_probe: input, output_probe: output, result: 'succeeded' });
  expect(rows.find((row) => row.key === 'size')).toMatchObject({ value: '4 GiB', after: '2 GiB' });
  expect(jobMetadata(null, null)).toEqual([]);
});

test('counts absent streams', () => {
  const rows = jobMetadata(null, {
    input_probe: { container: null, size_bytes: null, duration_s: null, streams: [] },
    output_probe: null,
    result: null,
  });
  expect(rows.find((row) => row.key === 'subtitle')?.value).toBe('0');
});
