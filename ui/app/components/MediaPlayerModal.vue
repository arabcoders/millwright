<template>
  <UModal
    v-model:open="open"
    :title="title"
    :ui="{
      content: 'w-[calc(100vw-1rem)] max-w-5xl',
      body: mediaActive ? 'p-0 sm:p-0' : '',
    }"
    @after:leave="emit('after:leave')"
  >
    <template #body>
      <div
        v-if="loading"
        class="flex items-center justify-center py-16"
        aria-label="Loading playback"
      >
        <UIcon name="i-lucide-loader-circle" class="size-10 animate-spin text-toned" />
      </div>
      <div v-else-if="error" class="p-4">
        <UAlert
          color="error"
          variant="soft"
          icon="i-lucide-triangle-alert"
          title="Playback unavailable"
          :description="error"
        />
        <UButton
          class="mt-3"
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-copy"
          @click="copyDiagnostics"
          >Copy playback diagnostics</UButton
        >
      </div>
      <div
        v-else-if="payload"
        ref="playerContainer"
        tabindex="0"
        class="relative flex w-full items-center justify-center overflow-hidden rounded-sm bg-black"
        :class="
          isFullscreen
            ? 'h-screen w-screen max-h-screen max-w-none rounded-none'
            : 'aspect-video max-h-[72vh] min-h-72'
        "
        @pointermove="handlePointerMove"
        @keydown="handleKeyDown"
      >
        <div
          v-if="!active"
          class="absolute inset-0 z-40 flex flex-col justify-end overflow-hidden bg-black"
        >
          <button
            type="button"
            class="group absolute inset-0 block overflow-hidden bg-black text-start"
            :aria-label="`Play ${title}`"
            :disabled="switchingToHls"
            @click="activatePlayer"
          >
            <img
              v-if="posterUrl"
              :src="posterUrl"
              :alt="`${title} preview`"
              class="block h-full w-full bg-black object-contain opacity-90 transition duration-200 group-hover:opacity-100"
              @error="posterFailed = true"
            />
            <div
              v-else
              class="flex h-full w-full items-center justify-center bg-black/90 text-white/80"
            >
              <UIcon :name="isAudio ? 'i-lucide-disc-3' : 'i-lucide-film'" class="size-12" />
            </div>
            <div
              class="pointer-events-none absolute inset-0 bg-linear-to-t from-black/70 via-transparent to-black/20"
            />
          </button>
          <div class="pointer-events-none relative min-h-0 space-y-3 overflow-y-auto p-4 sm:p-6">
            <div
              v-if="payload.audio_tracks.length || hasSubtitles"
              class="mw-card-padded pointer-events-auto grid gap-3 text-default sm:grid-cols-2"
            >
              <UFormField v-if="payload.audio_tracks.length" label="Audio" class="min-w-0">
                <USelectMenu
                  v-model="audioSelectValue"
                  :items="audioSelectItems"
                  value-key="value"
                  :disabled="switchingToHls"
                  color="neutral"
                  size="sm"
                  icon="i-lucide-languages"
                  class="w-full"
                  :portal="playerPortal"
                />
              </UFormField>
              <UFormField v-if="hasSubtitles" label="Subtitles" class="min-w-0">
                <USelectMenu
                  v-model="subtitleSelectValue"
                  :items="subtitleSelectItems"
                  value-key="value"
                  :disabled="switchingToHls"
                  color="neutral"
                  size="sm"
                  icon="i-lucide-captions"
                  class="w-full"
                  :portal="playerPortal"
                />
              </UFormField>
            </div>
            <div class="flex items-center justify-between gap-4">
              <div class="min-w-0">
                <div class="text-xs uppercase tracking-[0.2em] text-white/70">Click to play</div>
                <div class="mt-1 truncate text-lg font-semibold text-white">{{ title }}</div>
              </div>
              <UButton
                color="neutral"
                variant="soft"
                size="xl"
                icon="i-lucide-play"
                class="pointer-events-auto shrink-0"
                :loading="switchingToHls"
                @click="activatePlayer"
                >Play</UButton
              >
            </div>
          </div>
        </div>

        <video
          ref="videoElement"
          class="absolute inset-0 block h-full w-full bg-black object-contain"
          playsinline
          webkit-playsinline
          preload="metadata"
          :poster="posterUrl || undefined"
          @error="handleSourceError"
          @loadeddata="syncVideoState"
          @loadedmetadata="handleLoadedMetadata"
          @timeupdate="handleTimeUpdate"
          @play="handlePlay"
          @pause="handlePause"
          @ended="handleEnded"
          @click="handleVideoClick"
          @dblclick="toggleFullscreen"
          @volumechange="handleVolumeChange"
          @ratechange="handleTimeUpdate"
          @seeked="handleTimeUpdate"
        >
          <source
            v-for="source in sources"
            :key="source.src"
            :src="source.src"
            :type="source.type"
            @error="handleSourceError"
          />
          <track
            v-if="nativeSubtitleTrack && subtitleEnabled"
            ref="nativeTrackElement"
            :key="nativeSubtitleTrack.url"
            kind="subtitles"
            :srclang="nativeSubtitleTrack.lang || 'und'"
            :label="nativeSubtitleTrack.name || 'Subtitles'"
            default
            :src="uri(nativeSubtitleTrack.url || '')"
            @load="() => void restoreNativeTextTrack()"
            @error="subtitleError = 'The selected subtitle track could not be loaded.'"
          />
          Your browser does not support video playback.
        </video>

        <button
          v-if="active && isTouchDevice && !controlsVisible"
          type="button"
          class="absolute inset-0 z-10"
          aria-label="Show controls"
          @click="showControls"
        />

        <div
          v-if="usesAssSubtitleTrack"
          ref="assOverlayElement"
          class="pointer-events-none absolute inset-0 z-20 overflow-hidden"
          aria-hidden="true"
        />

        <div
          v-if="active"
          class="absolute inset-x-0 bottom-0 z-30 bg-linear-to-t from-black/36 via-black/8 to-transparent px-3 pb-3 pt-10 text-white transition-opacity duration-150"
          :class="controlsVisible ? 'opacity-100' : 'pointer-events-none opacity-0'"
        >
          <div
            class="rounded-sm border border-white/8 bg-black/8 p-2.5 shadow-lg backdrop-blur-sm"
            dir="ltr"
          >
            <p v-if="notice || subtitleError" role="status" class="mb-2 text-xs">
              {{ notice || subtitleError }}
            </p>
            <div
              class="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-2.5 sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-center sm:gap-3"
            >
              <div class="order-2 flex min-w-0 items-center gap-2 sm:order-1">
                <UButton
                  color="neutral"
                  variant="soft"
                  size="sm"
                  class="opacity-65 transition-opacity hover:opacity-100 focus-visible:opacity-100"
                  :icon="paused ? 'i-lucide-play' : 'i-lucide-pause'"
                  :aria-label="paused ? 'Play' : 'Pause'"
                  @click="togglePlayback"
                />
                <div class="min-w-0 truncate whitespace-nowrap text-xs font-medium text-white/60">
                  {{ timeLabel }}
                </div>
              </div>
              <div class="order-1 col-span-2 sm:order-2 sm:col-span-1 sm:min-w-0 sm:flex-1">
                <input
                  :value="progress"
                  type="range"
                  min="0"
                  max="1000"
                  step="1"
                  class="seek-bar h-1.5 w-full accent-white opacity-55 transition-opacity hover:opacity-100"
                  aria-label="Seek"
                  @input="handleSeekInput"
                />
              </div>
              <div class="order-3 flex shrink-0 items-center justify-end gap-1.5 sm:gap-2">
                <UTooltip v-if="!usingHls" side="top" text="Switch to converted HLS playback">
                  <UButton
                    color="neutral"
                    variant="soft"
                    size="sm"
                    class="opacity-65 transition-opacity hover:opacity-100 focus-visible:opacity-100"
                    icon="i-lucide-refresh-cw"
                    aria-label="Switch to HLS playback"
                    :loading="switchingToHls"
                    @click="switchToHls()"
                  />
                </UTooltip>
                <UTooltip text="Playback diagnostics" :portal="playerPortal">
                  <UButton
                    color="neutral"
                    variant="soft"
                    size="sm"
                    class="opacity-65 transition-opacity hover:opacity-100 focus-visible:opacity-100"
                    icon="i-lucide-bug"
                    aria-label="Playback diagnostics"
                    @click="showDiagnostics"
                  />
                </UTooltip>
                <USelectMenu
                  v-if="payload.audio_tracks.length > 1"
                  v-model="audioSelectValue"
                  :items="audioSelectItems"
                  value-key="value"
                  :disabled="switchingToHls"
                  color="neutral"
                  variant="soft"
                  size="sm"
                  class="opacity-65 transition-opacity hover:opacity-100 focus-visible:opacity-100"
                  :aria-label="audioButtonLabel"
                  trailing-icon=""
                  :content="{ align: 'end' }"
                  :ui="trackSelectUi"
                  :portal="playerPortal"
                >
                  <template #default>
                    <span class="sr-only">{{ audioButtonLabel }}</span>
                    <UIcon
                      :name="switchingToHls ? 'i-lucide-loader-circle' : 'i-lucide-languages'"
                      class="size-4 shrink-0"
                      :class="{ 'animate-spin': switchingToHls }"
                    />
                  </template>
                </USelectMenu>
                <USelectMenu
                  v-if="hasSubtitles"
                  v-model="subtitleSelectValue"
                  :items="subtitleSelectItems"
                  value-key="value"
                  :disabled="switchingToHls"
                  color="neutral"
                  variant="soft"
                  size="sm"
                  class="opacity-65 transition-opacity hover:opacity-100 focus-visible:opacity-100"
                  :aria-label="subtitleButtonLabel"
                  trailing-icon=""
                  :content="{ align: 'end' }"
                  :ui="trackSelectUi"
                  :portal="playerPortal"
                >
                  <template #default>
                    <span class="sr-only">{{ subtitleButtonLabel }}</span>
                    <UIcon
                      :name="subtitleEnabled ? 'i-lucide-captions' : 'i-lucide-captions-off'"
                      class="size-4 shrink-0"
                    />
                  </template>
                </USelectMenu>
                <UButton
                  color="neutral"
                  variant="soft"
                  size="sm"
                  class="opacity-65 transition-opacity hover:opacity-100 focus-visible:opacity-100"
                  :icon="muted || mediaVol <= 0 ? 'i-lucide-volume-x' : 'i-lucide-volume-2'"
                  :aria-label="muted || mediaVol <= 0 ? 'Unmute' : 'Mute'"
                  @click="toggleMute"
                />
                <input
                  v-if="!isTouchDevice"
                  :value="Math.round(mediaVol * 100)"
                  type="range"
                  min="0"
                  max="100"
                  step="1"
                  class="w-16 accent-white opacity-55 transition-opacity hover:opacity-100 sm:w-18"
                  aria-label="Volume"
                  @input="handleVolumeInput"
                />
                <UButton
                  color="neutral"
                  variant="soft"
                  size="sm"
                  class="opacity-65 transition-opacity hover:opacity-100 focus-visible:opacity-100"
                  :icon="isFullscreen ? 'i-lucide-minimize' : 'i-lucide-maximize'"
                  :aria-label="isFullscreen ? 'Exit fullscreen' : 'Enter fullscreen'"
                  @click="toggleFullscreen"
                />
                <UTooltip text="Keyboard shortcuts" :portal="playerPortal">
                  <UButton
                    color="neutral"
                    variant="soft"
                    size="sm"
                    class="opacity-65 transition-opacity hover:opacity-100 focus-visible:opacity-100"
                    icon="i-lucide-circle-help"
                    aria-label="Keyboard shortcuts"
                    @click="helpOpen = !helpOpen"
                  />
                </UTooltip>
              </div>
            </div>
          </div>
        </div>
        <section
          v-if="helpOpen"
          data-player-help
          class="mw-card-padded absolute inset-3 z-50 overflow-auto text-default"
          aria-label="Keyboard shortcuts"
          @keydown.esc.stop="helpOpen = false"
        >
          <div class="flex items-center justify-between">
            <h3 class="flex items-center gap-2 font-semibold text-highlighted">
              <UIcon name="i-lucide-keyboard" class="size-4" />Keyboard shortcuts
            </h3>
            <UButton
              color="neutral"
              variant="ghost"
              icon="i-lucide-x"
              aria-label="Close shortcuts"
              @click="helpOpen = false"
            />
          </div>
          <div class="mt-4 grid gap-x-8 gap-y-6 text-sm sm:grid-cols-2">
            <section v-for="group in shortcutGroups" :key="group.title">
              <h4 class="font-semibold text-highlighted">{{ group.title }}</h4>
              <dl class="mt-3 space-y-2">
                <div
                  v-for="shortcut in group.items"
                  :key="shortcut.label"
                  class="flex min-h-7 items-center justify-between gap-4"
                >
                  <dt class="text-toned">{{ shortcut.label }}</dt>
                  <dd class="flex shrink-0 items-center gap-1.5">
                    <template v-for="(key, index) in shortcut.keys" :key="key">
                      <span v-if="index" class="text-dimmed">/</span>
                      <UKbd size="lg" class="h-auto min-h-8 min-w-8 px-2 py-1 text-sm font-bold">{{
                        key
                      }}</UKbd>
                    </template>
                  </dd>
                </div>
              </dl>
            </section>
          </div>
        </section>
        <section
          v-if="diagnosticsOpen"
          data-player-diagnostics
          class="mw-card-padded absolute inset-3 z-50 flex min-h-0 flex-col overflow-hidden text-default"
          aria-label="Playback diagnostics"
          @keydown.esc.stop="diagnosticsOpen = false"
        >
          <div class="flex shrink-0 flex-wrap items-center justify-between gap-2">
            <h3 class="flex items-center gap-2 font-semibold text-highlighted">
              <UIcon name="i-lucide-bug" class="size-4" />Playback diagnostics
            </h3>
            <div class="flex items-center gap-2">
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-refresh-cw"
                @click="captureDiagnostics"
                >Refresh</UButton
              >
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-copy"
                @click="copyDiagnostics"
                >Copy diagnostics</UButton
              >
              <UButton
                color="neutral"
                variant="ghost"
                icon="i-lucide-x"
                aria-label="Close diagnostics"
                @click="diagnosticsOpen = false"
              />
            </div>
          </div>
          <div v-if="diagnosticsDisplay" class="mt-3 min-h-0 flex-1 space-y-4 overflow-y-auto">
            <UAlert
              v-for="(issue, index) in diagnosticsDisplay.issues"
              :key="index"
              :color="issue.color"
              variant="soft"
              icon="i-lucide-triangle-alert"
              :title="issue.title"
              :description="issue.message"
            />
            <section
              v-for="section in diagnosticsDisplay.sections"
              :key="section.title"
              class="mw-frame p-3"
            >
              <h4 class="font-semibold text-highlighted">{{ section.title }}</h4>
              <dl class="mt-3 grid gap-x-6 gap-y-3 sm:grid-cols-2">
                <div
                  v-for="row in section.rows"
                  :key="row.label"
                  :class="{ 'sm:col-span-2': row.label === 'User agent' }"
                >
                  <dt class="text-xs text-toned">{{ row.label }}</dt>
                  <dd class="mt-1 wrap-break-word text-sm font-medium">{{ row.value }}</dd>
                </div>
              </dl>
            </section>
            <section v-if="diagnosticsDisplay.events.length" class="mw-frame p-3">
              <h4 class="font-semibold text-highlighted">Recent events</h4>
              <ol class="mt-2 divide-y divide-default">
                <li
                  v-for="(event, index) in diagnosticsDisplay.events"
                  :key="index"
                  class="flex items-start gap-3 py-2 text-sm"
                >
                  <span class="w-16 shrink-0 text-xs tabular-nums text-toned">{{
                    event.time
                  }}</span>
                  <div class="min-w-0">
                    <p class="font-medium">{{ event.label }}</p>
                    <p v-if="event.message" class="wrap-break-word text-toned">
                      {{ event.message }}
                    </p>
                  </div>
                </li>
              </ol>
            </section>
          </div>
        </section>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import { useStorage } from '@vueuse/core';
