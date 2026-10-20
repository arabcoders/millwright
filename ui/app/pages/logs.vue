<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="logs">
      <template #actions>
        <div class="flex flex-wrap items-center gap-2">
          <UDropdownMenu :items="copyMenuItems" :modal="false">
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
            <UButton
              color="neutral"
              variant="outline"
              size="sm"
              :icon="live ? 'i-lucide-pause' : 'i-lucide-play'"
              @click="setLive(!live)"
            >
              {{ live ? 'Pause live updates' : 'Resume live updates' }}
            </UButton>
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
            <span class="text-sm font-medium text-toned">Copy</span>
          </UDropdownMenu>
          <UButton
            v-if="filtered.length"
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-download"
            @click="downloadVisible"
          >
            Download visible JSONL
          </UButton>
        </div>
      </template>
    </PageHeader>

    <div class="space-y-3">
      <div class="mw-card flex flex-wrap items-center gap-3 p-3 sm:p-4">
        <UInput
          v-model.lazy="query"
          icon="i-lucide-search"
          placeholder="Search logs..."
          size="sm"
          class="w-full sm:w-72"
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
          class="w-full sm:w-72"
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
          class="w-full sm:w-48"
          aria-label="Filter log level"
          >{{ levelFilterLabel }}</USelectMenu
        >
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          :icon="wrap ? 'i-lucide-wrap-text' : 'i-lucide-align-justify'"
          :aria-pressed="wrap"
          @click="wrap = !wrap"
        >
          Wrap
        </UButton>
      </div>
    </div>

    <UAlert v-if="error" color="error" variant="soft" title="Logs unavailable" :description="error">
      <template #actions
        ><UButton size="xs" color="error" variant="outline" @click="refresh"
          >Retry</UButton
        ></template
      >
    </UAlert>
    <div class="mw-card overflow-hidden">
      <div
        ref="container"
        class="max-h-[65vh] min-h-96 overflow-y-auto font-mono text-sm"
        @scroll.passive="onScroll"
      >
        <div v-if="hasOlder" class="border-b border-default p-3 text-center">
          <UButton size="xs" color="neutral" variant="outline" :loading="loading" @click="loadOlder"
            >Load older</UButton
          >
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
                  size="xs"
                  icon="i-lucide-ellipsis-vertical"
                  trailing-icon="i-lucide-chevron-down"
                  aria-label="Log actions"
                  class="inline-flex h-5! align-[-0.2em] opacity-70 hover:opacity-100"
                  :ui="{ base: 'min-h-0 min-w-0', leadingIcon: 'size-3', trailingIcon: 'size-3' }"
                >
                  <span class="text-[10px] font-medium">Actions</span>
                </UButton>
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
                class="block text-sm text-default"
                :class="
                  expanded.has(entry.id) || wrap
                    ? 'whitespace-pre-wrap wrap-break-word'
                    : 'message-collapsed md:truncate'
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
        <p v-if="!loading && !filtered.length" class="p-10 text-center text-sm text-toned">
          No matching logs.
        </p>
        <div v-if="loading && !entries.length" class="p-10 text-center">
          <UIcon name="i-lucide-loader-circle" class="size-6 animate-spin text-toned" />
        </div>
      </div>
    </div>
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
import { serializeLogs } from '~/utils/logs';

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
  setLive,
} = useLogs();
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
  if (node) atBottom.value = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
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
  await refreshHistory();
  if (atBottom.value) await nextTick(scrollBottom);
};
const openDetails = (entry: LogEntry): void => {
  selectedLog.value = entry;
  detailsOpen.value = true;
};
const rendered = (entry: LogEntry): string =>
  `${formatDate(entry.timestamp, 'timestamp')} ${entry.level.toUpperCase()} [${entry.logger}] ${entry.message}${entry.exception ? `: ${entry.exception.type}: ${entry.exception.message ?? ''}` : ''}`;
const copyMenuItems = [
  [
    {
      label: 'Copy rendered',
      icon: 'i-lucide-file-text',
      onSelect: () => copyText(filtered.value.map(rendered).join('\n')),
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
      label: 'Copy message',
      icon: 'i-lucide-message-square-text',
      onSelect: () => copyText(entry.message),
    },
    {
      label: 'Copy rendered',
      icon: 'i-lucide-file-text',
      onSelect: () => copyText(rendered(entry)),
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
