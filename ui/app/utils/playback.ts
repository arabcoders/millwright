import type { PlaybackFont, PlaybackPayload } from '~/composables/usePlayback';
import { uri } from '~/utils';

type DiagnosticVideo = Pick<
  HTMLVideoElement,
  | 'currentSrc'
  | 'currentTime'
  | 'duration'
  | 'paused'
  | 'ended'
  | 'seeking'
  | 'readyState'
  | 'networkState'
  | 'videoWidth'
  | 'videoHeight'
  | 'playbackRate'
  | 'muted'
  | 'volume'
  | 'error'
  | 'buffered'
  | 'seekable'
> & {
  getBoundingClientRect: () => { width: number; height: number };
  getVideoPlaybackQuality?: () => {
    totalVideoFrames: number;
    droppedVideoFrames: number;
    corruptedVideoFrames?: number;
  };
  webkitDecodedFrameCount?: number;
  webkitDroppedFrameCount?: number;
  webkitAudioDecodedByteCount?: number;
  audioTracks?: ArrayLike<{ id: string; label: string; language: string; enabled: boolean }>;
};

export function playbackMediaSnapshot(video: DiagnosticVideo | null) {
  if (!video) return null;
  const quality = video.getVideoPlaybackQuality?.();
  const bounds = video.getBoundingClientRect();
  const ranges = (range: TimeRanges) =>
    Array.from({ length: range.length }, (_, index) => [range.start(index), range.end(index)]);
  return {
    current_src: video.currentSrc,
    current_time: video.currentTime,
    duration: Number.isFinite(video.duration) ? video.duration : null,
    paused: video.paused,
    ended: video.ended,
    seeking: video.seeking,
    ready_state: [
      'HAVE_NOTHING',
      'HAVE_METADATA',
      'HAVE_CURRENT_DATA',
      'HAVE_FUTURE_DATA',
      'HAVE_ENOUGH_DATA',
    ][video.readyState],
    network_state: ['NETWORK_EMPTY', 'NETWORK_IDLE', 'NETWORK_LOADING', 'NETWORK_NO_SOURCE'][
      video.networkState
    ],
    video_width: video.videoWidth,
    video_height: video.videoHeight,
    display_width: bounds.width,
    display_height: bounds.height,
    playback_rate: video.playbackRate,
    muted: video.muted,
    volume: video.volume,
    buffered: ranges(video.buffered),
    seekable: ranges(video.seekable),
    frames: quality
      ? {
          total: quality.totalVideoFrames,
          dropped: quality.droppedVideoFrames,
          corrupted: quality.corruptedVideoFrames ?? null,
        }
      : null,
    webkit_decoded_frames: video.webkitDecodedFrameCount ?? null,
    webkit_dropped_frames: video.webkitDroppedFrameCount ?? null,
    webkit_audio_decoded_bytes: video.webkitAudioDecodedByteCount ?? null,
    browser_audio_tracks: video.audioTracks
      ? Array.from(video.audioTracks, (track) => ({
          id: track.id,
          label: track.label,
          language: track.language,
          enabled: track.enabled,
        }))
      : null,
    error: video.error ? { code: video.error.code, message: video.error.message } : null,
  };
}

export function serializePlaybackDiagnostics(report: unknown): string {
  return JSON.stringify(report, null, 2).replace(/pl_[A-Za-z0-9_-]+/g, 'pl_REDACTED');
}

export function playbackVideoMissing(
  video: Pick<
    HTMLVideoElement,
    'readyState' | 'videoWidth' | 'videoHeight' | 'paused' | 'seeking' | 'currentTime'
  > | null,
  hasVideo: boolean,
): boolean {
  return Boolean(
    hasVideo &&
    video &&
    video.readyState >= 2 &&
    !video.paused &&
    !video.seeking &&
    video.currentTime > 0 &&
    (video.videoWidth === 0 || video.videoHeight === 0),
  );
}

