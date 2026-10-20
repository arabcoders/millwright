import { computed, ref } from 'vue';
import { ensure_api_success, request } from '~/utils';
import type { BatchDryRunReport, BatchDryRunScope, FlowRow } from '~/types/api';

const ACTIVE = new Set(['preparing', 'running', 'cancelling']);

export const useBatchDryRun = () => {
  const report = ref<BatchDryRunReport | null>(null);
  const error = ref('');
  const loading = ref(false);
  const starting = ref(false);
  const cancelling = ref(false);
  const exporting = ref(false);
  const page = ref(1);
  const pageSize = ref(50);
  const statuses = ref<string[]>([]);
  const active = computed(() => report.value !== null && ACTIVE.has(report.value.state));
  let scope: BatchDryRunScope | null = null;
  let fileIds: number[] = [];
  let visible = false;
  let generation = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const requests = new Set<AbortController>();

  const invalidateReads = (): void => {
    generation++;
    clearTimeout(timer);
    timer = undefined;
    for (const controller of requests) controller.abort();
    requests.clear();
    loading.value = exporting.value = false;
  };

  const stop = (): void => {
    visible = false;
    invalidateReads();
  };

  const base = (): string =>
    scope && 'library' in scope
      ? `/api/libraries/${scope.library.id}/dry-runs`
      : '/api/files/dry-runs';
  const read = async <T>(url: string, allowMissing = false): Promise<T | null> => {
    const controller = new AbortController();
    requests.add(controller);
    try {
      const response = await request(url, { signal: controller.signal });
      if (allowMissing && response.status === 404) return null;
      await ensure_api_success(response);
      return (await response.json()) as T;
    } finally {
      requests.delete(controller);
    }
  };

  const schedule = (): void => {
    clearTimeout(timer);
    timer = undefined;
    if (visible && active.value) timer = setTimeout(() => void refresh(), 1000);
  };

  const loadPage = async (): Promise<void> => {
    if (!report.value) return;
    const token = generation;
    const id = report.value.id;
    clearTimeout(timer);
    loading.value = true;
    error.value = '';
    try {
      const query = new URLSearchParams({
        limit: String(pageSize.value),
        offset: String((page.value - 1) * pageSize.value),
      });
      for (const status of statuses.value) query.append('status', status);
      const result = await read<BatchDryRunReport>(`${base()}/${id}?${query}`, true);
      if (token !== generation) return;
      report.value = result;
      if (!result) page.value = 1;
      else {
        const lastPage = Math.max(1, Math.ceil(result.pagination.total / pageSize.value));
        if (page.value > lastPage) {
          page.value = lastPage;
          await loadPage();
          return;
        }
      }
      schedule();
    } catch (cause) {
      if (token === generation)
        error.value = cause instanceof Error ? cause.message : 'Could not load dry run.';
    } finally {
      if (token === generation) loading.value = false;
    }
  };

  const refresh = async (): Promise<void> => {
    if (!visible || loading.value || starting.value || cancelling.value || exporting.value) return;
    await loadPage();
  };

  const open = async (target: BatchDryRunScope): Promise<void> => {
    stop();
    visible = true;
    scope = target;
    fileIds =
      'files' in target
        ? [...new Set(target.files.map((file) => file.id))].sort((a, b) => a - b)
        : [];
    report.value = null;
    page.value = 1;
    statuses.value = [];
    error.value = '';
    loading.value = true;
    const token = generation;
    try {
      const query = new URLSearchParams(fileIds.map((id) => ['file_ids', String(id)]));
      const saved = (
        await read<BatchDryRunReport[]>(`${base()}${fileIds.length ? `?${query}` : ''}`)
      )?.[0];
      if (token !== generation) return;
      if (saved) {
        report.value = saved;
        await loadPage();
      }
    } catch (cause) {
      if (token === generation)
        error.value = cause instanceof Error ? cause.message : 'Could not load dry run.';
    } finally {
      if (token === generation) loading.value = false;
    }
  };

  const start = async (flow: FlowRow): Promise<void> => {
    if (
      !visible ||
      !scope ||
      loading.value ||
      starting.value ||
      cancelling.value ||
      exporting.value ||
      active.value
    )
      return;
    invalidateReads();
    const token = generation;
    starting.value = true;
    error.value = '';
    try {
      // Closing the view doesn't abort a start request or cancel server-owned work.
      const response = await request(base(), {
        method: 'POST',
        body: JSON.stringify({
          flow_id: flow.id,
          expected_version: flow.version,
          ...('files' in scope ? { file_ids: fileIds } : {}),
        }),
      });
      await ensure_api_success(response);
      const result = (await response.json()) as BatchDryRunReport;
      if (token !== generation) return;
      report.value = result;
      page.value = 1;
      if (statuses.value.length) await loadPage();
      else schedule();
    } catch (cause) {
      if (token === generation)
        error.value = cause instanceof Error ? cause.message : 'Could not start dry run.';
    } finally {
      starting.value = false;
      if (token !== generation && visible && active.value) schedule();
    }
  };

  const changePage = async (value: number, size = pageSize.value): Promise<void> => {
    if (
      !visible ||
      !report.value ||
      loading.value ||
      starting.value ||
      cancelling.value ||
      exporting.value
    )
      return;
    if (
      !Number.isInteger(value) ||
      !Number.isInteger(size) ||
      size < 1 ||
      size > 1000 ||
      value < 1 ||
      value > Math.max(1, Math.ceil(report.value.pagination.total / size))
    )
      return;
    invalidateReads();
    page.value = value;
    pageSize.value = size;
    await refresh();
  };

  const changeStatuses = async (values: string[]): Promise<void> => {
    if (!visible || !report.value || starting.value || cancelling.value || exporting.value) return;
    invalidateReads();
    statuses.value = values;
    page.value = 1;
    await refresh();
  };

  const cancel = async (): Promise<void> => {
    if (
      !visible ||
      !report.value ||
      !active.value ||
      report.value.state === 'cancelling' ||
      starting.value ||
      cancelling.value ||
      exporting.value
    )
      return;
    const id = report.value.id;
    invalidateReads();
    const token = generation;
    cancelling.value = true;
    error.value = '';
    try {
      const response = await request(`${base()}/${id}/cancel`, { method: 'POST' });
      if (token !== generation) return;
      if (response.status === 404) {
        report.value = null;
        page.value = 1;
        return;
      }
      await ensure_api_success(response);
      const result = (await response.json()) as BatchDryRunReport;
      if (token !== generation) return;
      report.value = { ...result, items: report.value.items, pagination: report.value.pagination };
      await loadPage();
    } catch (cause) {
      if (token === generation) {
        error.value = cause instanceof Error ? cause.message : 'Could not cancel dry run.';
        schedule();
      }
    } finally {
      cancelling.value = false;
      if (token !== generation && visible && active.value) schedule();
    }
  };

  const exportCsv = async (): Promise<Blob | null> => {
    if (
      !visible ||
      !report.value ||
      active.value ||
      !report.value.snapshot_complete ||
      loading.value ||
      starting.value ||
      cancelling.value ||
      exporting.value
    )
      return null;
    const token = generation;
    const controller = new AbortController();
    requests.add(controller);
    exporting.value = true;
    error.value = '';
    try {
      const response = await request(`${base()}/${report.value.id}/export.csv`, {
        headers: { Accept: 'text/csv' },
        signal: controller.signal,
      });
      if (token !== generation) return null;
      if (response.status === 404) {
        report.value = null;
        page.value = 1;
        return null;
      }
      await ensure_api_success(response);
      const blob = await response.blob();
      return token === generation ? blob : null;
    } catch (cause) {
      if (token === generation)
        error.value = cause instanceof Error ? cause.message : 'Could not export dry run.';
      return null;
    } finally {
      requests.delete(controller);
      if (token === generation) exporting.value = false;
    }
  };

  return {
    report,
    error,
    loading,
    starting,
    cancelling,
    exporting,
    page,
    pageSize,
    statuses,
    active,
    open,
    start,
    refresh,
    changePage,
    changeStatuses,
    cancel,
    exportCsv,
    stop,
  };
};
