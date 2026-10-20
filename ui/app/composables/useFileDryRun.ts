import { computed, onScopeDispose, ref, watch } from 'vue';
import FlowDryRunModal from '~/components/FlowDryRunModal.vue';
import { useFlows, type FlowError } from '~/composables/useFlows';
import { usePlugins } from '~/composables/usePlugins';
import { useLibraries } from '~/composables/useLibraries';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import { formatBitrate, formatBytes, formatTime } from '~/utils';
import { libraryLocation } from '~/utils/location';
import type { DryRunResult, FlowRow, MediaFileRow } from '~/types/api';

export const useFileDryRun = (onOpenFile?: (id: number) => void) => {
  const { items: flows, reload: reloadFlows, lastError: flowError, dryRun } = useFlows();
  const { items: plugins, ensure: ensurePlugins } = usePlugins();
  const { items: libraries } = useLibraries();
  const { messageFor } = useApiErrorMessage();
  const overlay = useOverlay();
  const isOpen = ref(false);
  const isOpening = ref(false);
  const file = ref<MediaFileRow | null>(null);
  const selectedFlowId = ref<number | undefined>(undefined);
  const capturedFlow = ref<FlowRow | null>(null);
  const availableFlows = computed(() => (capturedFlow.value ? [capturedFlow.value] : flows.value));
  const running = ref(false);
  const result = ref<DryRunResult | null>(null);
  const error = ref('');
  let request = 0;
  let disposed = false;
  let modal: ReturnType<typeof overlay.create<typeof FlowDryRunModal>> | null = null;

  const close = (open = false): void => {
    if (open) return;
    request += 1;
    isOpen.value = false;
    isOpening.value = false;
    running.value = false;
    result.value = null;
    error.value = '';
    file.value = null;
    selectedFlowId.value = undefined;
    capturedFlow.value = null;
    modal?.close();
    modal = null;
  };

  const run = async (): Promise<void> => {
    const requestId = ++request;
    running.value = false;
    result.value = null;
    error.value = '';
    const flow = availableFlows.value.find((candidate) => candidate.id === selectedFlowId.value);
    if (disposed || !isOpen.value || !file.value) return;
    if (!capturedFlow.value && flowError.value) {
      error.value = messageFor(flowError.value.detail, 'Could not load saved flows. Try again.');
      return;
    }
    if (!flow) {
      error.value = 'The selected flow is unavailable. Pick a saved flow.';
      return;
    }
    running.value = true;
    try {
      const preview = await dryRun(file.value.id, flow.definition);
      if (requestId === request && isOpen.value) result.value = preview;
    } catch (e) {
      if (requestId === request && isOpen.value) {
        const failure = e as FlowError;
        error.value = messageFor(
          failure.detail,
          e instanceof Error
            ? e.message
            : `Dry run failed${failure.status ? ` (HTTP ${failure.status})` : ''}. Try again.`,
        );
      }
    } finally {
      if (requestId === request) running.value = false;
    }
  };

  const modalProps = computed(() => ({
    open: isOpen.value,
    selectedFlowId: selectedFlowId.value,
    flows: availableFlows.value,
    plugins: plugins.value,
    filePath: file.value?.path ?? '',
    location: file.value
      ? libraryLocation(file.value.path, file.value.library_id, libraries.value)
      : '',
    facts: file.value
      ? [
          formatBytes(file.value.size),
          file.value.container,
          file.value.video_codec,
          file.value.width ? `${file.value.width}×${file.value.height}` : undefined,
          file.value.duration_s ? formatTime(file.value.duration_s) : undefined,
          file.value.bit_rate ? formatBitrate(file.value.bit_rate) : undefined,
        ]
          .filter(Boolean)
          .join(' · ')
      : '',
    running: running.value,
    result: result.value,
    error: error.value,
    argvPreview: result.value?.argv?.join(' ') ?? '',
    'onUpdate:open': close,
    'onUpdate:selectedFlowId': (id: number | undefined) => {
      selectedFlowId.value = id;
      void run();
    },
    onOpenFile: onOpenFile
      ? () => {
          if (file.value) onOpenFile(file.value.id);
        }
      : undefined,
    onRun: run,
  }));

  const open = async (row: MediaFileRow, selection?: number | FlowRow): Promise<void> => {
    if (disposed) return;
    isOpening.value = true;
    const requestId = ++request;
    running.value = false;
    result.value = null;
    error.value = '';
    const captured = typeof selection === 'object' ? selection : null;
    await Promise.allSettled(captured ? [ensurePlugins()] : [reloadFlows(), ensurePlugins()]);
    if (disposed || requestId !== request) return;
    isOpening.value = false;
    file.value = row;
    capturedFlow.value = captured;
    const bound = libraries.value.find((library) => library.id === row.library_id)?.flow_id;
    selectedFlowId.value =
      typeof selection === 'number' ? selection : (captured?.id ?? bound ?? flows.value[0]?.id);
    isOpen.value = true;
    if (!modal) {
      modal = overlay.create(FlowDryRunModal, { props: modalProps.value, destroyOnClose: true });
      const opened = modal;
      void opened.open().result.then(() => {
        if (modal === opened) modal = null;
      });
    }
    await run();
  };

  watch(modalProps, (props) => modal?.patch(props));
  onScopeDispose(() => {
    disposed = true;
    close();
  });
  return { isOpen, isOpening, fileId: computed(() => file.value?.id), open, close };
};