export function playbackDiagnosticDisplay(report: {
  transport: string;
  source: PlaybackPayload['media'] | null;
  media: ReturnType<typeof playbackMediaSnapshot>;
  audio_tracks: PlaybackPayload['audio_tracks'];
  requested_hls_audio_stream: number | null;
  browser: { user_agent: string; vendor: string; secure_context: boolean; visibility: string };
  errors: { playback: string; subtitle: string; renewal: string };
  events: Array<{ elapsed_ms: number; event: string; details?: Record<string, unknown> }>;
}) {
  const { media, source } = report;
  const count = (value: number | null | undefined) =>
    value == null ? 'Not reported' : value.toLocaleString();
  const dimensions = (width: number | null | undefined, height: number | null | undefined) =>
    width == null || height == null ? 'Not reported' : `${width} × ${height}`;
  const readyLabels: Record<string, string> = {
    HAVE_NOTHING: 'No media loaded',
    HAVE_METADATA: 'Metadata loaded',
    HAVE_CURRENT_DATA: 'Current data available',
    HAVE_FUTURE_DATA: 'Buffered ahead',
    HAVE_ENOUGH_DATA: 'Enough data to play',
  };
  const networkLabels: Record<string, string> = {
    NETWORK_EMPTY: 'Not started',
    NETWORK_IDLE: 'Idle',
    NETWORK_LOADING: 'Loading',
    NETWORK_NO_SOURCE: 'No playable source',
  };
  const selectedAudio = report.audio_tracks.find(
    (track) => track.stream_index === report.requested_hls_audio_stream,
  );
  const browserAudio = media?.browser_audio_tracks?.filter((track) => track.enabled);
  let audio = 'Not reported';
  if (source && !source.has_audio) {
    audio = 'No audio';
  } else if (source && report.transport === 'direct') {
    audio = browserAudio?.length
      ? browserAudio.map((track) => track.label || track.language || track.id).join(', ')
      : 'Browser-selected (not reported)';
  } else if (source && selectedAudio) {
    audio = [selectedAudio.name, selectedAudio.lang, selectedAudio.codec.toUpperCase()].join(' · ');
  }
  const sections = [
    {
      title: 'Playback',
      rows: [
        {
          label: 'Delivery',
          value:
            report.transport === 'direct'
              ? 'Direct'
              : report.transport === 'native-hls'
                ? 'HLS (browser)'
                : 'HLS (hls.js)',
        },
        {
          label: 'State',
          value: !media
            ? 'Unavailable'
            : media.ended
              ? 'Ended'
              : media.seeking
                ? 'Seeking'
                : media.paused
                  ? 'Paused'
                  : 'Playing',
        },
        {
          label: 'Position',
          value: media
            ? `${media.current_time.toFixed(1)} s / ${media.duration == null ? 'Unknown duration' : `${media.duration.toFixed(1)} s`}`
            : 'Not reported',
        },
        { label: 'Speed', value: media ? `${media.playback_rate}×` : 'Not reported' },
        {
          label: 'Volume',
          value: media
            ? media.muted
              ? 'Muted'
              : `${Math.round(media.volume * 100)}%`
            : 'Not reported',
        },
        { label: 'Audio track', value: audio },
        { label: 'Buffer', value: readyLabels[media?.ready_state ?? ''] ?? 'Not reported' },
        { label: 'Network', value: networkLabels[media?.network_state ?? ''] ?? 'Not reported' },
      ],
    },
    {
      title: 'Video',
      rows: [
        { label: 'Source codec', value: source?.video_codec?.toUpperCase() ?? 'Not reported' },
        { label: 'Profile', value: source?.video_profile ?? 'Not reported' },
        { label: 'Pixel format', value: source?.video_pix_fmt ?? 'Not reported' },
        { label: 'Source resolution', value: dimensions(source?.width, source?.height) },
        { label: 'Browser video size', value: dimensions(media?.video_width, media?.video_height) },
        { label: 'Player size', value: dimensions(media?.display_width, media?.display_height) },
        { label: 'Frames processed', value: count(media?.frames?.total) },
        {
          label: 'Frames dropped',
          value: count(media?.frames?.dropped ?? media?.webkit_dropped_frames),
        },
        { label: 'Frames decoded', value: count(media?.webkit_decoded_frames) },
        {
          label: 'Audio decoded',
          value:
            media?.webkit_audio_decoded_bytes == null
              ? 'Not reported'
              : `${count(media.webkit_audio_decoded_bytes)} bytes`,
        },
      ],
    },
    {
      title: 'Browser',
      rows: [
        { label: 'User agent', value: report.browser.user_agent },
        { label: 'Vendor', value: report.browser.vendor || 'Not reported' },
        { label: 'Secure context', value: report.browser.secure_context ? 'Yes' : 'No' },
        { label: 'Tab', value: report.browser.visibility === 'visible' ? 'Visible' : 'Hidden' },
      ],
    },
  ];
  const issues: Array<{ title: string; message: string; color: 'warning' | 'error' }> = [];
  const hlsSuggestion = report.transport === 'direct' ? ' Try HLS playback.' : '';
  if (source?.has_video && media && !media.paused && !media.seeking && media.current_time > 1) {
    if (media.video_width === 0 || media.video_height === 0) {
      issues.push({
        title: 'No video size reported',
        message: `The file has video, but the browser reports no video dimensions.${hlsSuggestion}`,
        color: 'warning',
      });
    } else if ((media.webkit_decoded_frames ?? media.frames?.total) === 0) {
      issues.push({
        title: 'No video frames reported',
        message: `Playback has advanced, but the browser reports zero video frames.${hlsSuggestion}`,
        color: 'warning',
      });
    }
  }
  const errors: Array<[string, string]> = [
    ['Playback error', report.errors.playback],
    ['Subtitle error', report.errors.subtitle],
    ['Session renewal error', report.errors.renewal],
    [
      'Browser media error',
      media?.error ? media.error.message || `Media error ${media.error.code}` : '',
    ],
  ];
  for (const [title, message] of errors) {
    if (message) issues.push({ title, message, color: 'error' });
  }
  const eventLabels: Record<string, string> = {
    loadedmetadata: 'Metadata loaded',
    loadeddata: 'Current media data loaded',
    canplay: 'Ready to play',
    playing: 'Playback started',
    pause: 'Playback paused',
    waiting: 'Waiting for data',
    stalled: 'Download stalled',
    error: 'Media error',
    emptied: 'Source cleared',
    seeking: 'Seek started',
    seeked: 'Seek completed',
    ended: 'Playback ended',
    'source-error': 'Source error',
    'hls-switch': 'Switching to HLS',
    'hls-error': 'HLS error',
    'missing-video': 'Playback advanced without video',
  };
  return {
    sections,
    issues: issues.map((issue) => ({
      ...issue,
      message: issue.message.replace(/pl_[A-Za-z0-9_-]+/g, 'pl_REDACTED'),
    })),
    events: [...report.events].reverse().map((event) => ({
      time: `${(event.elapsed_ms / 1000).toFixed(1)} s`,
      label: eventLabels[event.event] ?? event.event,
      message:
        typeof event.details?.message === 'string'
          ? event.details.message.replace(/pl_[A-Za-z0-9_-]+/g, 'pl_REDACTED')
          : '',
    })),
  };
}

