<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="logs">
      <template #icon="{ icon }">
        <UIcon
          :name="icon"
          :class="[
            'size-5 transition-colors',
            loading
              ? 'text-info'
              : error
                ? 'text-error'
                : live
                  ? 'motion-safe:animate-pulse text-success'
                  : 'text-muted',
          ]"
          role="img"
          :title="logStatusLabel"
          :aria-label="logStatusLabel"
        />
      </template>
      <template #actions>
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-refresh-cw"
          :loading="loading"
          @click="refresh"
        >
          Refresh
        </UButton>
      </template>
    </PageHeader>

    <UAlert
      v-if="error"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Logs unavailable"
      :description="error"
    >
      <template #actions>
        <UButton
          size="sm"
          color="neutral"
          variant="outline"
          icon="i-lucide-refresh-cw"
          :loading="loading"
          @click="refresh"
        >
          Retry
        </UButton>
      </template>
    </UAlert>
    <section aria-label="Server logs" :aria-busy="loading" class="mw-card overflow-hidden">
      <div class="bg-elevated/30 px-3 py-3 sm:px-4">
        <div class="flex flex-wrap items-center gap-2">
          <UInput
            v-model.lazy="query"
            type="search"
            icon="i-lucide-filter"
            placeholder="Search logs..."
            size="sm"
            class="w-full sm:w-64"
            aria-label="Search logs"
          />
          <UInputMenu
            v-model="logger"
            v-model:open="loggerOpen"
            :items="loggerNames"
            create-item="always"
            :clear="{ 'aria-label': 'Clear logger filter' }"
            icon="i-lucide-filter"
            placeholder="Exact logger name..."
            size="sm"
            class="w-full sm:w-56"
            aria-label="Filter by exact logger name"
            @create="
              (value) => {
                logger = value.trim();
                loggerOpen = false;
              }
            "
          >
            <template #create-item-label="{ item }">Use exact name: {{ item }}</template>
          </UInputMenu>
          <USelectMenu
            v-model="selectedLevels"
            :items="levelItems"
            value-key="value"
            label-key="label"
            multiple
            size="sm"
            icon="i-lucide-list-filter"
            class="w-44 shrink-0 sm:w-48"
            aria-label="Filter log level"
            >{{ levelFilterLabel }}</USelectMenu
          >
          <UButton
            v-if="!atBottom"
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-arrow-down"
            @click="scrollBottom"
          >
            Scroll to bottom
          </UButton>
          <div class="ms-auto flex flex-wrap items-center gap-2">
            <UButton
              color="neutral"
              :variant="wrap ? 'soft' : 'outline'"
              size="sm"
              icon="i-lucide-wrap-text"
              :aria-pressed="wrap"
              @click="wrap = !wrap"
            >
              Wrap
            </UButton>
            <UDropdownMenu
              v-if="filtered.length"
              :items="copyMenuItems"
              :content="{ align: 'end' }"
              :modal="false"
            >
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-copy"
                trailing-icon="i-lucide-chevron-down"
              >
                Copy
              </UButton>
            </UDropdownMenu>
            <UButton
              v-if="filtered.length"
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-download"
              @click="downloadVisible"
            >
              Download visible logs
            </UButton>
          </div>
        </div>
      </div>
      <div
        ref="container"
        class="w-full min-w-0 max-w-full min-h-[55vh] max-h-[65vh] overflow-y-auto overflow-x-hidden border-t border-default font-mono text-sm text-default overscroll-x-contain"
        dir="ltr"
        @scroll.passive="onScroll"
      >
        <div
          v-if="loading && entries.length"
          class="flex justify-center border-b border-default p-3"
          role="status"
        >
          <UIcon name="i-lucide-loader-circle" class="size-4 animate-spin text-toned" />
          <span class="sr-only">Loading logs</span>
        </div>
        <p v-if="truncated" class="border-b border-default px-4 py-3 text-sm text-warning">
          Older log files are no longer available.
        </p>
        <article
          v-for="(entry, index) in filtered"
          :key="entry.id"
          :data-log-id="entry.id"
          class="flex min-w-0 border-b border-default/40 bg-transparent transition-colors duration-150 last:border-b-0 hover:bg-elevated/70"
          :class="index % 2 === 1 ? 'bg-elevated/40' : ''"
        >
          <div
            class="flex w-full min-w-0 flex-col gap-1 px-3 py-[0.65rem] leading-[1.6] md:flex-row md:items-start md:gap-2"
          >
            <span
              class="inline-flex max-w-full flex-wrap items-center gap-x-2 gap-y-1 align-middle md:shrink-0 md:flex-nowrap"
            >
              <UTooltip :text="formatDate(entry.timestamp, 'detail')">
                <span class="inline text-[11px] font-semibold text-toned cursor-pointer">{{
                  formatDate(entry.timestamp, 'clock')
                }}</span>
              </UTooltip>
              <UDropdownMenu
                :items="logMenuItems(entry)"
                :content="{ align: 'start' }"
                :modal="false"
              >
                <UButton
                  color="neutral"
                  variant="ghost"
                  size="sm"
                  icon="i-lucide-ellipsis"
                  square
                  aria-label="Log actions"
                />
              </UDropdownMenu>
              <button
                type="button"
                :class="logLevelBadgeClass(getLogLevel(entry.level))"
                class="inline-flex items-center gap-1.5 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide cursor-pointer"
                :aria-label="`Show ${entry.level} log details`"
                @click="openDetails(entry)"
              >
                <UIcon
                  :name="logLevelIcon(getLogLevel(entry.level))"
                  class="size-3"
                  aria-hidden="true"
                />{{ getLogLevel(entry.level) }}
              </button>
              <span
                class="inline-block max-w-[46vw] truncate align-middle text-[11px] font-semibold text-toned sm:max-w-104"
                :title="entry.logger"
                >[{{ entry.logger }}]</span
              >
            </span>
            <button
              type="button"
              class="block min-w-0 flex-1 cursor-pointer text-start"
              :aria-expanded="expanded.has(entry.id) || wrap"
              @click="toggleRow(entry.id)"
            >
              <span
                class="text-sm text-default"
                :class="
                  expanded.has(entry.id) || wrap
                    ? 'block whitespace-pre-wrap wrap-break-word'
                    : 'line-clamp-2 md:block md:truncate'
                "
                >{{ entry.message
                }}<span v-if="entry.exception" class="text-error/90"
                  >: {{ entry.exception.type
                  }}{{ entry.exception.message ? `: ${entry.exception.message}` : '' }}</span
                ></span
              >
            </button>
          </div>
        </article>
        <div
          v-if="!filtered.length"
          class="flex min-h-[55vh] items-center justify-center px-6 py-8 font-sans"
        >
          <div v-if="loading" class="text-center" role="status">
            <UIcon name="i-lucide-loader-circle" class="size-6 animate-spin text-toned" />
            <span class="sr-only">Loading logs</span>
          </div>
          <UEmpty
            v-else-if="!error"
            variant="naked"
            size="sm"
            :icon="hasFilters ? 'i-lucide-filter-x' : 'i-lucide-scroll-text'"
            :title="hasFilters ? 'No matching logs.' : 'No logs.'"
          />
        </div>
      </div>
    </section>
    <LogDetailModal v-model="detailsOpen" :log="selectedLog" />
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useStorage } from '@vueuse/core';
import PageHeader from '~/components/PageHeader.vue';
import LogDetailModal from '~/components/LogDetailModal.vue';
import { useLogs } from '~/composables/useLogs';
import { copyText } from '~/utils';
import { formatDate } from '~/utils/date';
import type { LogEntry } from '~/types/logs';
import { getLogLevel, logLevels, normalizeLogLevels, type LogLevel } from '~/utils/logLevels';
import { formatLogText, serializeLogs } from '~/utils/logs';

