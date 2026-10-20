import { ref, shallowReadonly } from 'vue';
import { parse_api_error, request } from '~/utils';
import type { DryRunResult, FlowDefinition, FlowFact, FlowPreset, FlowRow } from '~/types/api';

const items = ref<FlowRow[]>([]);
const isLoading = ref(false);
export type FlowError = { status?: number; detail?: string; cause?: unknown };
const lastError = ref<FlowError | null>(null);
let loaded = false;
let inFlight: Promise<void> | null = null;
const facts = ref<FlowFact[]>([]);
const presets = ref<FlowPreset[]>([]);
let factsLoaded = false;
let presetsLoaded = false;
let factsInFlight: Promise<void> | null = null;
let presetsInFlight: Promise<void> | null = null;
const apiError = async (response: Response): Promise<FlowError> => ({
  status: response.status,
  detail: await parse_api_error(await response.json().catch(() => null)),
});

type OperationFailure = { ok: false; error: FlowError };
type SaveResult = { ok: true } | OperationFailure;
type LayoutResult = { ok: true; server: Partial<FlowRow> } | OperationFailure;

const fetchFlows = async (): Promise<void> => {
  isLoading.value = true;
  lastError.value = null;
  try {
    const response = await request('/api/flows');
    if (!response.ok) throw await apiError(response);
    items.value = (await response.json()) as FlowRow[];
    loaded = true;
  } catch (error) {
    lastError.value = error && typeof error === 'object' ? (error as FlowError) : { cause: error };
    loaded = false;
  } finally {
    isLoading.value = false;
    inFlight = null;
  }
};

const ensure = (): Promise<void> => {
  if (loaded) return Promise.resolve();
  return (inFlight ??= fetchFlows());
};

const reload = (): Promise<void> => inFlight ?? (inFlight = fetchFlows());

const fetchFacts = async (): Promise<void> => {
  const response = await request('/api/flows/facts');
  if (!response.ok) throw await apiError(response);
  facts.value = (await response.json()) as FlowFact[];
  factsLoaded = true;
};

const ensureFacts = (): Promise<void> => {
  if (factsLoaded) return Promise.resolve();
  return (factsInFlight ??= fetchFacts().finally(() => (factsInFlight = null)));
};

const fetchPresets = async (): Promise<void> => {
  const response = await request('/api/flows/presets');
  if (!response.ok) throw await apiError(response);
  presets.value = (await response.json()) as FlowPreset[];
  presetsLoaded = true;
};

const ensurePresets = (): Promise<void> => {
  if (presetsLoaded) return Promise.resolve();
  return (presetsInFlight ??= fetchPresets().finally(() => (presetsInFlight = null)));
};

const upsert = (row: FlowRow): void => {
  const index = items.value.findIndex((item) => item.id === row.id);
  if (index === -1) items.value.push(row);
  else items.value[index] = row;
};

const remove = (id: number): void => {
  items.value = items.value.filter((item) => item.id !== id);
};

const save = async (
  payload: { name: string; definition: FlowRow['definition'] },
  original?: FlowRow,
): Promise<SaveResult> => {
  try {
    const renamed = Boolean(original && payload.name.trim() !== original.name);
    const response = await request(
      `/api/flows/${encodeURIComponent(renamed ? original!.name : payload.name)}`,
      renamed
        ? {
            method: 'PATCH',
            body: JSON.stringify({ ...payload, expected_version: original!.version }),
          }
        : {
            method: 'PUT',
            body: JSON.stringify({
              definition: payload.definition,
              expected_version: original?.version ?? null,
            }),
          },
    );
    if (!response.ok) throw await apiError(response);
    await reload();
    return { ok: true };
  } catch (error) {
    return {
      ok: false,
      error: error && typeof error === 'object' ? (error as FlowError) : { cause: error },
    };
  }
};

const removeSaved = async (flow: FlowRow): Promise<SaveResult> => {
  try {
    const response = await request(
      `/api/flows/${encodeURIComponent(flow.name)}?expected_version=${flow.version}`,
      {
        method: 'DELETE',
      },
    );
    if (response.status !== 204) throw await apiError(response);
    await reload();
    return { ok: true };
  } catch (error) {
    return {
      ok: false,
      error: error && typeof error === 'object' ? (error as FlowError) : { cause: error },
    };
  }
};

const saveLayout = async (
  flow: FlowRow,
  definition: FlowRow['definition'],
): Promise<LayoutResult> => {
  try {
    const response = await request(`/api/flows/${encodeURIComponent(flow.name)}`, {
      method: 'PUT',
      body: JSON.stringify({ definition, expected_version: flow.version }),
    });
    if (!response.ok) throw await apiError(response);
    const server = (await response.json()) as Partial<FlowRow>;
    upsert({
      ...flow,
      definition,
      version: server.version ?? flow.version,
      updated_at: server.updated_at ?? flow.updated_at,
    });
    return { ok: true, server };
  } catch (error) {
    return {
      ok: false,
      error: error && typeof error === 'object' ? (error as FlowError) : { cause: error },
    };
  }
};

const dryRun = async (fileId: number, flow: FlowRow['definition']): Promise<DryRunResult> => {
  const response = await request('/api/flows/dry-run', {
    method: 'POST',
    body: JSON.stringify({ file_id: fileId, flow }),
  });
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as DryRunResult;
};
const normalize = async (definition: FlowDefinition): Promise<FlowDefinition> => {
  const response = await request('/api/flows/normalize', {
    method: 'POST',
    body: JSON.stringify(definition),
  });
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as FlowDefinition;
};
const exportZip = async (): Promise<Blob> => {
  const response = await request('/api/flows/export.zip', {
    headers: { Accept: 'application/zip' },
  });
  if (!response.ok) throw await apiError(response);
  return response.blob();
};

export const useFlows = () => ({
  items: shallowReadonly(items),
  isLoading: shallowReadonly(isLoading),
  lastError: shallowReadonly(lastError),
  ensure,
  reload,
  upsert,
  remove,
  save,
  removeSaved,
  saveLayout,
  dryRun,
  normalize,
  exportZip,
  facts: shallowReadonly(facts),
  presets: shallowReadonly(presets),
  ensureFacts,
  ensurePresets,
});