import type Hls from 'hls.js';
import type ASS from 'assjs';
import {
  handlePlaybackKey,
  loadPlaybackFonts,
  playbackMediaSnapshot,
  playbackVideoMissing,
  playbackDiagnosticDisplay,
  serializePlaybackDiagnostics,
} from '~/utils/playback';
import {
  closePlayback,
  openPlayback,
  playbackGrantExpiresSoon,
  playbackResume,
  type PlaybackPayload,
  type PlaybackSubtitleTrack,
} from '~/composables/usePlayback';
import { copyText, uri } from '~/utils';

type FullscreenCapableElement = HTMLElement & {
  webkitRequestFullscreen?: () => Promise<void> | void;
};
type FullscreenCapableDocument = Document & {
  webkitFullscreenElement?: Element | null;
  webkitExitFullscreen?: () => Promise<void> | void;
};

const props = defineProps<{ fileId: number; title: string }>();
const emit = defineEmits<{ 'after:leave': [] }>();

const open = defineModel<boolean>('open', { required: true });

const { clampMediaVolume, clampResumeTime, clearResume, nearEnd, readResume, saveResume } =
  playbackResume;

const playerContainer = ref<HTMLElement | null>(null);
const videoElement = ref<HTMLVideoElement | null>(null);
const nativeTrackElement = ref<HTMLTrackElement | null>(null);
const assOverlayElement = ref<HTMLElement | null>(null);
const payload = ref<PlaybackPayload | null>(null);
const loading = ref(true);
const error = ref('');
const subtitleError = ref('');
const notice = ref('');
const helpOpen = ref(false);
const diagnosticsOpen = ref(false);
const diagnosticsReport = shallowRef<ReturnType<typeof captureDiagnostics> | null>(null);
const diagnosticsDisplay = computed(() =>
  diagnosticsReport.value ? playbackDiagnosticDisplay(diagnosticsReport.value) : null,
);
const diagnosticsStartedAt = Date.now();
const playbackEvents: Array<{
  elapsed_ms: number;
  event: string;
  transport: string;
  media: ReturnType<typeof playbackMediaSnapshot>;
  details?: Record<string, unknown>;
}> = [];
const shortcutGroups = [
  {
    title: 'Playback',
    items: [
      { label: 'Play or pause', keys: ['Space', 'K'] },
      { label: 'Back 10 seconds', keys: ['J'] },
      { label: 'Forward 10 seconds', keys: ['L'] },
      { label: 'Mute', keys: ['M'] },
    ],
  },
  {
    title: 'Seeking',
    items: [
      { label: 'Back 5 seconds', keys: ['←'] },
      { label: 'Forward 5 seconds', keys: ['→'] },
      { label: 'Beginning / end', keys: ['Home', 'End'] },
      { label: 'Seek by 10%', keys: ['0–9'] },
    ],
  },
  {
    title: 'Volume and speed',
    items: [
      { label: 'Volume up / down', keys: ['↑', '↓'] },
      { label: 'Slower by 0.25×', keys: [';'] },
      { label: 'Faster by 0.25×', keys: ["'"] },
      { label: 'Step 1/30 second', keys: [',', '.'] },
    ],
  },
  {
    title: 'Display',
    items: [
      { label: 'Fullscreen', keys: ['F'] },
      { label: 'Toggle subtitles', keys: ['C'] },
      { label: 'Shortcut help', keys: ['?', '/'] },
      { label: 'Close help', keys: ['Esc'] },
    ],
  },
];
const active = ref(false);
const destroyed = ref(false);
const isFullscreen = ref(false);
const controlsVisible = ref(true);
const isTouchDevice = ref(false);
const currentTime = ref(0);
const duration = ref(0);
const paused = ref(true);
const usingHls = ref(false);
const switchingToHls = ref(false);
const posterFailed = ref(false);
const posterImage = ref('');
const assLayoutVersion = ref(0);
const mediaVol = useStorage<number>('player_volume', 1);
const muted = useStorage<boolean>('player_muted', false);