const {
  entries,
  loggerNames,
  loading,
  error,
  live,
  truncated,
  hasOlder,
  start,
  refreshHistory,
  searchHistory,
  loadOlder: fetchOlder,
} = useLogs();
const logStatusLabel = computed(() =>
  loading.value
    ? 'Loading logs'
    : error.value
      ? error.value
      : live.value
        ? 'Live log updates'
        : 'Browsing older logs',
);
const container = ref<HTMLElement | null>(null);
const query = ref('');
const logger = ref<string | null>(null);
const loggerOpen = ref(false);
const wrap = useStorage('mw_logs_text_wrap', false);
const atBottom = ref(true);
const expanded = ref(new Set<number>());
let searchTimer: ReturnType<typeof setTimeout> | null = null;
let scrollFrame: number | null = null;
let mounted = true;
const levels = logLevels;
const storedLevels = useStorage<unknown>('mw_logs_levels', [...levels]);
const selectedLevels = computed<LogLevel[]>({
  get: () => normalizeLogLevels(storedLevels.value),
  set: (value) => {
    storedLevels.value = normalizeLogLevels(value);
  },
});
const levelItems = levels.map((value) => ({
  label: value.charAt(0).toUpperCase() + value.slice(1),
  value,
}));
const levelFilterLabel = computed(() =>
  selectedLevels.value.length === levels.length
    ? 'All levels'
    : selectedLevels.value.length
      ? selectedLevels.value.join(', ')
      : 'No levels',
);
const hasFilters = computed(
  () =>
    !!query.value.trim() || !!logger.value?.trim() || selectedLevels.value.length !== levels.length,
);
const detailsOpen = ref(false);
const notify = useNotification();
const selectedLog = ref<LogEntry | null>(null);
const filtered = computed(() =>
  entries.value.filter((entry) => {
    return (
      selectedLevels.value.includes(getLogLevel(entry.level)) &&
      (!logger.value?.trim() || entry.logger === logger.value.trim()) &&
      (!query.value || JSON.stringify(entry).toLowerCase().includes(query.value.toLowerCase()))
    );
  }),
);
const logLevelIcon = (value: string): string =>
  ({
    error: 'i-lucide-circle-x',
    warning: 'i-lucide-triangle-alert',
    info: 'i-lucide-info',
    debug: 'i-lucide-terminal',
  })[value.toLowerCase()] ?? 'i-lucide-terminal';
