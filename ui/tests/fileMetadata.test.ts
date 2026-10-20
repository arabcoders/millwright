import { expect, test } from 'bun:test';
import { fileMetadata, fileSnapshotMetadata } from '../app/utils/mediaMetadata';
import type {
  FileChangeStats,
  FileMediaSnapshot,
  MediaFileDetail,
  MediaFileRow,
  ProbeSummary,
} from '../app/types/api';

const before: FileChangeStats = {
  size: 4 * 1024 ** 3,
  duration_s: 60,
  bit_rate: 572_662_306,
  video_codec: 'h264',
  width: 1920,
  height: 960,
  audio: 2,
  subtitle: 4,
};
const after: FileChangeStats = {
  ...before,
  size: 2 * 1024 ** 3,
  bit_rate: 286_331_153,
  video_codec: 'hevc',
};
const file: MediaFileRow = {
  id: 1,
  library_id: 1,
  path: '/library/film.mkv',
  size: 2 * 1024 ** 3,
  container: 'mkv',
  video_codec: after.video_codec,
  width: after.width,
  height: after.height,
  duration_s: after.duration_s,
  bit_rate: after.bit_rate,
  medium: 'movie',
  modified_at: '2026-10-05T00:00:00Z',
  cooldown_until: null,
  created_at: '2026-10-06T00:00:00Z',
  updated_at: '2026-10-06T12:00:00Z',
  transcode_status: 'transcoded',
  health_status: 'healthy',
  active_job_kind: null,
  active_job_id: null,
  active_job_status: null,
  active_job_progress: null,
  active_job_updated_at: null,
  last_change: { job_id: 1, attempt_number: 1, before, after },
};

const probe = (stats: FileChangeStats): NonNullable<ProbeSummary> => ({
  container: 'mkv',
  size_bytes: stats.size,
  duration_s: stats.duration_s,
  streams: [
    {
      index: 0,
      codec_type: 'video',
      codec: stats.video_codec,
      width: stats.width,
      height: stats.height,
      fps: 24,
      bit_rate: 5_000_000,
      language: null,
      title: null,
      channels: null,
    },
    {
      index: 1,
      codec_type: 'audio',
      codec: 'aac',
      channels: 2,
      channel_layout: 'stereo',
      bit_rate: 128_000,
      language: null,
      title: null,
    },
    ...Array.from({ length: stats.subtitle ?? 0 }, (_, index) => ({
      index: index + 2,
      codec_type: 'subtitle',
      codec: 'subrip',
      language: null,
      title: null,
      channels: null,
    })),
  ],
});
const input = { ...probe(before), container: 'mp4' };
const outputProbe = probe({ ...after, subtitle: 1 });
const output = {
  ...outputProbe,
  streams: outputProbe.streams.map((stream) =>
    stream.codec_type === 'video'
      ? { ...stream, fps: 30 }
      : stream.codec_type === 'audio'
        ? { ...stream, codec: 'opus', channels: 6, channel_layout: '5.1', bit_rate: 256_000 }
        : stream,
  ),
};
const lastTranscode = {
  job_id: 1,
  attempt_number: 1,
  source_size: before.size,
  output_size: after.size,
  input_probe: input,
  output_probe: output,
};
const detail: MediaFileDetail = {
  ...file,
  last_change: null,
  probe: output,
  media_snapshots: [],
  jobs: [],
  last_transcode: lastTranscode,
};

test('selects recorded data', () => {
  const snapshot: FileMediaSnapshot = {
    id: 'attempt-1-original',
    job_id: 1,
    attempt_number: 1,
    stage_number: null,
    kind: 'original',
    created_at: '2026-10-05T00:00:00Z',
    probe: input,
    result: 'succeeded',
  };
  const current = fileSnapshotMetadata(detail);
  const recorded = fileSnapshotMetadata(detail, snapshot);
  for (const [key, present, previous] of [
    ['size', '2 GiB', '4 GiB'],
    ['container', 'mkv', 'mp4'],
    ['video_codec', 'hevc', 'h264'],
    ['fps', '30 fps', '24 fps'],
    ['audio_codec', 'opus', 'aac'],
    ['subtitle', '1', '4'],
  ]) {
    expect(current.find((row) => row.key === key)).toMatchObject({ value: present, after: null });
    expect(recorded.find((row) => row.key === key)).toMatchObject({ value: previous, after: null });
  }
  expect(fileSnapshotMetadata(detail)).toEqual(current);
});

test('keeps historical unknowns', () => {
  const snapshot: FileMediaSnapshot = {
    id: 'attempt-1-original',
    job_id: 1,
    attempt_number: 1,
    stage_number: null,
    kind: 'original',
    created_at: '2026-10-05T00:00:00Z',
    probe: {
      container: null,
      size_bytes: null,
      duration_s: null,
      streams: [
        {
          index: 0,
          codec_type: 'video',
          codec: 'h264',
          language: null,
          title: null,
          channels: null,
        },
      ],
    },
    result: 'failed',
  };
  const rows = fileSnapshotMetadata(detail, snapshot);
  for (const key of [
    'size',
    'container',
    'duration',
    'resolution',
    'fps',
    'bit_rate',
    'audio_layout',
  ])
    expect(rows.find((row) => row.key === key)?.value).toBe('Unknown');
  expect(rows.find((row) => row.key === 'video_codec')?.value).toBe('h264');
});