let hls: Hls | null = null;
let assRenderer: ASS | null = null;
let subtitleRequest: AbortController | null = null;
let posterRequest: AbortController | null = null;
let releaseFonts: (() => void) | null = null;
let grantRequest: Promise<PlaybackPayload | null> | null = null;
let renewalTimer = 0;
let assRequestId = 0;
let controlsHideTimeout = 0;
let pendingVideoClickTimeout = 0;
let automaticHlsTried = false;
const nativeHlsActive = ref(false);
let hlsJsTried = false;
let pendingSeek: number | null = null;
let pendingPlay = false;
let pendingRate = 1;
let resumeVersion = 0;
let lastSaveAt = 0;
let cachedAssUrl = '';
let cachedAssContent = '';

const hasVideo = computed(() => payload.value?.media.has_video ?? false);
const resumeKey = computed(() =>
  payload.value ? playbackResume.key(props.fileId, payload.value.generation) : '',
);
const isAudio = computed(() => !hasVideo.value);
const mediaActive = computed(() => !loading.value && !error.value && payload.value !== null);
const playerPortal = computed(() => (isFullscreen.value ? playerContainer.value || false : true));
const posterUrl = computed(() => {
  return posterFailed.value ? '' : posterImage.value;
});
const sources = computed(() => {
  if (!payload.value) return [];
  if (usingHls.value) {
    return nativeHlsActive.value
      ? [{ src: uri(payload.value.urls.master), type: 'application/x-mpegURL' }]
      : [];
  }
  return [{ src: uri(payload.value.urls.media), type: payload.value.media.mimetype }];
});
const playbackTransport = computed(() =>
  usingHls.value ? (nativeHlsActive.value ? 'native-hls' : 'hls.js') : 'direct',
);
const progress = computed(() =>
  duration.value ? Math.round((currentTime.value / duration.value) * 1000) : 0,
);
const timeLabel = computed(() => {
  const currentLabel = formatDuration(Math.round(currentTime.value));
  const durationLabel = duration.value ? formatDuration(Math.round(duration.value)) : '--:--';
  return `${currentLabel} / ${durationLabel}`;
});

