import type {
  FileChangeStats,
  FileMediaSnapshot,
  JobAttemptRow,
  JobFile,
  JobRow,
  MediaFileDetail,
  MediaFileRow,
  ProbeSummary,
} from '~/types/api';
import { ACTIVE_JOB_STATUSES } from '~/types/api';
import { formatBitrate, formatBytes, formatTime } from '~/utils';
import { formatReason, jobReason } from '~/utils/jobList';

const statsFromProbe = (probe: ProbeSummary): FileChangeStats => {
  const streams = probe?.streams ?? [];
  const video = streams.find((stream) => stream.codec_type === 'video');
  return {
    size: probe?.size_bytes ?? null,
    duration_s: probe?.duration_s ?? null,
    bit_rate:
      probe?.size_bytes && probe?.duration_s ? (probe.size_bytes * 8) / probe.duration_s : null,
    video_codec: video?.codec ?? null,
    width: video?.width ?? null,
    height: video?.height ?? null,
    audio: streams.filter((stream) => stream.codec_type === 'audio').length,
    subtitle: streams.filter((stream) => stream.codec_type === 'subtitle').length,
  };
};

const metadataValues = (
  file: (JobFile & { bit_rate?: number | null }) | null,
  probe: ProbeSummary,
) => {
  const video = probe?.streams.find((stream) => stream.codec_type === 'video');
  const audio = probe?.streams.find((stream) => stream.codec_type === 'audio');
  const size = probe ? probe.size_bytes : file?.size;
  const duration = probe ? probe.duration_s : file?.duration_s;
  const width = probe ? video?.width : file?.width;
  const height = probe ? video?.height : file?.height;
  const fps = probe ? video?.fps : file?.video_fps;
  const channels = probe ? audio?.channels : file?.audio_channels;
  const layout = probe ? audio?.channel_layout : file?.audio_channel_layout;
  const bitrate = probe
    ? statsFromProbe(probe).bit_rate
    : file && 'bit_rate' in file
      ? file.bit_rate
      : size && duration
        ? (size * 8) / duration
        : null;
  return {
    size: size == null ? 'Unknown' : formatBytes(size),
    container: (probe ? probe.container : file?.container) ?? 'Unknown',
    video_codec: probe ? (video?.codec ?? 'No video') : (file?.video_codec ?? 'Unknown'),
    resolution: width && height ? `${width}×${height}` : 'Unknown',
    duration: duration == null ? 'Unknown' : formatTime(duration),
    fps: fps ? `${fps.toFixed(2).replace(/\.?0+$/, '')} fps` : 'Unknown',
    bit_rate: formatBitrate(bitrate),
    video_bit_rate: formatBitrate(probe ? video?.bit_rate : file?.video_bit_rate),
    audio_codec: probe ? (audio?.codec ?? 'No audio') : (file?.audio_codec ?? 'Unknown'),
    audio_layout: layout || (channels ? `${channels} channels` : 'Unknown'),
    audio_bit_rate: formatBitrate(probe ? audio?.bit_rate : file?.audio_bit_rate),
    audio: probe
      ? String(probe.streams.filter((stream) => stream.codec_type === 'audio').length)
      : 'Unknown',
    subtitle: probe
      ? String(probe.streams.filter((stream) => stream.codec_type === 'subtitle').length)
      : 'Unknown',
  };
};

const fields = [
  { key: 'size', label: 'File size', icon: 'i-lucide-hard-drive' },
  { key: 'container', label: 'Container', icon: 'i-lucide-box' },
  { key: 'video_codec', label: 'Video codec', icon: 'i-lucide-film' },
  { key: 'resolution', label: 'Resolution', icon: 'i-lucide-scan' },
  { key: 'duration', label: 'Duration', icon: 'i-lucide-clock-3' },
  { key: 'fps', label: 'Frame rate', icon: 'i-lucide-gauge' },
  { key: 'bit_rate', label: 'Overall bitrate', icon: 'i-lucide-signal' },
  { key: 'video_bit_rate', label: 'Video bitrate', icon: 'i-lucide-signal' },
  { key: 'audio_codec', label: 'Audio codec', icon: 'i-lucide-volume-2' },
  { key: 'audio_layout', label: 'Audio layout', icon: 'i-lucide-speaker' },
  { key: 'audio_bit_rate', label: 'Audio bitrate', icon: 'i-lucide-radio' },
  { key: 'audio', label: 'Audio streams', icon: 'i-lucide-volume-2' },
  { key: 'subtitle', label: 'Subtitles', icon: 'i-lucide-captions' },
] satisfies Array<{ key: keyof ReturnType<typeof metadataValues>; label: string; icon: string }>;

const metadataRows = (
  before: ReturnType<typeof metadataValues>,
  after: ReturnType<typeof metadataValues> | null,
  beforeStats: FileChangeStats | null,
  afterStats: FileChangeStats | null,
) => {
  return fields.map((field) => {
    const value = before[field.key];
    const next = after?.[field.key] ?? null;
    const changed = next !== null && value !== next;
    let color = changed ? 'text-info' : 'text-toned';
    if (
      changed &&
      beforeStats &&
      afterStats &&
      (field.key === 'size' ||
        field.key === 'bit_rate' ||
        field.key === 'audio' ||
        field.key === 'subtitle')
    ) {
      const previous = beforeStats[field.key];
      const current = afterStats[field.key];
      if (previous != null && current != null)
        color = current < previous ? 'text-success' : current > previous ? 'text-error' : color;
    }
    return { ...field, value, after: changed ? next : null, color };
  });
};

