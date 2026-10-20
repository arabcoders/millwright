import { afterEach, expect, mock, test } from 'bun:test';
import {
  handlePlaybackKey,
  loadPlaybackFonts,
  playbackMediaSnapshot,
  playbackVideoMissing,
  playbackDiagnosticDisplay,
  serializePlaybackDiagnostics,
} from '../app/utils/playback';

const descriptors = new Map<string, PropertyDescriptor | undefined>();
function replaceGlobal(name: string, value: unknown): void {
  descriptors.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
}
afterEach(() => {
  for (const [name, descriptor] of descriptors) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else Reflect.deleteProperty(globalThis, name);
  }
  descriptors.clear();
});

test('keyboard playback controls', () => {
  const video = {
    currentTime: 50,
    duration: 100,
    volume: 0.5,
    muted: false,
    playbackRate: 1,
    paused: true,
    play: mock(() => Promise.resolve()),
    pause: mock(() => {}),
  } as unknown as HTMLVideoElement;
  const actions = { fullscreen: mock(() => {}), captions: mock(() => {}), help: mock(() => {}) };
  function press(key: string, ctrlKey = false): boolean {
    return handlePlaybackKey(
      {
        key,
        ctrlKey,
        target: null,
        preventDefault() {},
        stopPropagation() {},
      } as unknown as KeyboardEvent,
      video,
      actions,
    );
  }
  press('j');
  expect(video.currentTime).toBe(40);
  press('ArrowRight');
  expect(video.currentTime).toBe(45);
  press('9');
  expect(video.currentTime).toBe(90);
  press(';');
  expect(video.playbackRate).toBe(0.75);
  press('m');
  expect(video.muted).toBeTrue();
  press('k');
  expect(video.play).toHaveBeenCalledTimes(1);
  press('c');
  press('f');
  press('?');
  expect(actions.captions).toHaveBeenCalledTimes(1);
  expect(actions.fullscreen).toHaveBeenCalledTimes(1);
  expect(actions.help).toHaveBeenCalledTimes(1);
  expect(press('j', true)).toBeFalse();
  expect(video.currentTime).toBe(90);
  press('.');
  expect(video.pause).toHaveBeenCalledTimes(1);
  expect(video.currentTime).toBeCloseTo(90 + 1 / 30);
});

test('captures audio-only playback', () => {
  const range = { length: 1, start: () => 0, end: () => 30 };
  const video = {
    currentSrc: '/api/playback/pl_secret/media',
    currentTime: 12,
    duration: 100,
    paused: false,
    ended: false,
    seeking: false,
    readyState: 4,
    networkState: 1,
    videoWidth: 0,
    videoHeight: 0,
    playbackRate: 1,
    volume: 1,
    muted: false,
    error: null,
    buffered: range,
    seekable: range,
    getBoundingClientRect: () => ({ width: 960, height: 540 }),
    getVideoPlaybackQuality: () => ({ totalVideoFrames: 0, droppedVideoFrames: 0 }),
    webkitAudioDecodedByteCount: 2048,
  };
  const snapshot = playbackMediaSnapshot(video);
  expect(snapshot?.current_time).toBe(12);
  expect(snapshot?.ready_state).toBe('HAVE_ENOUGH_DATA');
  expect(snapshot?.network_state).toBe('NETWORK_IDLE');
  expect(snapshot?.video_width).toBe(0);
  expect(snapshot?.frames?.total).toBe(0);
  expect(snapshot?.webkit_audio_decoded_bytes).toBe(2048);
  expect(snapshot?.browser_audio_tracks).toBeNull();
  expect(snapshot?.buffered).toEqual([[0, 30]]);
  expect(snapshot?.display_width).toBe(960);
  const report = {
    transport: 'direct',
    source: {
      container: 'mkv',
      mimetype: 'video/x-matroska',
      duration_s: 100,
      has_video: true,
      has_audio: true,
      video_codec: 'hevc',
      video_profile: 'Main 10',
      video_pix_fmt: 'yuv420p10le',
      width: 1920,
      height: 1080,
    },
    media: snapshot,
    audio_tracks: [
      {
        stream_index: 2,
        name: 'English',
        lang: 'eng',
        codec: 'flac',
        channels: 2,
        channel_layout: 'stereo',
        default: false,
      },
    ],
    requested_hls_audio_stream: 2,
    browser: {
      user_agent: 'Chrome test',
      vendor: 'Google Inc.',
      secure_context: true,
      visibility: 'visible',
    },
    errors: { playback: '', subtitle: '', renewal: '' },
    events: [
      { elapsed_ms: 100, event: 'playing' },
      {
        elapsed_ms: 1000,
        event: 'hls-error',
        details: { message: 'Failed /api/playback/pl_secret/stream.m3u8' },
      },
    ],
  };
  const display = playbackDiagnosticDisplay(report);
  expect(display.issues.map(({ color }) => color)).toEqual(['warning']);
  expect(display.events[0]?.time).toBe('1.0 s');
  expect(display.events[0]?.message).not.toContain('pl_secret');
  expect(report.events[0]?.event).toBe('playing');
  const decoded = playbackMediaSnapshot({
    ...video,
    videoWidth: 1920,
    videoHeight: 1080,
    getVideoPlaybackQuality: undefined,
    webkitDecodedFrameCount: 300,
    audioTracks: [{ id: '2', label: 'English', language: 'eng', enabled: true }],
    error: { code: 3, message: 'Video decode failed' },
  });
  expect(decoded?.frames).toBeNull();
  expect(decoded?.webkit_decoded_frames).toBe(300);
  expect(decoded?.browser_audio_tracks?.[0]?.enabled).toBeTrue();
  expect(decoded?.error).toEqual({ code: 3, message: 'Video decode failed' });
  const hlsDisplay = playbackDiagnosticDisplay({ ...report, transport: 'hls.js', media: decoded });
  expect(
    hlsDisplay.sections.flatMap((section) => section.rows.map(({ value }) => value)),
  ).toContain('English · eng · FLAC');
  expect(hlsDisplay.issues).toMatchObject([{ message: 'Video decode failed', color: 'error' }]);
  const missing = playbackDiagnosticDisplay({ ...report, source: null, media: null, events: [] });
  expect(missing.issues).toEqual([]);
  expect(playbackMediaSnapshot(null)).toBeNull();
});