const playableTracks = computed<PlaybackSubtitleTrack[]>(() =>
  (payload.value?.subtitles ?? []).filter(
    (track) => track.renderer === 'bitmap' || (track.renderer !== 'unsupported' && track.url),
  ),
);
const hasSubtitles = computed(() => (payload.value?.subtitles.length ?? 0) > 0);
const subtitleEnabled = ref(true);
const selectedTrackId = ref<string | null>(null);
watch(
  playableTracks,
  (tracks) => {
    if (!tracks.length) {
      selectedTrackId.value = null;
      subtitleEnabled.value = false;
      return;
    }
    if (!selectedTrackId.value || !tracks.some((track) => track.id === selectedTrackId.value)) {
      selectedTrackId.value = tracks[0]!.id;
      subtitleEnabled.value = true;
    }
  },
  { immediate: true },
);
const selectedTrack = computed(
  () => playableTracks.value.find((track) => track.id === selectedTrackId.value) ?? null,
);
const nativeSubtitleTrack = computed(() =>
  subtitleEnabled.value && selectedTrack.value?.renderer === 'native' ? selectedTrack.value : null,
);
const usesAssSubtitleTrack = computed(
  () => subtitleEnabled.value && selectedTrack.value?.renderer === 'assjs',
);
const bitmapSubtitleId = computed(() =>
  subtitleEnabled.value && selectedTrack.value?.renderer === 'bitmap'
    ? selectedTrack.value.id
    : null,
);
watch(bitmapSubtitleId, (trackId) => {
  if (active.value && trackId !== payload.value?.subtitle_track_id)
    void switchToHls(undefined, trackId);
});

const SUBTITLE_OFF_VALUE = '__off__';
const DIRECT_AUDIO_VALUE = '__direct__';
const splashAudioStreamIndex = ref<number | null>(null);
const trackSelectUi = {
  base: 'size-8 min-h-8 min-w-8 justify-center rounded-md px-0',
  value: 'flex items-center justify-center px-0',
  trailing: 'hidden',
  leading: 'hidden',
  content: 'z-40 min-w-56 w-auto max-w-[min(24rem,calc(100vw-2rem))]',
} as const;
const audioSelectItems = computed(() => [
  ...(!usingHls.value
    ? [{ label: 'Browser-selected audio (Direct)', value: DIRECT_AUDIO_VALUE }]
    : []),
  ...(payload.value?.audio_tracks ?? []).map((track) => ({
    label: [
      track.name,
      track.lang !== 'und' ? track.lang : '',
      track.codec.toUpperCase(),
      track.channel_layout || (track.channels ? `${track.channels} channels` : ''),
      track.default ? 'File default' : '',
    ]
      .filter(Boolean)
      .join(' · '),
    value: track.stream_index,
  })),
]);
const audioButtonLabel = computed(() => {
  if (!usingHls.value)
    return 'Audio track: chosen by the browser. Select a track to use converted HLS playback.';
  const selected = audioSelectItems.value.find(
    (track) => track.value === payload.value?.audio_stream_index,
  );
  return `Audio track: ${selected?.label ?? 'Default'}. Other tracks use converted HLS playback.`;
});
const audioSelectValue = computed<string | number | undefined>({
  get: () => {
    if (!active.value && splashAudioStreamIndex.value !== null) return splashAudioStreamIndex.value;
    return usingHls.value ? (payload.value?.audio_stream_index ?? undefined) : DIRECT_AUDIO_VALUE;
  },
  set: (value) => {
    if (!active.value) {
      splashAudioStreamIndex.value = typeof value === 'number' ? value : null;
      return;
    }
    if (
      typeof value === 'number' &&
      (!usingHls.value || value !== payload.value?.audio_stream_index)
    )
      void switchToHls(value);
  },
});
const subtitleSelectItems = computed(() => [
  { label: 'Subtitles off', value: SUBTITLE_OFF_VALUE },
  ...(payload.value?.subtitles ?? []).map((track) => ({
    label: track.renderer === 'unsupported' ? `${track.name} (unavailable)` : track.name,
    value: track.id,
    disabled: track.renderer === 'unsupported',
  })),
]);
const subtitleButtonLabel = computed(() =>
  subtitleEnabled.value && selectedTrack.value ? selectedTrack.value.name : 'Subtitles off',
);
const subtitleSelectValue = computed<string>({
  get: () =>
    subtitleEnabled.value ? (selectedTrackId.value ?? SUBTITLE_OFF_VALUE) : SUBTITLE_OFF_VALUE,
  set: (value: string) => {
    if (SUBTITLE_OFF_VALUE === value) {
      subtitleEnabled.value = false;
      return;
    }
    selectedTrackId.value = value;
    subtitleEnabled.value = true;
  },
});