export function handlePlaybackKey(
  event: KeyboardEvent,
  video: HTMLVideoElement,
  actions: { fullscreen: () => void; captions: () => void; help: () => void },
): boolean {
  if (event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return false;
  const target = event.target;
  if (
    target &&
    target instanceof Element &&
    target.closest(
      'input,textarea,select,button,a,[contenteditable=""],[contenteditable="true"],[role="menu"],[role="listbox"],[data-player-help],[data-player-diagnostics]',
    )
  )
    return false;
  const key = event.key.toLowerCase();
  const seek = (time: number): void => {
    if (Number.isFinite(video.duration))
      video.currentTime = Math.max(0, Math.min(time, video.duration));
  };
  switch (key) {
    case ' ':
    case 'k':
      if (video.paused) void video.play().catch(() => {});
      else video.pause();
      break;
    case 'j':
      seek(video.currentTime - 10);
      break;
    case 'l':
      seek(video.currentTime + 10);
      break;
    case 'arrowleft':
      seek(video.currentTime - 5);
      break;
    case 'arrowright':
      seek(video.currentTime + 5);
      break;
    case 'home':
      seek(0);
      break;
    case 'end':
      seek(video.duration);
      break;
    case 'arrowup':
      video.volume = Math.min(1, video.volume + 0.1);
      break;
    case 'arrowdown':
      video.volume = Math.max(0, video.volume - 0.1);
      break;
    case 'm':
      video.muted = !video.muted;
      break;
    case ';':
      video.playbackRate = Math.max(0.25, video.playbackRate - 0.25);
      break;
    case "'":
      video.playbackRate = Math.min(4, video.playbackRate + 0.25);
      break;
    case ',':
    case '.':
      video.pause();
      seek(video.currentTime + (key === ',' ? -1 : 1) / 30);
      break;
    case 'f':
      actions.fullscreen();
      break;
    case 'c':
      actions.captions();
      break;
    case '?':
    case '/':
      actions.help();
      break;
    default:
      if (!/^[0-9]$/.test(key)) return false;
      seek((video.duration * Number(key)) / 10);
  }
  event.preventDefault();
  event.stopPropagation();
  return true;
}

export async function loadPlaybackFonts(
  fonts: PlaybackFont[],
  signal: AbortSignal,
): Promise<() => void> {
  const loaded: FontFace[] = [];
  const release = (): void => {
    for (const face of loaded) document.fonts.delete(face);
  };
  try {
    for (const font of fonts) {
      const response = await fetch(uri(font.url), { signal });
      if (!response.ok) throw new Error('Embedded subtitle font could not be loaded.');
      const metadata: { families: string[]; weight: string; style: string } = JSON.parse(
        response.headers.get('X-Millwright-Font') || 'null',
      );
      if (!metadata?.families?.length)
        throw new Error('Embedded subtitle font has no family metadata.');
      const data = await response.arrayBuffer();
      for (const family of metadata.families) {
        const face = new FontFace(family, data, { weight: metadata.weight, style: metadata.style });
        await face.load();
        signal.throwIfAborted();
        document.fonts.add(face);
        loaded.push(face);
      }
    }
    return release;
  } catch (cause) {
    release();
    throw cause;
  }
}
