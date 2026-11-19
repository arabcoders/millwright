<template>
  <main class="w-full min-w-0 max-w-full space-y-6" :aria-busy="loading">
    <PageHeader page="stats">
      <template #actions
        ><div class="flex flex-wrap justify-end gap-2">
          <USelectMenu
            value-key="value"
            v-model="days"
            :items="rangeItems"
            size="sm"
            class="w-32"
            aria-label="Time range"
          />
          <USelectMenu
            value-key="value"
            v-model="libraryId"
            :items="libraryItems"
            :placeholder="libraryId ? `Library #${libraryId}` : 'All libraries'"
            :loading="librariesLoading"
            :disabled="librariesLoading"
            size="sm"
            class="w-44"
            aria-label="Library"
          />
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-refresh-cw"
            :loading="loading || librariesLoading"
            @click="reload"
            >Refresh</UButton
          >
        </div></template
      >
    </PageHeader>
    <UAlert
      v-if="librariesError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load libraries"
      :description="librariesError.message"
    >
      <template #actions>
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-refresh-cw"
          :loading="librariesLoading"
          @click="reload"
          >Retry</UButton
        >
      </template>
    </UAlert>
    <UAlert
      v-if="error"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Statistics loading failed"
      :description="error"
    />
    <UAlert
      v-if="loading"
      color="info"
      variant="soft"
      icon="i-lucide-loader-circle"
      title="Loading statistics"
      :ui="{ icon: 'animate-spin' }"
    />
    <template v-if="overview">
      <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Current files"
          :value="overview.inventory.files.toLocaleString('en')"
          icon="i-lucide-files"
          :hint="formatBytes(overview.inventory.bytes)"
        />
        <StatCard
          label="Completed"
          :value="overview.jobs.completed.toLocaleString('en')"
          icon="i-lucide-circle-check"
          color="info"
          :hint="
            overview.jobs.avg_duration_s
              ? `Avg ${formatTime(overview.jobs.avg_duration_s)}`
              : 'No duration recorded'
          "
        />
        <StatCard
          label="Saved in range"
          :value="formatBytes(windowSaved)"
          icon="i-lucide-calendar-range"
          color="success"
          :hint="`${windowFiles.toLocaleString('en')} successful jobs`"
        />
        <StatCard
          label="Total saved"
          :value="formatBytes(overview.savings.totals.saved_bytes)"
          icon="i-lucide-hard-drive-download"
          color="success"
          :hint="`${overview.savings.totals.files.toLocaleString('en')} successful processing jobs · ${overview.savings.totals.avg_reduction_pct}% avg reduction`"
        />
      </div>
      <div class="grid gap-3 sm:grid-cols-2">
        <StatCard
          v-for="item in processingSavings"
          :key="item.kind"
          :label="`${item.label} saved in range`"
          :value="formatBytes(item.rangeSaved)"
          :icon="item.icon"
          :color="item.kind === 'unclassified' ? 'neutral' : 'success'"
          :hint="`${item.rangeCount.toLocaleString('en')} jobs · ${formatBytes(item.totalSaved)} all time`"
          :tooltip="
            item.kind === 'unclassified'
              ? 'Job details are unavailable. These savings cannot be split between encoding and remuxing.'
              : `${item.totalCount.toLocaleString('en')} successful ${item.label.toLowerCase()} jobs all time`
          "
        />
      </div>
      <div class="grid gap-4 xl:grid-cols-3">
        <section class="mw-card min-w-0 space-y-3 p-4">
          <div class="flex flex-wrap justify-between gap-1">
            <h2 class="text-sm font-semibold text-highlighted">Space saved</h2>
            <span class="text-xs text-toned">Selected range</span>
          </div>
          <Chart
            :values="seriesValues"
            :timestamps="seriesTimestamps"
            label="Bytes saved"
            icon="i-lucide-chart-column"
            color="success"
            :height="105"
            :format-value="formatBytes"
            :format-timestamp="formatDay"
          />
        </section>
        <section class="mw-card min-w-0 space-y-3 p-4">
          <div class="flex flex-wrap justify-between gap-1">
            <h2 class="text-sm font-semibold text-highlighted">Files added</h2>
            <span class="text-xs text-toned">First indexed</span>
          </div>
          <Chart
            :values="acquiredFiles"
            :timestamps="acquisitionTimestamps"
            label="Files"
            icon="i-lucide-files"
            color="info"
            :height="105"
            :format-timestamp="formatDay"
          />
        </section>
        <section class="mw-card min-w-0 space-y-3 p-4">
          <div class="flex flex-wrap justify-between gap-1">
            <h2 class="text-sm font-semibold text-highlighted">Media size added</h2>
            <UTooltip
              text="Earliest retained pre-conversion sizes, grouped by first indexed date. Files without recorded input sizes use their current catalog size. This is not necessarily the original download size."
            >
              <span class="text-xs text-toned">Pre-conversion sizes</span>
            </UTooltip>
          </div>
          <Chart
            :values="acquiredBytes"
            :timestamps="acquisitionTimestamps"
            label="Bytes"
            icon="i-lucide-hard-drive"
            color="warning"
            :height="105"
            :format-value="formatBytes"
            :format-timestamp="formatDay"
          />
          <p v-if="currentSizeFiles" class="text-xs text-toned">
            {{ currentSizeFiles.toLocaleString('en') }}
            {{ currentSizeFiles === 1 ? 'file uses its' : 'files use their' }} current size because
            no input size is recorded.
          </p>
        </section>
      </div>
      <section class="space-y-3" aria-label="Job outcomes">
        <div class="flex items-center gap-2">
          <UIcon name="i-lucide-list-checks" class="size-4 text-toned" />
          <h2 class="text-sm font-semibold text-highlighted">Job outcomes</h2>
        </div>
        <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <StatsDistributionCard
            v-for="item in jobOutcomes"
            :key="item.kind"
            :title="item.label"
            status-group="job"
            :icon="item.icon"
            show-zero
            :values="overview.jobs.by_kind[item.kind] ?? {}"
          />
        </div>
      </section>
      <section class="space-y-3" aria-label="Current media inventory">
        <div class="flex items-center gap-2">
          <UIcon name="i-lucide-chart-no-axes-combined" class="size-4 text-toned" />
          <h2 class="text-sm font-semibold text-highlighted">Current media inventory</h2>
        </div>
        <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          <StatsDistributionCard
            title="Containers"
            icon="i-lucide-box"
            :values="overview.inventory.containers"
          />
          <StatsDistributionCard
            title="Resolutions"
            icon="i-lucide-monitor"
            :values="overview.inventory.resolutions"
          />
          <StatsDistributionCard
            title="Video codecs"
            icon="i-lucide-film"
            :values="overview.inventory.video_codecs"
          >
            <template #empty>No indexed video tracks.</template>
          </StatsDistributionCard>
          <StatsDistributionCard
            title="Audio tracks by codec"
            icon="i-lucide-music"
            :values="overview.inventory.audio_codecs"
          >
            <template #empty>No indexed audio tracks.</template>
          </StatsDistributionCard>
          <StatsDistributionCard
            title="Audio tracks by language"
            icon="i-lucide-languages"
            :values="overview.inventory.audio_languages"
          >
            <template #label="{ row }">{{ languageName(row.key) }}</template>
            <template #empty>No indexed audio tracks.</template>
          </StatsDistributionCard>
          <StatsDistributionCard
            title="Health status"
            status-group="health"
            icon="i-lucide-heart-pulse"
            :values="overview.inventory.health_status"
          />
          <StatsDistributionCard
            title="Subtitle tracks by codec"
            icon="i-lucide-captions"
            :values="overview.inventory.subtitle_codecs"
          >
            <template #empty>No indexed subtitle tracks.</template>
          </StatsDistributionCard>
          <StatsDistributionCard
            title="Subtitle tracks by language"
            icon="i-lucide-languages"
            :values="overview.inventory.subtitle_languages"
          >
            <template #label="{ row }">{{ languageName(row.key) }}</template>
            <template #empty>No indexed subtitle tracks.</template>
          </StatsDistributionCard>
          <StatsDistributionCard
            title="Transcode status"
            status-group="media"
            icon="i-lucide-settings-2"
            :values="overview.inventory.transcode_status"
          />
        </div>
      </section>
      <section class="space-y-3" aria-label="All-time savings by library">
        <div class="flex items-center gap-2">
          <UIcon name="i-lucide-library" class="size-4 text-toned" />
          <div>
            <h2 class="text-sm font-semibold text-highlighted">All-time savings by library</h2>
            <p class="text-xs text-toned">All libraries</p>
          </div>
        </div>
        <UEmpty
          v-if="!overview.libraries.length"
          size="sm"
          icon="i-lucide-library"
          title="No library savings available."
        />
        <div v-else class="overflow-x-auto">
          <UTable :data="overview.libraries" :columns="libraryColumns">
            <template #files-cell="{ row }"
              ><span class="tabular-nums">{{
                row.original.files.toLocaleString('en')
              }}</span></template
            >
            <template #bytes-cell="{ row }">{{ formatBytes(row.original.bytes) }}</template>
            <template #successful_transcodes-cell="{ row }"
              ><span class="tabular-nums">{{
                row.original.successful_transcodes.toLocaleString('en')
              }}</span></template
            >
            <template #successful_remuxes-cell="{ row }"
              ><span class="tabular-nums">{{
                row.original.successful_remuxes.toLocaleString('en')
              }}</span></template
            >
            <template #successful_unclassified-cell="{ row }"
              ><span class="tabular-nums">{{
                row.original.successful_unclassified.toLocaleString('en')
              }}</span></template
            >
            <template #transcode_saved_bytes-cell="{ row }">{{
              formatBytes(row.original.transcode_saved_bytes)
            }}</template>
            <template #remux_saved_bytes-cell="{ row }">{{
              formatBytes(row.original.remux_saved_bytes)
            }}</template>
            <template #unclassified_saved_bytes-cell="{ row }">{{
              formatBytes(row.original.unclassified_saved_bytes)
            }}</template>
            <template #source_bytes-cell="{ row }">{{
              formatBytes(row.original.source_bytes)
            }}</template>
            <template #saved_bytes-cell="{ row }"
              ><span :class="row.original.saved_bytes > 0 ? 'text-success' : undefined">{{
                formatBytes(row.original.saved_bytes)
              }}</span></template
            >
            <template #avg_reduction_pct-cell="{ row }"
              ><span class="tabular-nums">{{ row.original.avg_reduction_pct }}%</span></template
            >
            <template #files-footer
              ><span class="tabular-nums">{{
                libraryTotals.files.toLocaleString('en')
              }}</span></template
            >
            <template #bytes-footer>{{ formatBytes(libraryTotals.bytes) }}</template>
            <template #successful_transcodes-footer
              ><span class="tabular-nums">{{
                libraryTotals.successful_transcodes.toLocaleString('en')
              }}</span></template
            >
            <template #successful_remuxes-footer
              ><span class="tabular-nums">{{
                libraryTotals.successful_remuxes.toLocaleString('en')
              }}</span></template
            >
            <template #successful_unclassified-footer
              ><span class="tabular-nums">{{
                libraryTotals.successful_unclassified.toLocaleString('en')
              }}</span></template
            >
            <template #transcode_saved_bytes-footer>{{
              formatBytes(libraryTotals.transcode_saved_bytes)
            }}</template>
            <template #remux_saved_bytes-footer>{{
              formatBytes(libraryTotals.remux_saved_bytes)
            }}</template>
            <template #unclassified_saved_bytes-footer>{{
              formatBytes(libraryTotals.unclassified_saved_bytes)
            }}</template>
            <template #source_bytes-footer>{{ formatBytes(libraryTotals.source_bytes) }}</template>
            <template #saved_bytes-footer
              ><span :class="libraryTotals.saved_bytes > 0 ? 'text-success' : undefined">{{
                formatBytes(libraryTotals.saved_bytes)
              }}</span></template
            >
            <template #avg_reduction_pct-footer
              ><span class="tabular-nums">{{ libraryTotals.avg_reduction_pct }}%</span></template
            >
          </UTable>
        </div>
      </section>
    </template>
  </main>