const logLevelBadgeClass = (value: string): string => {
  const level = value.toLowerCase();
  return level === 'error'
    ? 'bg-error/15 text-error ring-1 ring-inset ring-error/40'
    : level === 'warning'
      ? 'bg-warning/20 text-warning ring-1 ring-inset ring-warning/50'
      : level === 'info'
        ? 'bg-info/15 text-info ring-1 ring-inset ring-info/40'
        : 'bg-neutral/15 text-default ring-1 ring-inset ring-default';
};
const scrollBottom = (): void => {
  container.value?.scrollTo({ top: container.value.scrollHeight });
  atBottom.value = true;
};
const onScroll = (): void => {
  const node = container.value;
  if (!node) return;
  atBottom.value = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
  if (!atBottom.value && node.scrollTop < 50 && hasOlder.value && !loading.value && !error.value)
    void loadOlder();
};
const toggleRow = (id: number): void => {
  const next = new Set(expanded.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  expanded.value = next;
};
const scheduleScrollBottom = (): void => {
  if (scrollFrame !== null) cancelAnimationFrame(scrollFrame);
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = null;
    if (mounted && atBottom.value) scrollBottom();
  });
};
const loadOlder = async (): Promise<void> => {
  const node = container.value;
  const height = node?.scrollHeight ?? 0;
  const top = node?.scrollTop ?? 0;
  const anchorId = entries.value[0]?.id;
  const anchorTop = node?.querySelector(`[data-log-id="${anchorId}"]`)?.getBoundingClientRect().top;
  atBottom.value = false;
  await fetchOlder();
  if (node)
    await nextTick(() => {
      const anchor = node.querySelector(`[data-log-id="${anchorId}"]`);
      node.scrollTo({
        top:
          anchor && anchorTop !== undefined
            ? top + anchor.getBoundingClientRect().top - anchorTop
            : node.scrollHeight - height + top,
      });
    });
};
const refresh = async (): Promise<void> => {
  atBottom.value = true;
  await refreshHistory();
  await nextTick(scrollBottom);
};
const openDetails = (entry: LogEntry): void => {
  selectedLog.value = entry;
  detailsOpen.value = true;
};
const copyMenuItems = [
  [
    {
      label: 'Copy text',
      icon: 'i-lucide-file-text',
      onSelect: () => copyText(filtered.value.map(formatLogText).join('\n')),
    },
    {
      label: 'Copy raw',
      icon: 'i-lucide-braces',
      onSelect: () => copyText(serializeLogs(filtered.value)),
    },
  ],
];
const logMenuItems = (entry: LogEntry) => [
  [
    {
      label: 'Details',
      icon: 'i-lucide-panel-right-open',
      onSelect: () => openDetails(entry),
    },
    {
      label: 'Copy text',
      icon: 'i-lucide-file-text',
      onSelect: () => copyText(formatLogText(entry)),
    },
    {
      label: 'Copy raw',
      icon: 'i-lucide-braces',
      onSelect: () => copyText(serializeLogs([entry])),
    },
  ],
];
watch([query, selectedLevels, logger], () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchTimer = null;
    void searchHistory(query.value, selectedLevels.value, logger.value ?? '');
  }, 300);
});
watch(
  () => entries.value[0]?.id,
  async (id) => {
    const retained = new Set(entries.value.map((entry) => entry.id));
    expanded.value = new Set([...expanded.value].filter((value) => retained.has(value)));
    if (!live.value || atBottom.value || id === undefined) return;
    const node = container.value;
    const anchor = node?.querySelector(`[data-log-id="${id}"]`);
    if (!node || !anchor) return;
    const top = node.scrollTop;
    const offset = anchor.getBoundingClientRect().top;
    await nextTick();
    const updated = node.querySelector(`[data-log-id="${id}"]`);
    if (updated) node.scrollTo({ top: top + updated.getBoundingClientRect().top - offset });
  },
);
watch(
  () => entries.value.at(-1)?.id,
  async (id, previous) => {
    if (id !== previous && atBottom.value) {
      await nextTick();
      scheduleScrollBottom();
    }
  },
);
const downloadVisible = (): void => {
  let url: string | null = null;
  try {
    const blob = new Blob([serializeLogs(filtered.value)], { type: 'application/x-ndjson' });
    url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'millwright-logs.jsonl';
    link.click();
  } catch {
    notify.error('Unable to download visible logs.');
  } finally {
    if (url) URL.revokeObjectURL(url);
  }
};
onMounted(() => start(selectedLevels.value));
onBeforeUnmount(() => {
  mounted = false;
  if (searchTimer) clearTimeout(searchTimer);
  if (scrollFrame !== null) cancelAnimationFrame(scrollFrame);
});
</script>