watch([mediaVol, muted], () => applyMediaState(videoElement.value), { immediate: true });
watch(videoElement, (element) => applyMediaState(element), { immediate: true });
watch(videoElement, (element, _previous, onCleanup) => {
  if (!element) return;
  const events = [
    'loadedmetadata',
    'loadeddata',
    'canplay',
    'playing',
    'pause',
    'waiting',
    'stalled',
    'error',
    'emptied',
    'seeking',
    'seeked',
    'ended',
  ];
  const record = (event: Event) => recordPlaybackEvent(event.type);
  for (const event of events) element.addEventListener(event, record);
  onCleanup(() => {
    for (const event of events) element.removeEventListener(event, record);
  });
});
watch(
  () => [
    selectedTrack.value?.url ?? '',
    selectedTrack.value?.renderer ?? '',
    subtitleEnabled.value,
    active.value,
    assLayoutVersion.value,
    videoElement.value,
    assOverlayElement.value,
  ],
  () => void syncAssRenderer(),
  { immediate: true },
);
watch(subtitleEnabled, () => void nextTick(restoreNativeTextTrack));

function formatDuration(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return [hours, minutes, seconds].map((value) => String(value).padStart(2, '0')).join(':');
  }
  return [minutes, seconds].map((value) => String(value).padStart(2, '0')).join(':');
}

function recordPlaybackEvent(event: string, details?: Record<string, unknown>): void {
  if (destroyed.value) return;
  playbackEvents.push({
    elapsed_ms: Date.now() - diagnosticsStartedAt,
    event,
    transport: playbackTransport.value,
    media: playbackMediaSnapshot(videoElement.value),
    details,
  });
  if (playbackEvents.length > 40) playbackEvents.shift();
}

function captureDiagnostics() {
  const video = videoElement.value;
  const style = video ? getComputedStyle(video) : null;
  const mimeTypes = [
    payload.value?.media.mimetype,
    'video/mp4; codecs="avc1.640028"',
    'video/mp4; codecs="hvc1.1.6.L120.B0"',
    'video/mp4; codecs="hvc1.2.4.L150.B0"',
    'audio/webm; codecs="opus"',
  ].filter((mime): mime is string => Boolean(mime));
  const report = {
    captured_at: new Date().toISOString(),
    file: { id: props.fileId, name: props.title },
    browser: {
      user_agent: navigator.userAgent,
      vendor: navigator.vendor,
      secure_context: window.isSecureContext,
      visibility: document.visibilityState,
    },
    transport: playbackTransport.value,
    active: active.value,
    fullscreen: isFullscreen.value,
    switching_to_hls: switchingToHls.value,
    automatic_hls_tried: automaticHlsTried,
    source: payload.value?.media ?? null,
    audio_tracks: payload.value?.audio_tracks ?? [],
    requested_hls_audio_stream: payload.value?.audio_stream_index ?? null,
    requested_hls_subtitle_track: payload.value?.subtitle_track_id ?? null,
    direct_audio_selection: usingHls.value
      ? null
      : 'Browser-selected. The file default does not identify the playing stream.',
    subtitle: selectedTrack.value
      ? {
          id: selectedTrack.value.id,
          name: selectedTrack.value.name,
          renderer: selectedTrack.value.renderer,
          enabled: subtitleEnabled.value,
        }
      : null,
    media: playbackMediaSnapshot(video),
    video_style: style
      ? { display: style.display, visibility: style.visibility, opacity: style.opacity }
      : null,
    can_play_type: video
      ? Object.fromEntries(mimeTypes.map((mime) => [mime, video.canPlayType(mime)]))
      : null,
    can_play_type_note:
      'Browser format support does not confirm that the current video stream is decoding.',
    errors: { playback: error.value, subtitle: subtitleError.value, renewal: notice.value },
    events: [...playbackEvents],
  };
  diagnosticsReport.value = report;
  return report;
}

function showDiagnostics(): void {
  helpOpen.value = false;
  captureDiagnostics();
  diagnosticsOpen.value = true;
}

function copyDiagnostics(): void {
  copyText(serializePlaybackDiagnostics(captureDiagnostics()));
}

async function loadPayload(mode: 'direct' | 'hls' = 'direct'): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    await requestPayload(mode);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    loading.value = false;
  }
}

async function loadPoster(): Promise<void> {
  if (!payload.value) return;
  const controller = new AbortController();
  posterRequest = controller;
  try {
    const response = await fetch(uri(payload.value.urls.poster), { signal: controller.signal });
    if (!response.ok) return;
    const image = await response.blob();
    if (destroyed.value) return;
    // Share the loaded image with Media Session instead of extracting it again.
    posterImage.value = URL.createObjectURL(image);
    applyMediaSessionMetadata();
  } catch {
    // Poster failures leave the media icon visible without blocking playback.
  } finally {
    posterRequest = null;
  }
}

async function requestPayload(
  mode: 'direct' | 'hls',
  audioStreamIndex: number | null = payload.value?.audio_stream_index ?? null,
  subtitleTrackId: string | null = payload.value?.subtitle_track_id ?? null,
): Promise<PlaybackPayload | null> {
  if (grantRequest) await grantRequest;
  if (destroyed.value || !open.value) return null;
  const request = openPlayback(props.fileId, {
    mode,
    playbackId: payload.value?.playback_id,
    audioStreamIndex,
    subtitleTrackId,
  }).then((result) => {
    if (destroyed.value || !open.value) {
      closePlayback(result.playback_id);
      return null;
    }
    payload.value = result;
    return result;
  });
  grantRequest = request;
  try {
    return await request;
  } finally {
    if (grantRequest === request) grantRequest = null;
  }
}

function activatePlayer(): void {
  if (active.value || destroyed.value || switchingToHls.value) return;
  resumeVersion += 1;
  active.value = true;
  pendingPlay = true;
  if (pendingSeek !== null) pendingSeek = pendingSeek || readResume(resumeKey.value);
  playerContainer.value?.focus();
  const audioStreamIndex = splashAudioStreamIndex.value;
  if (
    (audioStreamIndex !== null &&
      (!usingHls.value || audioStreamIndex !== payload.value?.audio_stream_index)) ||
    bitmapSubtitleId.value !== payload.value?.subtitle_track_id
  ) {
    void switchToHls(audioStreamIndex ?? payload.value?.audio_stream_index ?? null);
    return;
  }
  void nextTick(async () => {
    if (destroyed.value) return;
    applyMediaState(videoElement.value);
    await restoreResume();
  });
}

