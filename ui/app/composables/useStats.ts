import { computed, ref, shallowReadonly } from 'vue';
import type { StatsOverview, StatsResponse } from '~/types/api';
import { parse_api_error, request } from '~/utils';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';

const summary = ref<StatsResponse | null>(null);
const overview = ref<StatsOverview | null>(null);
const libraryTotals = computed(() => {
  const totals = {
    files: 0,
    bytes: 0,
    successful_transcodes: 0,
    successful_remuxes: 0,
    successful_unclassified: 0,
    transcode_saved_bytes: 0,
    remux_saved_bytes: 0,
    unclassified_saved_bytes: 0,
    source_bytes: 0,
    saved_bytes: 0,
    avg_reduction_pct: 0,
  };
  for (const library of overview.value?.libraries ?? []) {
    totals.files += library.files;
    totals.bytes += library.bytes;
    totals.successful_transcodes += library.successful_transcodes;
    totals.successful_remuxes += library.successful_remuxes;
    totals.successful_unclassified += library.successful_unclassified;
    totals.transcode_saved_bytes += library.transcode_saved_bytes;
    totals.remux_saved_bytes += library.remux_saved_bytes;
    totals.unclassified_saved_bytes += library.unclassified_saved_bytes;
    totals.source_bytes += library.source_bytes;
    totals.saved_bytes += library.saved_bytes;
  }
  if (totals.source_bytes) {
    const reduction = (100 * totals.saved_bytes) / totals.source_bytes;
    // Match Python's ties-to-even rounding. Exact one-decimal ties are quarter fractions.
    totals.avg_reduction_pct =
      Math.abs(reduction % 0.5) === 0.25
        ? Math.round(reduction * 5) / 5
        : Number(reduction.toFixed(1));
  }
  return totals;
});
const isLoadingOverview = ref(false);
const overviewError = ref<string | null>(null);
let overviewRequest = 0;

const { messageFor } = useApiErrorMessage();

const loadSummary = async (): Promise<void> => {
  try {
    const response = await request('/api/stats');
    if (response.ok) summary.value = (await response.json()) as StatsResponse;
  } catch {}
};

const setSummary = (payload: StatsResponse): void => {
  summary.value = payload;
};

const loadOverview = async (days: number, libraryId?: number): Promise<void> => {
  const generation = ++overviewRequest;
  isLoadingOverview.value = true;
  overviewError.value = null;
  const params = new URLSearchParams({ days: String(days) });
  if (libraryId !== undefined && libraryId !== 0) params.set('library_id', String(libraryId));
  try {
    const response = await request(`/api/stats/overview?${params}`);
    if (generation !== overviewRequest) return;
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      if (generation !== overviewRequest) return;
      overviewError.value = messageFor(await parse_api_error(body));
      return;
    }
    const body = (await response.json()) as StatsOverview;
    if (generation === overviewRequest) overview.value = body;
  } catch (cause) {
    if (generation === overviewRequest) {
      overviewError.value = `Could not load statistics: ${messageFor(cause instanceof Error ? cause.message : undefined)}`;
    }
  } finally {
    if (generation === overviewRequest) {
      isLoadingOverview.value = false;
    }
  }
};

export const useStats = () => ({
  summary: shallowReadonly(summary),
  loadSummary,
  setSummary,
  overview: shallowReadonly(overview),
  libraryTotals,
  loadOverview,
  isLoadingOverview: shallowReadonly(isLoadingOverview),
  overviewError: shallowReadonly(overviewError),
});
