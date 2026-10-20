import { onScopeDispose, ref, watch } from 'vue';
import { ensure_api_success, request } from '~/utils';
import type { FlowRow, JobDetail, LibraryRow, NodeRow } from '~/types/api';

export type Searchable = {
  nodes: NodeRow[];
  libraries: LibraryRow[];
  flows: FlowRow[];
};

export const loadSearchable = async (signal?: AbortSignal): Promise<Searchable> => {
  const responses = await Promise.all(
    ['/api/nodes', '/api/libraries', '/api/flows'].map(async (path) => {
      const response = await request(path, { signal });
      await ensure_api_success(response);
      return response.json();
    }),
  );
  return {
    nodes: responses[0] as NodeRow[],
    libraries: responses[1] as LibraryRow[],
    flows: responses[2] as FlowRow[],
  };
};

export const lookupJob = async (id: string, signal?: AbortSignal): Promise<JobDetail | null> => {
  const response = await request(`/api/jobs/${id}`, { signal });
  if (response.status === 404) return null;
  await ensure_api_success(response);
  return (await response.json()) as JobDetail;
};

export const useGlobalSearch = () => {
  const searchOpen = ref(false);
  const searchTerm = ref('');
  const searchable = ref<Searchable>({ nodes: [], libraries: [], flows: [] });
  const matchedJob = ref<JobDetail | null>(null);
  const listError = ref('');
  const jobError = ref('');
  let listController: AbortController | null = null;
  let jobController: AbortController | null = null;

  const closeSearch = (): void => {
    searchOpen.value = false;
    listController?.abort();
    listController = null;
    jobController?.abort();
    jobController = null;
    matchedJob.value = null;
    searchTerm.value = '';
    listError.value = '';
    jobError.value = '';
  };

  const openSearch = async (): Promise<void> => {
    if (searchOpen.value) return;
    searchOpen.value = true;
    const controller = new AbortController();
    listController = controller;
    try {
      const result = await loadSearchable(controller.signal);
      if (controller.signal.aborted || listController !== controller) return;
      searchable.value = result;
    } catch {
      if (!controller.signal.aborted && listController === controller) {
        listError.value = 'Could not load search data. Close and reopen search to retry.';
        controller.abort();
      }
    } finally {
      if (listController === controller) listController = null;
    }
  };

  watch(searchTerm, (term, _previous, onCleanup) => {
    jobController?.abort();
    matchedJob.value = null;
    jobError.value = '';
    const id = /^(?:job\s*)?#?(\d+)$/i.exec(term.trim())?.[1];
    if (!id || !searchOpen.value) return;
    const controller = new AbortController();
    jobController = controller;
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const job = await lookupJob(id, controller.signal);
          if (!controller.signal.aborted && jobController === controller) matchedJob.value = job;
        } catch {
          if (!controller.signal.aborted && jobController === controller)
            jobError.value = 'Could not look up this job. Change the query to retry.';
        }
      })();
    }, 150);
    onCleanup(() => {
      clearTimeout(timer);
      controller.abort();
    });
  });
  onScopeDispose(closeSearch);

  return {
    searchOpen,
    searchTerm,
    searchable,
    matchedJob,
    listError,
    jobError,
    openSearch,
    closeSearch,
  };
};