function handleLoadedMetadata(): void {
  resumeVersion += 1;
  error.value = '';
  showControls();
  scheduleAssLayoutRefresh();
  updateMediaSessionPosition(videoElement.value);
  if (pendingSeek !== null) {
    if (videoElement.value) videoElement.value.playbackRate = pendingRate;
    const time = pendingSeek;
    const play = pendingPlay;
    pendingSeek = null;
    pendingPlay = false;
    void restorePosition(time, play);
    return;
  }
  void restoreResume();
}

async function restoreResume(): Promise<void> {
  const video = videoElement.value;
  const version = resumeVersion;
  if (!video || !active.value || !pendingPlay || switchingToHls.value || video.readyState < 1)
    return;
  pendingPlay = false;
  await restorePosition(readSwitchTime() || readResume(resumeKey.value), true);
  if (destroyed.value || video !== videoElement.value || version !== resumeVersion) return;
  showControls();
}

async function restorePosition(time: number, play: boolean): Promise<void> {
  const video = videoElement.value;
  if (!video) return;
  video.currentTime = clampResumeTime(video, time);
  if (play) {
    try {
      await video.play();
    } catch {
      // Rejected autoplay leaves the video paused at the restored position.
    }
  }
}

function handleTimeUpdate(): void {
  syncVideoState();
  if (
    !destroyed.value &&
    !usingHls.value &&
    !automaticHlsTried &&
    !switchingToHls.value &&
    playbackVideoMissing(videoElement.value, hasVideo.value)
  ) {
    automaticHlsTried = true;
    recordPlaybackEvent('missing-video');
    void switchToHls();
  }
  updateMediaSessionPosition(videoElement.value);
  refreshGrant();
  persistProgress(false);
}

function handleEnded(): void {
  syncVideoState();
  persistProgress(true);
}

function handlePlay(): void {
  if (pendingSeek === null) pendingPlay = false;
  resumeVersion += 1;
  error.value = '';
  syncVideoState();
  showControls();
  refreshGrant();
}

function handlePause(): void {
  syncVideoState();
  showControls();
  persistProgress(true);
}

function readSwitchTime(): number {
  const video = videoElement.value;
  if (!video) return 0;
  return Number.isFinite(video.currentTime) && video.currentTime > 0 ? video.currentTime : 0;
}

function isPlaying(): boolean {
  const video = videoElement.value;
  return Boolean(video && !video.paused && !video.ended);
}

function handleSourceError(event: Event): void {
  recordPlaybackEvent('source-error', {
    target: event.target instanceof HTMLSourceElement ? 'source' : 'video',
  });
  if (hls || switchingToHls.value) return;
  void nextTick(async () => {
    if (destroyed.value || switchingToHls.value) return;
    // A native-HLS source failure still has one escape: hand the playlist to hls.js.
    if (usingHls.value) {
      if (nativeHlsActive.value && !hlsJsTried) {
        hlsJsTried = true;
        switchingToHls.value = true;
        try {
          await attachHlsJs(pendingSeek ?? readSwitchTime(), pendingPlay || isPlaying());
        } catch (cause) {
          error.value = cause instanceof Error ? cause.message : String(cause);
        } finally {
          switchingToHls.value = false;
        }
        return;
      }
      error.value = 'The browser could not play this file, even after conversion.';
      console.warn('Playback source failed', event);
      return;
    }
    if (automaticHlsTried) {
      error.value = 'The browser could not play this file, even after conversion.';
      console.warn('Playback source failed', event);
      return;
    }
    automaticHlsTried = true;
    await switchToHls();
  });
}

async function switchToHls(
  audioStreamIndex: number | null = payload.value?.audio_stream_index ?? null,
  subtitleTrackId: string | null = active.value ? bitmapSubtitleId.value : null,
): Promise<void> {
  const video = videoElement.value;
  if (
    !payload.value ||
    (audioStreamIndex === payload.value.audio_stream_index &&
      subtitleTrackId === payload.value.subtitle_track_id &&
      usingHls.value) ||
    switchingToHls.value ||
    !video
  )
    return;
  switchingToHls.value = true;
  recordPlaybackEvent('hls-switch', {
    audio_stream_index: audioStreamIndex,
    subtitle_track_id: subtitleTrackId,
  });
  try {
    const saved = active.value ? readResume(resumeKey.value) : 0;
    const time = pendingSeek ?? (readSwitchTime() || currentTime.value || saved);
    const resumePlayback = active.value && (pendingPlay || isPlaying());
    pendingSeek = null;
    pendingRate = video.playbackRate;
    pendingPlay = resumePlayback;
    video.pause();
    if (!(await requestPayload('hls', audioStreamIndex, subtitleTrackId)) || destroyed.value)
      return;
    usingHls.value = true;
    // Keep the working hls.js transport after a native-HLS failure.
    nativeHlsActive.value = !hls && nativeHlsSupport();
    await nextTick();
    if (destroyed.value) return;
    if (nativeHlsActive.value) {
      pendingSeek = time;
      pendingPlay = resumePlayback && active.value;
      video.load();
      return;
    }
    hlsJsTried = true;
    await attachHlsJs(time, resumePlayback && active.value);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    switchingToHls.value = false;
  }
}

async function attachHlsJs(time: number, play: boolean): Promise<void> {
  const video = videoElement.value;
  if (!video || !payload.value) return;
  pendingRate = video.playbackRate;
  const { default: HlsCtor } = await import('hls.js/light');
  if (destroyed.value || video !== videoElement.value) return;
  if (!HlsCtor.isSupported()) {
    throw new Error('This browser cannot play the converted stream.');
  }
  hls?.destroy();
  nativeHlsActive.value = false;
  await nextTick();
  if (destroyed.value) return;
  video.removeAttribute('src');
  video.load();
  hls = new HlsCtor({
    enableWorker: true,
    backBufferLength: 120,
    fragLoadingTimeOut: 200000,
  });
  hls.on(HlsCtor.Events.ERROR, (_event, data) => {
    recordPlaybackEvent('hls-error', {
      type: data.type,
      details: data.details,
      fatal: data.fatal,
      message: data.error?.message,
      response: data.response ? { code: data.response.code, url: data.response.url } : null,
    });
    if (data.fatal) error.value = 'Converted stream playback failed.';
  });
  pendingSeek = time;
  pendingPlay = play;
  hls.loadSource(uri(payload.value.urls.master));
  hls.attachMedia(video);
}

function nativeHlsSupport(): boolean {
  const video = videoElement.value;
  return Boolean(video?.canPlayType('application/vnd.apple.mpegurl'));
}

function refreshGrant(): void {
  if (!payload.value || destroyed.value || grantRequest || switchingToHls.value || notice.value)
    return;
  if (!playbackGrantExpiresSoon(payload.value)) return;
  const mode = payload.value.mode;
  void requestPayload(mode).catch(() => {
    if (!destroyed.value)
      notice.value = 'Playback could not be renewed. Close and reopen the player.';
  });
}

