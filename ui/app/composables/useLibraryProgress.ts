import { computed, ref, shallowReadonly } from 'vue';
import { parse_api_error, request } from '~/utils';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import type {
  LibraryProgress,
  LibraryProgressSnapshot,
  ScannerState,
  ProbeState,
} from '~/types/api';
import { useEvents } from './useEvents';

const items = ref<Record<number, LibraryProgress>>({});
const scanner = ref<ScannerState | null>(null);
const probing = ref<ProbeState | null>(null);
const scannerBusy = ref(false);
const summary = computed(() => {
  const progress = Object.values(items.value);
  const active = progress.find((row) => ['scanning', 'cancelling'].includes(row.discovery.status));
  const queued = progress.filter(
    (row) => row !== active && (row.scanner.discovery_pending || row.discovery.status === 'queued'),
  );
  let processed = 0;
  let remaining = 0;
  let activeProbes = 0;
  for (const row of progress) {
    processed += row.probing.processed;
    remaining += row.probing.remaining;
    activeProbes += row.probing.active.length;
  }
  const discoveryStatus = scanner.value?.failed
    ? 'failed'
    : (active?.discovery.status ??
      (scanner.value?.paused ? 'paused' : queued.length ? 'queued' : 'idle'));
  const probingStatus = probing.value?.failed
    ? 'failed'
    : progress.some((row) => row.probing.status === 'scanning')
      ? 'scanning'
      : scanner.value?.paused
        ? 'paused'
        : remaining
          ? 'queued'
          : 'idle';
  return {
    processed,
    remaining,
    activeProbes,
    found: active?.discovery.found ?? 0,
    discoveryStatus,
    discoveryLibraryId: active?.library_id ?? queued[0]?.library_id ?? null,
    queued: queued.length,
    probingStatus,
  };
});
const isLoading = ref(false);
const lastError = ref<Error | null>(null);
let loaded = false;
let inFlight: Promise<void> | null = null;
let journal: Map<number, LibraryProgress> | null = null;
let scannerJournal: ScannerState | null = null;
let probingJournal: ProbeState | null = null;
let eventsBound = false;

const updateScanner = (state: ScannerState): void => {
  scanner.value = state;
  if (journal) scannerJournal = state;
  for (const progress of Object.values(items.value)) progress.scanner.paused = state.paused;
};
const update = (progress: LibraryProgress): void => {
  if (scanner.value) progress.scanner.paused = scanner.value.paused;
  journal?.set(progress.library_id, progress);
  items.value[progress.library_id] = progress;
};

const bindEvents = (): void => {
  if (eventsBound) return;
  eventsBound = true;
  const events = useEvents();
  events.on((event) => {
    if (event.type === 'library.progress') {
      update(event.payload);
    } else if (event.type === 'scanner.state') {
      updateScanner(event.payload);
    } else if (event.type === 'probing.state') {
      probing.value = event.payload;
      if (journal) probingJournal = event.payload;
    }
  });
};

const fetchProgress = async (): Promise<void> => {
  isLoading.value = true;
  lastError.value = null;
  const updates = new Map<number, LibraryProgress>();
  journal = updates;
  scannerJournal = null;
  probingJournal = null;
  try {
    const response = await request('/api/libraries/progress', { cache: 'no-store' });
    if (!response.ok) throw new Error(`Could not load library activity (HTTP ${response.status}).`);
    const snapshot = (await response.json()) as LibraryProgressSnapshot;
    const next = Object.fromEntries(snapshot.libraries.map((row) => [row.library_id, row]));
    for (const [id, progress] of updates) next[id] = progress;
    items.value = next;
    updateScanner(scannerJournal ?? snapshot.scanner);
    probing.value = probingJournal ?? snapshot.probing;
    loaded = true;
  } catch (error) {
    lastError.value = error instanceof Error ? error : new Error(String(error));
    loaded = false;
  } finally {
    journal = null;
    scannerJournal = null;
    probingJournal = null;
    isLoading.value = false;
  }
};

const load = (): Promise<void> =>
  (inFlight ??= fetchProgress().finally(() => {
    inFlight = null;
  }));

const ensure = (): Promise<void> => (loaded ? Promise.resolve() : load());

const setPaused = async (paused: boolean): Promise<void> => {
  if (!scanner.value || isLoading.value || scannerBusy.value) return;
  scannerBusy.value = true;
  const notify = useNotification();
  const { messageFor } = useApiErrorMessage();
  try {
    const response = await request(`/api/libraries/scanner/${paused ? 'pause' : 'resume'}`, {
      method: 'POST',
    });
    if (!response.ok)
      throw new Error(
        (await parse_api_error(await response.json().catch(() => null))) ??
          `Could not ${paused ? 'pause' : 'resume'} library activity.`,
      );
    updateScanner((await response.json()) as ScannerState);
    notify.success(`Activity ${paused ? 'paused' : 'resumed'}.`);
  } catch (error) {
    notify.error(messageFor(error instanceof Error ? error.message : undefined));
  } finally {
    scannerBusy.value = false;
  }
};

export const useLibraryProgress = () => {
  bindEvents();
  return {
    items: shallowReadonly(items),
    scanner: shallowReadonly(scanner),
    scannerBusy: shallowReadonly(scannerBusy),
    setPaused,
    summary,
    isLoading: shallowReadonly(isLoading),
    lastError: shallowReadonly(lastError),
    ensure,
    load,
    update,
  };
};