test('redacts playback credentials', () => {
  const report = serializePlaybackDiagnostics({
    media: { current_src: '/api/playback/pl_secret/segments/1.ts?audio_stream_index=2' },
    events: [
      {
        details: {
          message: 'Failed to load https://server/api/playback/pl_another-token/master.m3u8',
        },
      },
    ],
    frames: { total: 0 },
  });
  expect(report).not.toContain('pl_secret');
  expect(report).not.toContain('pl_another-token');
  expect(JSON.parse(report).media.current_src).toBe(
    '/api/playback/pl_REDACTED/segments/1.ts?audio_stream_index=2',
  );
  expect(JSON.parse(report).frames.total).toBe(0);
});

test('detects missing video', () => {
  const video = {
    readyState: 4,
    paused: false,
    seeking: false,
    currentTime: 663.3,
    videoWidth: 0,
    videoHeight: 0,
  };
  expect(playbackVideoMissing(video, true)).toBeTrue();
  expect(playbackVideoMissing(video, false)).toBeFalse();
  expect(playbackVideoMissing(null, true)).toBeFalse();
  expect(playbackVideoMissing({ ...video, readyState: 1 }, true)).toBeFalse();
  expect(playbackVideoMissing({ ...video, paused: true }, true)).toBeFalse();
  expect(playbackVideoMissing({ ...video, seeking: true }, true)).toBeFalse();
  expect(playbackVideoMissing({ ...video, currentTime: 0 }, true)).toBeFalse();
  expect(playbackVideoMissing({ ...video, videoWidth: 1920, videoHeight: 1080 }, true)).toBeFalse();
});

test('font registration cleanup', async () => {
  replaceGlobal('useRuntimeConfig', () => ({ app: { baseURL: '/' } }));
  const faces = new Set();
  const events: string[] = [];
  replaceGlobal('document', {
    fonts: {
      add: (face: unknown) => {
        events.push('add');
        faces.add(face);
      },
      delete: (face: unknown) => faces.delete(face),
    },
  });
  replaceGlobal(
    'FontFace',
    class {
      constructor(
        readonly family: string,
        readonly data: ArrayBuffer,
        readonly descriptors: FontFaceDescriptors,
      ) {}
      async load() {
        events.push('load');
        return this;
      }
    },
  );
  replaceGlobal(
    'fetch',
    async () =>
      new Response('font', {
        headers: {
          'X-Millwright-Font': JSON.stringify({
            families: ['Actual Family'],
            weight: '700',
            style: 'italic',
          }),
        },
      }),
  );
  const release = await loadPlaybackFonts(
    [{ id: 'f1', family: 'attachment', mimetype: 'font/ttf', url: '/api/playback/pl_1/fonts/f1' }],
    new AbortController().signal,
  );
  expect(events).toEqual(['load', 'add']);
  expect(faces.size).toBe(1);
  release();
  expect(faces.size).toBe(0);
});

test('cancel late fonts', async () => {
  replaceGlobal('useRuntimeConfig', () => ({ app: { baseURL: '/' } }));
  const controller = new AbortController();
  const add = mock(() => {});
  replaceGlobal('document', { fonts: { add, delete: mock(() => {}) } });
  replaceGlobal(
    'FontFace',
    class {
      async load() {
        controller.abort();
        return this;
      }
    },
  );
  replaceGlobal(
    'fetch',
    async () =>
      new Response('font', {
        headers: {
          'X-Millwright-Font': JSON.stringify({
            families: ['Family'],
            weight: '400',
            style: 'normal',
          }),
        },
      }),
  );
  await expect(
    loadPlaybackFonts(
      [{ id: 'f1', family: 'attachment', mimetype: 'font/ttf', url: '/fonts/f1' }],
      controller.signal,
    ),
  ).rejects.toThrow();
  expect(add).not.toHaveBeenCalled();
});
