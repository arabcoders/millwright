import { parse_api_error, request } from '~/utils';

export type PlaybackRenderer = 'native' | 'assjs' | 'bitmap' | 'unsupported';

export type PlaybackSubtitleTrack = {
  id: string;
  lang: string;
  name: string;
  source_format: string;
  delivery_format: string;
  renderer: PlaybackRenderer;
  url?: string;
};

export type PlaybackFont = {
  id: string;
  family: string;
  mimetype: string;
  url: string;
};

export type PlaybackAudioTrack = {
  stream_index: number;
  lang: string;
  name: string;
  codec: string;
  channels: number | null;
  channel_layout: string;
  default: boolean;
};

export type PlaybackPayload = {
  generation: Record<string, number>;
  playback_id: string;
  expires_at: string;
  mode: 'direct' | 'hls';
  media: {
    container: string;
    mimetype: string;
    duration_s: number;
    has_video: boolean;
    has_audio: boolean;
    video_codec: string | null;
    video_profile: string | null;
    video_pix_fmt: string | null;
    width: number | null;
    height: number | null;
  };
  subtitles: PlaybackSubtitleTrack[];
  fonts: PlaybackFont[];
  audio_tracks: PlaybackAudioTrack[];
  audio_stream_index: number | null;
  subtitle_track_id: string | null;
  urls: {
    media: string;
    master: string;
    poster: string;
  };
};

const RESUME_KEY = 'mw_playback_resume';
const RESUME_TTL_MS = 24 * 60 * 60 * 1000;
const NEAR_END_PAD_S = 5;

type ResumeEntry = { at: number; time: number };

const clampMediaVolume = (volume: number): number =>
  Number.isFinite(volume) ? Math.min(1, Math.max(0, volume)) : 1;

const nearEnd = (target: Pick<HTMLMediaElement, 'currentTime' | 'duration'> | null): boolean => {
  if (!target) return false;
  const duration = target.duration;
  if (!Number.isFinite(duration) || duration <= 0) return false;
  const time =
    Number.isFinite(target.currentTime) && target.currentTime > 0 ? target.currentTime : 0;
  return duration - time <= NEAR_END_PAD_S;
};

const clampResumeTime = (
  target: Pick<HTMLMediaElement, 'duration' | 'seekable'>,
  time: number,
): number => {
  if (!Number.isFinite(time) || time <= 0) return 0;
  const seekable = target.seekable;
  if (seekable && seekable.length > 0) {
    const last = seekable.length - 1;
    const start = seekable.start(last);
    const end = seekable.end(last);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return 0;
    return Math.min(Math.max(time, start), end);
  }
  const duration = target.duration;
  if (Number.isFinite(duration) && duration > 0)
    return Math.min(time, Math.max(duration - 0.25, 0));
  return time;
};

type ResumeStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

const resumeStorage = (): ResumeStorage | null => {
  if (!import.meta.client) return null;
  try {
    return localStorage;
  } catch {
    return null;
  }
};

const readResumeMap = (storage: ResumeStorage): Record<string, ResumeEntry> => {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(RESUME_KEY) || '{}');
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, ResumeEntry>) : {};
  } catch {
    return {};
  }
};

const readResume = (key: string): number => {
  const storage = resumeStorage();
  if (!storage) return 0;
  const entry = readResumeMap(storage)[key];
  if (!entry || !Number.isFinite(entry.at) || !Number.isFinite(entry.time) || entry.time <= 0)
    return 0;
  if (Date.now() - entry.at > RESUME_TTL_MS) {
    clearResume(key);
    return 0;
  }
  return entry.time;
};

const saveResume = (key: string, time: number, duration: number): void => {
  const storage = resumeStorage();
  if (!storage || !Number.isFinite(time) || time <= 0) return;
  if (Number.isFinite(duration) && duration > 0 && duration - time <= NEAR_END_PAD_S) {
    clearResume(key);
    return;
  }
  const now = Date.now();
  const map = Object.fromEntries(
    Object.entries(readResumeMap(storage)).filter(
      ([, entry]) => entry && Number.isFinite(entry.at) && now - entry.at <= RESUME_TTL_MS,
    ),
  );
  map[key] = { at: now, time };
  try {
    storage.setItem(RESUME_KEY, JSON.stringify(map));
  } catch {
    // A full or blocked store drops the position. Playback continues.
  }
};

const clearResume = (key: string): void => {
  const storage = resumeStorage();
  if (!storage) return;
  const map = readResumeMap(storage);
  if (!(key in map)) return;
  const next: Record<string, ResumeEntry> = {};
  for (const entry of Object.keys(map)) {
    if (entry !== key) next[entry] = map[entry]!;
  }
  try {
    storage.setItem(RESUME_KEY, JSON.stringify(next));
  } catch {
    // Failed removal leaves the entry to expire by TTL.
  }
};

export const openPlayback = async (
  fileId: number,
  options: {
    mode?: 'direct' | 'hls';
    playbackId?: string;
    audioStreamIndex?: number | null;
    subtitleTrackId?: string | null;
  } = {},
): Promise<PlaybackPayload> => {
  const response = await request(`/api/files/${fileId}/playback`, {
    method: 'POST',
    body: JSON.stringify({
      mode: options.mode ?? 'direct',
      playback_id: options.playbackId ?? null,
      audio_stream_index: options.audioStreamIndex ?? null,
      subtitle_track_id: options.subtitleTrackId ?? null,
    }),
  });
  if (!response.ok) {
    const detail = await parse_api_error(
      (await response.json().catch(() => null)) as { detail?: string } | null,
    );
    throw new Error(detail || `Could not open playback (HTTP ${response.status}).`);
  }
  return (await response.json()) as PlaybackPayload;
};

export const closePlayback = (playbackId: string): void => {
  void request(`/api/playback/${encodeURIComponent(playbackId)}/`, { method: 'DELETE' }).catch(
    () => {
      // The server drops the grant by TTL even if the close request fails.
    },
  );
};

export const playbackGrantExpiresSoon = (
  payload: PlaybackPayload,
  leadS: number = 120,
): boolean => {
  const expiresAt = Date.parse(payload.expires_at);
  if (!Number.isFinite(expiresAt)) return false;
  return expiresAt - Date.now() <= leadS * 1000;
};

export const usePlayback = () => ({
  openPlayback,
  closePlayback,
  playbackGrantExpiresSoon,
});

export const playbackResume = {
  key: (fileId: number, generation: Record<string, number>): string =>
    `${fileId}:${JSON.stringify(
      Object.keys(generation)
        .sort()
        .map((name) => [name, generation[name]]),
    )}`,
  clampMediaVolume,
  clampResumeTime,
  clearResume,
  nearEnd,
  readResume,
  saveResume,
};