function persistProgress(force: boolean): void {
  const video = videoElement.value;
  if (!video || destroyed.value) return;
  if (nearEnd(video)) {
    clearResume(resumeKey.value);
    lastSaveAt = 0;
    return;
  }
  const time = Number.isFinite(video.currentTime) && video.currentTime > 0 ? video.currentTime : 0;
  if (time <= 0) return;
  const now = Date.now();
  if (!force && now - lastSaveAt < 1000) return;
  saveResume(resumeKey.value, time, video.duration);
  lastSaveAt = now;
}

function syncVideoState(): void {
  const video = videoElement.value;
  if (!video) {
    currentTime.value = 0;
    duration.value = 0;
    paused.value = true;
    return;
  }
  duration.value = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;
  currentTime.value =
    Number.isFinite(video.currentTime) && video.currentTime > 0 ? video.currentTime : 0;
  paused.value = video.paused;
  if (video.ended || nearEnd(video)) {
    clearResume(resumeKey.value);
    lastSaveAt = 0;
  }
}

async function syncAssRenderer(): Promise<void> {
  const track = selectedTrack.value;
  const video = videoElement.value;
  const overlay = assOverlayElement.value;
  const requestId = ++assRequestId;
  subtitleRequest?.abort();
  subtitleRequest = new AbortController();
  const signal = subtitleRequest.signal;
  subtitleError.value = '';
  destroyAssRenderer();
  if (
    !track ||
    track.renderer !== 'assjs' ||
    !subtitleEnabled.value ||
    !active.value ||
    !video ||
    !overlay ||
    !track.url
  ) {
    return;
  }
  try {
    let content = cachedAssContent;
    if (cachedAssUrl !== track.url) {
      const response = await fetch(uri(track.url), {
        headers: { Accept: 'text/x-ssa, text/plain' },
        signal,
      });
      if (!response.ok) throw new Error(`Subtitle fetch failed (HTTP ${response.status}).`);
      content = await response.text();
      if (requestId !== assRequestId) return;
      cachedAssUrl = track.url;
      cachedAssContent = content;
    }
    const assModule = await import('assjs');
    if (requestId !== assRequestId) return;
    const release = await loadPlaybackFonts(payload.value?.fonts ?? [], signal);
    if (requestId !== assRequestId || destroyed.value) {
      release();
      return;
    }
    releaseFonts = release;
    assRenderer = new assModule.default(content, video, {
      container: overlay,
      resampling: 'video_height',
    });
    assRenderer.show();
    video.dispatchEvent(new Event('seeking'));
    if (!video.paused) video.dispatchEvent(new Event('playing'));
  } catch (cause) {
    if (requestId === assRequestId && !signal.aborted) {
      destroyAssRenderer();
      subtitleError.value =
        cause instanceof Error ? cause.message : 'Subtitles could not be rendered.';
    }
  }
}

function destroyAssRenderer(): void {
  assRenderer?.destroy();
  assRenderer = null;
  releaseFonts?.();
  releaseFonts = null;
}

async function restoreNativeTextTrack(): Promise<void> {
  const video = videoElement.value;
  if (!video) return;
  try {
    const tracks = video.textTracks;
    for (let index = 0; index < tracks.length; index += 1) tracks[index]!.mode = 'disabled';
    if (subtitleEnabled.value && nativeSubtitleTrack.value) {
      await nextTick();
      if (video !== videoElement.value || destroyed.value) return;
      for (let index = 0; index < video.textTracks.length; index += 1) {
        video.textTracks[index]!.mode =
          video.textTracks[index] === nativeTrackElement.value?.track ? 'showing' : 'disabled';
      }
    }
  } catch {
    // Safari can reject native track changes. Playback continues without them.
  }
}

function scheduleAssLayoutRefresh(): void {
  if (!usesAssSubtitleTrack.value || destroyed.value) return;
  void nextTick(() => {
    assLayoutVersion.value += 1;
  });
}

function showControls(): void {
  controlsVisible.value = true;
  if (controlsHideTimeout) {
    window.clearTimeout(controlsHideTimeout);
    controlsHideTimeout = 0;
  }
  if (videoElement.value?.paused) return;
  controlsHideTimeout = window.setTimeout(() => {
    controlsVisible.value = false;
  }, 2500);
}

function handleVideoClick(): void {
  if (isTouchDevice.value) {
    if (videoElement.value?.paused) {
      showControls();
      return;
    }
    controlsVisible.value = !controlsVisible.value;
    return;
  }
  if (pendingVideoClickTimeout) window.clearTimeout(pendingVideoClickTimeout);
  pendingVideoClickTimeout = window.setTimeout(() => {
    pendingVideoClickTimeout = 0;
    if (controlsVisible.value && !videoElement.value?.paused) controlsVisible.value = false;
  }, 180);
}

function handlePointerMove(event: PointerEvent): void {
  if (!playerContainer.value || isTouchDevice.value) return;
  const rect = playerContainer.value.getBoundingClientRect();
  const bottomZone = Math.min(Math.max(rect.height * 0.28, 96), 180);
  if (event.clientY - rect.top >= rect.height - bottomZone) showControls();
}

function handleSeekInput(event: Event): void {
  resumeVersion += 1;
  const target = event.target as HTMLInputElement | null;
  if (!target || !videoElement.value || !duration.value) return;
  const sliderValue = Number(target.value);
  if (!Number.isFinite(sliderValue)) return;
  videoElement.value.currentTime = (sliderValue / 1000) * duration.value;
  syncVideoState();
  showControls();
}

function handleVolumeInput(event: Event): void {
  const target = event.target as HTMLInputElement | null;
  if (!target) return;
  const nextVol = clampMediaVolume(Number(target.value) / 100);
  mediaVol.value = nextVol;
  muted.value = nextVol <= 0;
  applyMediaState(videoElement.value);
}

function handleVolumeChange(event: Event): void {
  const target = event.target as HTMLMediaElement | null;
  if (!target || typeof target.volume !== 'number') return;
  if (target.muted !== muted.value) muted.value = target.muted;
  const normalizedVolume = clampMediaVolume(target.volume);
  if (Math.abs(mediaVol.value - normalizedVolume) > 0.001) mediaVol.value = normalizedVolume;
}

function togglePlayback(): void {
  const video = videoElement.value;
  if (!video) return;
  if (video.paused) void video.play().catch(() => {});
  else video.pause();
}