test('merges file changes', () => {
  const rows = fileMetadata(file);
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
  expect(new Set(rows.map((row) => row.key)).size).toBe(rows.length);
});

test('keeps unchanged values', () => {
  const rows = fileMetadata(file);
  for (const [key, value] of [
    ['resolution', '1920×960'],
    ['duration', '01:00'],
    ['container', 'mkv'],
    ['audio', '2'],
    ['subtitle', '4'],
  ])
    expect(rows.find((row) => row.key === key)).toMatchObject({
      value,
      after: null,
      color: 'text-toned',
    });
});

test('retains count changes', () => {
  const rows = fileMetadata({
    ...file,
    last_change: {
      job_id: 1,
      attempt_number: 1,
      before,
      after: { ...after, audio: 1, subtitle: 5 },
    },
  });
  expect(rows.find((row) => row.key === 'audio')).toMatchObject({
    value: '2',
    after: '1',
    color: 'text-success',
  });
  expect(rows.find((row) => row.key === 'subtitle')).toMatchObject({
    value: '4',
    after: '5',
    color: 'text-error',
  });
});

test('uses catalog metadata', () => {
  const rows = fileMetadata({ ...file, last_change: null, bit_rate: 5_850_000 });
  expect(rows.map((row) => row.key)).toEqual([
    'size',
    'container',
    'video_codec',
    'resolution',
    'duration',
    'bit_rate',
  ]);
  expect(rows.find((row) => row.key === 'size')?.value).toBe('2 GiB');
  expect(rows.find((row) => row.key === 'bit_rate')?.value).toBe('5.85 Mbps');
  expect(rows.every((row) => row.after === null)).toBe(true);
});

test('preserves measured bitrate', () => {
  for (const [bit_rate, value, color] of [
    [285_000_000, '285 Mbps', 'text-success'],
    [600_000_000, '600 Mbps', 'text-error'],
  ] as const) {
    const rows = fileMetadata({ ...file, bit_rate });
    expect(rows.find((row) => row.key === 'bit_rate')).toMatchObject({ after: value, color });
  }
});

test('ignores outdated comparisons', () => {
  for (const change of [
    { size: file.size + 1 },
    { video_codec: 'av1' },
    { width: 1280 },
    { height: 720 },
    { duration_s: 90 },
  ]) {
    const updated = { ...file, ...change };
    const rows = fileMetadata(updated);
    expect(rows).toEqual(fileMetadata({ ...updated, last_change: null }));
  }
});

test('merges detail snapshots', () => {
  const rows = fileMetadata(detail);
  for (const [key, value, after, color] of [
    ['size', '4 GiB', '2 GiB', 'text-success'],
    ['container', 'mp4', 'mkv', 'text-info'],
    ['video_codec', 'h264', 'hevc', 'text-info'],
    ['fps', '24 fps', '30 fps', 'text-info'],
    ['audio_codec', 'aac', 'opus', 'text-info'],
    ['audio_layout', 'stereo', '5.1', 'text-info'],
    ['audio_bit_rate', '128 kbps', '256 kbps', 'text-info'],
    ['subtitle', '4', '1', 'text-success'],
  ])
    expect(rows.find((row) => row.key === key)).toMatchObject({ value, after, color });
  expect(rows.find((row) => row.key === 'resolution')).toMatchObject({
    value: '1920×960',
    after: null,
  });
  expect(rows.find((row) => row.key === 'audio')).toMatchObject({ value: '1', after: null });
  expect(new Set(rows.map((row) => row.key)).size).toBe(rows.length);
});

test('uses current detail', () => {
  for (const last_transcode of [
    null,
    { ...lastTranscode, input_probe: null },
    { ...lastTranscode, output_probe: null },
  ]) {
    const rows = fileMetadata({ ...detail, last_transcode });
    expect(rows.every((row) => row.after === null)).toBe(true);
    expect(rows.find((row) => row.key === 'fps')?.value).toBe('30 fps');
    expect(rows.find((row) => row.key === 'audio_codec')?.value).toBe('opus');
    expect(rows.find((row) => row.key === 'subtitle')?.value).toBe('1');
  }
});

test('ignores outdated snapshots', () => {
  const updated = { ...detail, size: detail.size + 1, video_codec: 'av1' };
  const rows = fileMetadata(updated);
  expect(rows).toEqual(fileMetadata({ ...updated, last_transcode: null }));
  expect(rows.find((row) => row.key === 'video_codec')?.value).toBe('av1');
});

test('retains catalog bitrate', () => {
  const rows = fileMetadata({ ...detail, bit_rate: 600_000_000 });
  expect(rows.find((row) => row.key === 'bit_rate')).toMatchObject({
    after: '600 Mbps',
    color: 'text-error',
  });
});
