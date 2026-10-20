import { computed, onScopeDispose, ref, toRaw, watch } from 'vue';
import FileTranscodeModal from '~/components/FileTranscodeModal.vue';
import { useFlows } from '~/composables/useFlows';
import { useLibraries } from '~/composables/useLibraries';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import { useFileDryRun } from '~/composables/useFileDryRun';
import type { FlowRow, MediaFileRow } from '~/types/api';

export const useTranscodeFlow = () => {
  const { items: flows, reload, lastError } = useFlows();
  const { items: libraries } = useLibraries();
  const { messageFor } = useApiErrorMessage();
  const overlay = useOverlay();
  const preview = useFileDryRun();
  const previewing = computed(() => preview.isOpening.value || preview.isOpen.value);
  const state = ref<{
    file: MediaFileRow;
    flows: FlowRow[];
    selectedFlowId?: number;
    loading: boolean;
    error: string;
  } | null>(null);
  let modal: ReturnType<typeof overlay.create<typeof FileTranscodeModal>> | null = null;
  let disposed = false;
  const props = computed(() => ({
    open: state.value !== null,
    filePath: state.value?.file.path ?? '',
    flows: state.value?.flows ?? [],
    selectedFlowId: state.value?.selectedFlowId,
    loading: state.value?.loading ?? false,
    previewing: previewing.value,
    error: state.value?.error ?? '',
    'onUpdate:open': (open: boolean) => {
      if (!open) {
        preview.close();
        modal?.close(null);
      }
    },
    'onUpdate:selectedFlowId': (id: number | undefined) => {
      preview.close();
      if (state.value) state.value.selectedFlowId = id;
    },
    onDryRun: async () => {
      const current = state.value;
      if (!current || current.loading || current.error || previewing.value) return;
      const flow = current.flows.find((flow) => flow.id === current.selectedFlowId);
      if (!flow) return;
      await preview.open(current.file, flow);
    },
    onRun: () => {
      if (!state.value || state.value.loading || state.value.error || previewing.value) return;
      const flow = state.value.flows.find((flow) => flow.id === state.value?.selectedFlowId);
      if (flow) modal?.close(flow);
    },
  }));
  watch(props, (props) => modal?.patch(props));
  onScopeDispose(() => {
    disposed = true;
    modal?.close(null);
  });

  const pick = async (file: MediaFileRow): Promise<FlowRow | null> => {
    if (disposed) return null;
    state.value = { file, flows: [], loading: true, error: '' };
    const current = state.value;
    const opened = overlay.create(FileTranscodeModal, {
      props: props.value,
      destroyOnClose: true,
    });
    modal = opened;
    const result = opened.open().result;
    void reload().then(() => {
      if (disposed || state.value !== current) return;
      current.loading = false;
      if (lastError.value) {
        current.error = messageFor(
          lastError.value.detail,
          'Close this dialog and try again to reload saved flows.',
        );
        return;
      }
      current.flows = flows.value.map((flow) => structuredClone(toRaw(flow)));
      const bound = libraries.value.find((library) => library.id === file.library_id)?.flow_id;
      current.selectedFlowId =
        current.flows.find((flow) => flow.id === bound)?.id ?? current.flows[0]?.id;
    });
    try {
      return (await result) ?? null;
    } finally {
      preview.close();
      state.value = null;
      modal = null;
    }
  };
  return { pick };
};