function handleKeyDown(event: KeyboardEvent): void {
  if (diagnosticsOpen.value && event.key === 'Escape') {
    diagnosticsOpen.value = false;
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  if (helpOpen.value && event.key === 'Escape') {
    helpOpen.value = false;
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  if (!active.value || destroyed.value || helpOpen.value || diagnosticsOpen.value) return;
  const video = videoElement.value;
  if (!video) return;
  if (
    handlePlaybackKey(event, video, {
      fullscreen: () => void toggleFullscreen(),
      captions: () => {
        if (playableTracks.value.length && !switchingToHls.value)
          subtitleEnabled.value = !subtitleEnabled.value;
      },
      help: () => {
        helpOpen.value = true;
      },
    })
  ) {
    syncVideoState();
    showControls();
  }
}

function toggleMute(): void {
  if (muted.value || mediaVol.value <= 0) {
    mediaVol.value = mediaVol.value > 0 ? clampMediaVolume(mediaVol.value) : 1;
    muted.value = false;
  } else {
    muted.value = true;
  }
  applyMediaState(videoElement.value);
}

function applyMediaState(element: HTMLVideoElement | null): void {
  if (!element) return;
  const normalizedVolume = clampMediaVolume(mediaVol.value);
  if (Math.abs(element.volume - normalizedVolume) > 0.001) element.volume = normalizedVolume;
  if (element.muted !== muted.value) element.muted = muted.value;
}

function syncFullscreenState(): void {
  const doc = document as FullscreenCapableDocument;
  const fullscreenElement = doc.fullscreenElement || doc.webkitFullscreenElement || null;
  isFullscreen.value = Boolean(fullscreenElement && fullscreenElement === playerContainer.value);
  scheduleAssLayoutRefresh();
}

async function toggleFullscreen(): Promise<void> {
  const element = playerContainer.value as FullscreenCapableElement | null;
  if (!element) return;
  const doc = document as FullscreenCapableDocument;
  try {
    if (isFullscreen.value) {
      if (typeof doc.exitFullscreen === 'function') await doc.exitFullscreen();
      else await doc.webkitExitFullscreen?.();
      return;
    }
    if (typeof element.requestFullscreen === 'function') await element.requestFullscreen();
    else await element.webkitRequestFullscreen?.();
  } catch {
    // Rejections keep inline playback. The state listener corrects the icon.
  }
}

function applyMediaSessionMetadata(): void {
  if (!('mediaSession' in navigator)) return;
  const metadata: MediaMetadataInit = { title: props.title };
  if (posterUrl.value) {
    metadata.artwork = [{ src: posterUrl.value }];
  }
  try {
    navigator.mediaSession.metadata = new MediaMetadata(metadata);
    navigator.mediaSession.setActionHandler('play', () => {
      if (!active.value) activatePlayer();
      else void videoElement.value?.play();
    });
    navigator.mediaSession.setActionHandler('pause', () => videoElement.value?.pause());
    navigator.mediaSession.setActionHandler('seekbackward', () => seekBy(-10));
    navigator.mediaSession.setActionHandler('seekforward', () => seekBy(10));
    navigator.mediaSession.setActionHandler('seekto', (details) => {
      if (typeof details.seekTime === 'number') seekTo(details.seekTime);
    });
  } catch {
    // Unsupported action handlers are optional per platform.
  }
}

function seekBy(delta: number): void {
  const video = videoElement.value;
  if (!video) return;
  seekTo(video.currentTime + delta);
}

function seekTo(time: number): void {
  const video = videoElement.value;
  if (!video) return;
  video.currentTime = clampResumeTime(video, time);
  syncVideoState();
}

function updateMediaSessionPosition(target: HTMLVideoElement | null): void {
  if (!('mediaSession' in navigator) || !target) return;
  const mediaDuration = target.duration;
  if (!Number.isFinite(mediaDuration) || mediaDuration <= 0) return;
  try {
    navigator.mediaSession.setPositionState({
      duration: mediaDuration,
      playbackRate: target.playbackRate,
      position: Math.max(0, Math.min(target.currentTime, mediaDuration)),
    });
  } catch {
    // Position state is informational. Platform rejections do not affect playback.
  }
}

function teardown(): void {
  if (destroyed.value) return;
  resumeVersion += 1;
  persistProgress(true);
  destroyed.value = true;
  window.clearInterval(renewalTimer);
  subtitleRequest?.abort();
  posterRequest?.abort();
  if (controlsHideTimeout) window.clearTimeout(controlsHideTimeout);
  if (pendingVideoClickTimeout) window.clearTimeout(pendingVideoClickTimeout);
  if (hls) {
    hls.destroy();
    hls = null;
  }
  assRequestId += 1;
  destroyAssRenderer();
  if ('mediaSession' in navigator) {
    try {
      navigator.mediaSession.metadata = null;
      for (const action of ['play', 'pause', 'seekbackward', 'seekforward', 'seekto'] as const)
        navigator.mediaSession.setActionHandler(action, null);
    } catch {
      // Media Session cleanup must not prevent the player from closing.
    }
  }
  if (posterImage.value) URL.revokeObjectURL(posterImage.value);
  const video = videoElement.value;
  if (video) {
    try {
      video.pause();
      video.removeAttribute('src');
      for (const track of video.querySelectorAll('track')) track.removeAttribute('src');
      for (const source of video.querySelectorAll('source')) source.removeAttribute('src');
      video.load();
    } catch {
      // The element is being discarded with the modal.
    }
  }
  if (payload.value) closePlayback(payload.value.playback_id);
}

watch(open, (value) => {
  if (!value) teardown();
});

const handleVisibilityChange = (): void => {
  if (document.visibilityState === 'hidden') persistProgress(true);
};
const flushProgress = (): void => persistProgress(true);

onMounted(async () => {
  isTouchDevice.value = window.matchMedia('(pointer: coarse)').matches;
  document.addEventListener('fullscreenchange', syncFullscreenState);
  document.addEventListener('webkitfullscreenchange', syncFullscreenState);
  window.addEventListener('resize', scheduleAssLayoutRefresh);
  window.addEventListener('orientationchange', scheduleAssLayoutRefresh);
  document.addEventListener('visibilitychange', handleVisibilityChange);
  window.addEventListener('pagehide', flushProgress);
  syncFullscreenState();
  await loadPayload();
  if (destroyed.value) return;
  renewalTimer = window.setInterval(refreshGrant, 30000);
  if (payload.value && 'mediaSession' in navigator) applyMediaSessionMetadata();
  void loadPoster();
});

onBeforeUnmount(() => {
  document.removeEventListener('fullscreenchange', syncFullscreenState);
  document.removeEventListener('webkitfullscreenchange', syncFullscreenState);
  window.removeEventListener('resize', scheduleAssLayoutRefresh);
  window.removeEventListener('orientationchange', scheduleAssLayoutRefresh);
  document.removeEventListener('visibilitychange', handleVisibilityChange);
  window.removeEventListener('pagehide', flushProgress);
  teardown();
});
</script>

<style scoped>
.seek-bar {
  touch-action: none;
}
</style>