</template>

<script setup lang="ts">
import { useStorage } from '@vueuse/core';
import { computed, onMounted, watch } from 'vue';
import type { TableColumn } from '@nuxt/ui';
import Chart from '~/components/Chart.vue';
import PageHeader from '~/components/PageHeader.vue';
import StatCard from '~/components/StatCard.vue';
import StatsDistributionCard from '~/components/StatsDistributionCard.vue';
import { useLibraries } from '~/composables/useLibraries';
import { useStats } from '~/composables/useStats';
import { formatBytes, formatTime } from '~/utils';
import { formatDate, type DateInput } from '~/utils/date';
import type { StatsLibraryRow } from '~/types/api';

const rangeItems = [7, 30, 90, 365].map((value) => ({
  label: `${value === 365 ? '1 year' : `${value} days`}`,
  value,
}));
const days = useStorage('stats_range_days', 30);
const libraryId = useStorage('stats_library_id', 0);
const {
  items: libraries,
  isLoading: librariesLoading,
  lastError: librariesError,
  ensure: ensureLibraries,
} = useLibraries();
const {
  overview,
  libraryTotals,
  loadOverview,
  isLoadingOverview: loading,
  overviewError: error,
} = useStats();
const libraryItems = computed(() => [
  { label: 'All libraries', value: 0 },
  ...libraries.value.map((library) => ({ label: library.name, value: library.id })),
]);
const libraryColumns = computed<TableColumn<StatsLibraryRow>[]>(() => [
  { accessorKey: 'name', header: 'Library', footer: 'Total' },
  { accessorKey: 'files', header: 'Files' },
  { accessorKey: 'bytes', header: 'Current size' },
  { accessorKey: 'successful_transcodes', header: 'Transcodes' },
  { accessorKey: 'successful_remuxes', header: 'Remuxes' },
  ...(libraryTotals.value.successful_unclassified
    ? [{ accessorKey: 'successful_unclassified', header: 'Unclassified' }]
    : []),
  { accessorKey: 'source_bytes', header: 'Source processed' },
  { accessorKey: 'transcode_saved_bytes', header: 'Transcode saved' },
  { accessorKey: 'remux_saved_bytes', header: 'Remux saved' },
  ...(libraryTotals.value.successful_unclassified
    ? [{ accessorKey: 'unclassified_saved_bytes', header: 'Unclassified saved' }]
    : []),
  { accessorKey: 'saved_bytes', header: 'Total saved' },
  { accessorKey: 'avg_reduction_pct', header: 'Reduction' },
]);
const processingKinds = [
  { kind: 'transcode', label: 'Transcode', icon: 'i-lucide-film' },
  { kind: 'remux', label: 'Remux', icon: 'i-lucide-arrow-right-left' },
  { kind: 'unclassified', label: 'Unclassified processing', icon: 'i-lucide-circle-help' },
] as const;
const processingSavings = computed(() => {
  const savings = overview.value?.savings;
  if (!savings) return [];
  return processingKinds
    .filter((item) => item.kind !== 'unclassified' || savings.by_kind[item.kind].totals.files > 0)
    .map((item) => {
      const breakdown = savings.by_kind[item.kind];
      return {
        ...item,
        rangeSaved: breakdown.series.reduce((sum, point) => sum + point.saved_bytes, 0),
        rangeCount: breakdown.series.reduce((sum, point) => sum + point.files, 0),
        totalSaved: breakdown.totals.saved_bytes,
        totalCount: breakdown.totals.files,
      };
    });
});
const outcomeKinds = [
  { kind: 'probe', label: 'Metadata probe', icon: 'i-lucide-search' },
  { kind: 'mkv_stats', label: 'MKV statistics', icon: 'i-lucide-chart-no-axes-combined' },
  { kind: 'healthcheck', label: 'Health check', icon: 'i-lucide-heart-pulse' },
  { kind: 'remux', label: 'Remux', icon: 'i-lucide-arrow-right-left' },
  { kind: 'transcode', label: 'Transcode', icon: 'i-lucide-film' },
  { kind: 'not_run', label: 'Skipped / Not run', icon: 'i-lucide-skip-forward' },
  { kind: 'unclassified', label: 'Unclassified processing', icon: 'i-lucide-circle-help' },
];
const languageNames = new Intl.DisplayNames(['en'], { type: 'language', fallback: 'none' });
const languageName = (code: string): string => {
  if (code === 'und') return 'Unknown language';
  return /^[a-z]{3}$/.test(code) ? (languageNames.of(code) ?? code) : code;
};
const jobOutcomes = computed(() =>
  outcomeKinds.filter(
    (item) =>
      (item.kind !== 'not_run' && item.kind !== 'unclassified') ||
      Object.values(overview.value?.jobs.by_kind[item.kind] ?? {}).some((count) => count > 0),
  ),
);
const seriesValues = computed(() =>
  (overview.value?.savings.series ?? []).map((point) => point.saved_bytes),
);
const seriesTimestamps = computed(() =>
  (overview.value?.savings.series ?? []).map((point) => point.date),
);
const acquiredFiles = computed(() =>
  (overview.value?.acquisition.series ?? []).map((point) => point.files),
);
const acquiredBytes = computed(() =>
  (overview.value?.acquisition.series ?? []).map((point) => point.bytes),
);
const currentSizeFiles = computed(() =>
  (overview.value?.acquisition.series ?? []).reduce(
    (sum, point) => sum + point.current_size_files,
    0,
  ),
);
const acquisitionTimestamps = computed(() =>
  (overview.value?.acquisition.series ?? []).map((point) => point.date),
);
const windowSaved = computed(() =>
  (overview.value?.savings.series ?? []).reduce((sum, point) => sum + point.saved_bytes, 0),
);
const windowFiles = computed(() =>
  (overview.value?.savings.series ?? []).reduce((sum, point) => sum + point.files, 0),
);
const formatDay = (ts: DateInput) => formatDate(ts, 'shortDate');
const reload = async (): Promise<void> => {
  await ensureLibraries();
  if (
    !librariesError.value &&
    libraryId.value &&
    !libraries.value.some((library) => library.id === libraryId.value)
  ) {
    libraryId.value = 0;
  } else await loadOverview(days.value, libraryId.value);
};
onMounted(reload);
watch([days, libraryId], reload);
</script>