export const jobMetadata = (
  file: JobFile | null,
  attempt: Pick<JobAttemptRow, 'input_probe' | 'output_probe' | 'result'> | null | undefined,
  job: Pick<JobRow, 'kind' | 'status' | 'decision_log'> | null = null,
) => {
  const input = attempt?.input_probe ?? null;
  if (!file && !input) return [];
  const output = input && attempt?.result === 'succeeded' ? attempt.output_probe : null;
  const expected =
    job?.kind === 'transcode' &&
    job.status !== 'pending' &&
    (ACTIVE_JOB_STATUSES.includes(job.status) || job.status === 'cancelled') &&
    !output;
  const reason = job ? jobReason(job) : null;
  const reasons = reason ? formatReason(reason).split('\n') : [];
  const drops = { audio: 0, subtitle: 0 };
  for (const match of (reason ?? '').matchAll(
    /\bDrop (audio|subtitle) stream \d+ \([^,]+, [^)]+\): /g,
  )) {
    if (match[1] === 'audio') drops.audio++;
    else drops.subtitle++;
  }
  return metadataRows(
    metadataValues(file, input),
    output ? metadataValues(null, output) : null,
    input ? statsFromProbe(input) : null,
    output ? statsFromProbe(output) : null,
  )
    .filter(({ key }) => !['audio', 'subtitle'].includes(key) || input !== null)
    .map((row) => {
      const type =
        row.key === 'audio' || row.key.startsWith('audio_')
          ? 'audio'
          : row.key === 'subtitle'
            ? 'subtitle'
            : ['video_codec', 'resolution', 'fps', 'video_bit_rate'].includes(row.key)
              ? 'video'
              : null;
      const details = reasons.filter((line) => {
        if (row.key === 'container') return line.startsWith('Change container ');
        if (!type) return false;
        const actions = ['audio', 'subtitle'].includes(row.key)
          ? ['Dropping']
          : ['Dropping', 'Transcoding', 'Retagging'];
        return actions.some((action) => line.startsWith(`${action} ${type} stream`));
      });
      const count = row.key === 'audio' || row.key === 'subtitle' ? drops[row.key] : 0;
      const plannedCount = expected && input && count > 0;
      const after = plannedCount ? String(Number(row.value) - count) : row.after;
      const planned = Boolean(expected && (plannedCount || details.length > 0));
      const cancelled = planned && job?.status === 'cancelled';
      const prefix = planned ? (cancelled ? 'Planned ' : 'Expected ') : '';
      return {
        ...row,
        after,
        color: plannedCount ? 'text-info' : row.color,
        expected: planned,
        tooltip:
          after !== null
            ? [
                `${prefix}${row.label}: ${row.value} → ${after}${cancelled ? ' (cancelled)' : ''}`,
                ...details,
              ].join('\n')
            : planned
              ? [cancelled ? 'Planned changes (cancelled):' : 'Planned changes:', ...details].join(
                  '\n',
                )
              : null,
      };
    });
};

const changeValues = (stats: FileChangeStats) => ({
  size: stats.size == null ? 'Unknown' : formatBytes(stats.size),
  video_codec: stats.video_codec ?? 'No video',
  resolution: stats.width && stats.height ? `${stats.width}×${stats.height}` : 'Unknown',
  duration: stats.duration_s == null ? 'Unknown' : formatTime(stats.duration_s),
  bit_rate: formatBitrate(stats.bit_rate),
  audio: stats.audio == null ? 'Unknown' : String(stats.audio),
  subtitle: stats.subtitle == null ? 'Unknown' : String(stats.subtitle),
});

const fileFields = [
  'size',
  'container',
  'video_codec',
  'resolution',
  'duration',
  'bit_rate',
] as const;

export const fileSnapshotMetadata = (
  file: MediaFileDetail,
  snapshot: FileMediaSnapshot | null = null,
) => {
  const values = metadataValues(snapshot ? null : file, snapshot ? snapshot.probe : file.probe);
  if (!snapshot) {
    const catalog = metadataValues(file, null);
    for (const key of fileFields) values[key] = catalog[key];
  }
  return metadataRows(values, null, null, null);
};

export const fileMetadata = (file: MediaFileRow | MediaFileDetail) => {
  const detail = 'probe' in file ? file : null;
  const input = detail?.last_transcode?.input_probe;
  const output = detail?.last_transcode?.output_probe;
  const current = metadataValues(file, detail?.probe ?? null);
  const catalog = metadataValues(file, null);
  for (const key of fileFields) current[key] = catalog[key];
  const recorded =
    input && output
      ? { before: statsFromProbe(input), after: statsFromProbe(output) }
      : file.last_change;
  const change =
    recorded &&
    recorded.after.size === file.size &&
    recorded.after.video_codec === file.video_codec &&
    recorded.after.width === file.width &&
    recorded.after.height === file.height &&
    recorded.after.duration_s === file.duration_s
      ? recorded
      : null;
  return metadataRows(
    change
      ? input && output
        ? metadataValues(null, input)
        : { ...current, ...changeValues(change.before) }
      : current,
    change
      ? {
          ...(input && output
            ? metadataValues(null, output)
            : { ...current, ...changeValues(change.after) }),
          bit_rate: current.bit_rate,
        }
      : null,
    change?.before ?? null,
    change ? { ...change.after, bit_rate: file.bit_rate } : null,
  ).filter(
    ({ key, value, after }) =>
      fileFields.some((field) => field === key) ||
      (detail && !['audio', 'subtitle'].includes(key)) ||
      ((change || detail?.probe) &&
        ['audio', 'subtitle'].includes(key) &&
        (value !== 'Unknown' || after !== null)),
  );
};
