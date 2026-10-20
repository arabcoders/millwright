import { afterEach, beforeAll, describe, expect, mock, test } from 'bun:test';
import * as realUtils from '../app/utils';

const RESUME_KEY = 'mw_playback_resume';
const storage = new Map<string, string>();

const makeMedia = (currentTime: number, duration: number, seekable?: [number, number]) => {
  const range =
    seekable === undefined
      ? undefined
      : {
          length: 1,
          start: () => seekable[0],
          end: () => seekable[1],
        };
  return {
    currentTime,
    duration,
    ...(range === undefined ? {} : { seekable: range }),
  } as HTMLMediaElement;
};

const calls: string[] = [];
const responses: Array<{ status: number; body: unknown }> = [];
const bodies: Array<Record<string, unknown>> = [];

mock.module('~/utils', () => ({
  ...realUtils,
  request: (url: string, options: { method?: string; body?: string } = {}) => {
    calls.push(`${options.method ?? 'GET'} ${url}`);
    if (options.body) bodies.push(JSON.parse(options.body));
    const response = responses.shift() ?? { status: 200, body: {} };
    return Promise.resolve(
      new Response(JSON.stringify(response.body), {
        status: response.status,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  },
  parse_api_error: async (body: { detail?: string } | null) => body?.detail ?? null,
}));

// The composable module must load after the rewrite plugin and storage stub exist.
Bun.plugin({
  name: 'playback-composable',
  setup(build) {
    build.onLoad({ filter: /usePlayback\.ts$/ }, async (args) => ({
      contents: (await Bun.file(args.path).text()).replaceAll('import.meta.client', 'true'),
      loader: 'ts',
    }));
  },
});
globalThis.localStorage = {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => void storage.set(key, value),
  removeItem: (key: string) => void storage.delete(key),
};
type PlaybackResume = (typeof import('../app/composables/usePlayback'))['playbackResume'];
let openPlayback: typeof import('../app/composables/usePlayback').openPlayback;
let playbackGrantExpiresSoon: typeof import('../app/composables/usePlayback').playbackGrantExpiresSoon;
let clampMediaVolume: PlaybackResume['clampMediaVolume'];
let clampResumeTime: PlaybackResume['clampResumeTime'];
let clearResume: PlaybackResume['clearResume'];
let nearEnd: PlaybackResume['nearEnd'];
let readResume: PlaybackResume['readResume'];
let saveResume: PlaybackResume['saveResume'];

beforeAll(async () => {
  const loaded = await import('../app/composables/usePlayback');
  openPlayback = loaded.openPlayback;
  playbackGrantExpiresSoon = loaded.playbackGrantExpiresSoon;
  clampMediaVolume = loaded.playbackResume.clampMediaVolume;
  clampResumeTime = loaded.playbackResume.clampResumeTime;
  clearResume = loaded.playbackResume.clearResume;
  nearEnd = loaded.playbackResume.nearEnd;
  readResume = loaded.playbackResume.readResume;
  saveResume = loaded.playbackResume.saveResume;
});

afterEach(() => {
  storage.clear();
  calls.length = 0;
  responses.length = 0;
  bodies.length = 0;
});

describe('playback resume store', () => {
  test('saves and reads positions with recency', () => {
    saveResume('7:g1', 93.5, 600);
    expect(readResume('7:g1')).toBe(93.5);
    expect(readResume('7:g2')).toBe(0);
  });

  test('drops positions near the end', () => {
    saveResume('7:g1', 598, 600);
    expect(readResume('7:g1')).toBe(0);
    saveResume('7:g1', 400, 600);
    saveResume('7:g1', 597, 600);
    expect(readResume('7:g1')).toBe(0);
  });

  test('expires positions after 24 hours', () => {
    const entry = { at: Date.now() - 24 * 60 * 60 * 1000 - 1, time: 120 };
    storage.set(RESUME_KEY, JSON.stringify({ '7:g1': entry }));
    expect(readResume('7:g1')).toBe(0);
    expect(storage.get(RESUME_KEY)).toBe('{}');
  });

  test('clears stored positions', () => {
    saveResume('7:g1', 50, 600);
    clearResume('7:g1');
    expect(readResume('7:g1')).toBe(0);
    expect(storage.get(RESUME_KEY)).toBe('{}');
  });

  test('survives a corrupt store', () => {
    storage.set(RESUME_KEY, 'not json');
    expect(readResume('7:g1')).toBe(0);
    saveResume('7:g1', 10, 600);
    expect(readResume('7:g1')).toBe(10);
  });

  test('near end detection and clamping', () => {
    expect(nearEnd(makeMedia(595, 600))).toBeTrue();
    expect(nearEnd(makeMedia(400, 600))).toBeFalse();
    expect(nearEnd(makeMedia(0, 0))).toBeFalse();
    expect(nearEnd(null)).toBeFalse();

    expect(clampResumeTime(makeMedia(0, 600), 700)).toBe(599.75);
    expect(clampResumeTime(makeMedia(0, 600, [10, 500]), 700)).toBe(500);
    expect(clampResumeTime(makeMedia(0, 600, [10, 500]), 5)).toBe(10);
    expect(clampResumeTime(makeMedia(0, 600), -3)).toBe(0);
    expect(clampMediaVolume(1.4)).toBe(1);
    expect(clampMediaVolume(-0.2)).toBe(0);
    expect(clampMediaVolume(Number.NaN)).toBe(1);
  });
});

describe('playback grant requests', () => {
  test('opens direct sessions and forwards mode and id', async () => {
    responses.push({
      status: 200,
      body: {
        playback_id: 'pl_1',
        expires_at: '2026-01-01T00:00:00Z',
        mode: 'direct',
        media: {
          container: 'mkv',
          mimetype: 'video/x-matroska',
          duration_s: 600,
          has_video: true,
          has_audio: true,
          video_codec: 'hevc',
          video_profile: 'Main 10',
          video_pix_fmt: 'yuv420p10le',
          width: 1920,
          height: 1080,
        },
        subtitles: [],
        fonts: [],
        audio_tracks: [],
        audio_stream_index: 1,
        subtitle_track_id: null,
        urls: {
          media: '/api/playback/pl_1/media',
          master: '/api/playback/pl_1/master.m3u8',
          poster: '/api/playback/pl_1/poster',
        },
      },
    });
    const payload = await openPlayback(42);
    expect(calls).toEqual(['POST /api/files/42/playback']);
    expect(bodies).toEqual([
      { mode: 'direct', playback_id: null, audio_stream_index: null, subtitle_track_id: null },
    ]);
    expect(payload.playback_id).toBe('pl_1');

    await openPlayback(42, { mode: 'hls', playbackId: 'pl_1', audioStreamIndex: 3 });
    expect(bodies[1]).toEqual({
      mode: 'hls',
      playback_id: 'pl_1',
      audio_stream_index: 3,
      subtitle_track_id: null,
    });
    await openPlayback(42, { mode: 'hls', playbackId: 'pl_1', audioStreamIndex: 0 });
    expect(bodies[2]).toEqual({
      mode: 'hls',
      playback_id: 'pl_1',
      audio_stream_index: 0,
      subtitle_track_id: null,
    });
    await openPlayback(42, {
      mode: 'hls',
      playbackId: 'pl_1',
      audioStreamIndex: 0,
      subtitleTrackId: 'e3',
    });
    expect(bodies[3]).toEqual({
      mode: 'hls',
      playback_id: 'pl_1',
      audio_stream_index: 0,
      subtitle_track_id: 'e3',
    });
    await openPlayback(42, {
      mode: 'hls',
      playbackId: 'pl_1',
      audioStreamIndex: 0,
      subtitleTrackId: null,
    });
    expect(bodies[4]).toEqual({
      mode: 'hls',
      playback_id: 'pl_1',
      audio_stream_index: 0,
      subtitle_track_id: null,
    });
  });

  test('reports API errors with detail', async () => {
    responses.push({ status: 409, body: { detail: 'file changed since the last scan' } });
    await expect(openPlayback(9)).rejects.toThrow('file changed since the last scan');
  });

  test('detects grants needing refresh', () => {
    const payload = {
      generation: { inode: 1 },
      playback_id: 'pl_1',
      expires_at: new Date(Date.now() + 30 * 1000).toISOString(),
      mode: 'direct' as const,
      media: {
        container: 'mkv',
        mimetype: 'video/x-matroska',
        duration_s: 600,
        has_video: true,
        has_audio: true,
        video_codec: 'hevc',
        video_profile: 'Main 10',
        video_pix_fmt: 'yuv420p10le',
        width: 1920,
        height: 1080,
      },
      subtitles: [],
      fonts: [],
      audio_tracks: [],
      audio_stream_index: null,
      subtitle_track_id: null,
      urls: { media: '', master: '', poster: '' },
    };
    expect(playbackGrantExpiresSoon(payload)).toBeTrue();
    expect(
      playbackGrantExpiresSoon({
        ...payload,
        expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString(),
      }),
    ).toBeFalse();
    expect(playbackGrantExpiresSoon({ ...payload, expires_at: 'not-a-date' })).toBeFalse();
  });
});
