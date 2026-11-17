<template>
  <UModal
    :open="open"
    :title="'Log details'"
    :ui="{
      content: 'max-w-5xl',
      body: 'max-h-[75vh] overflow-y-auto',
    }"
    @update:open="emit('update:modelValue', $event)"
  >
    <template #body>
      <div v-if="log" class="space-y-5">
        <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          <StatCard
            :label="'Level'"
            :value="getLogLevel(log.level)"
            :icon="LOG_LEVEL_ICON[getLogLevel(log.level)]"
            :color="LOG_LEVEL_COLOR[getLogLevel(log.level)]"
          />
          <div v-if="log.logger" dir="ltr">
            <StatCard :label="'Logger'" :value="log.logger" icon="i-lucide-tag" />
          </div>
          <StatCard
            :label="'Date'"
            :value="relativeTime(log.timestamp)"
            icon="i-lucide-clock"
            :tooltip="formatDate(log.timestamp, 'detail')"
          />
        </div>

        <div class="flex flex-wrap items-center justify-end gap-2">
          <UDropdownMenu :items="copyMenuItems" :content="{ align: 'end' }" :modal="false">
            <UButton color="neutral" variant="outline" size="sm" icon="i-lucide-copy">
              {{ 'Copy' }}
            </UButton>
          </UDropdownMenu>
        </div>

        <UAlert
          :color="LOG_LEVEL_COLOR[getLogLevel(log.level)]"
          variant="soft"
          :icon="LOG_LEVEL_ICON[getLogLevel(log.level)]"
          title=""
          class="min-w-0"
          dir="ltr"
        >
          <template #description>
            <p class="wrap-break-word w-full font-mono text-sm text-default" dir="ltr">
              {{ log.message }}
            </p>
          </template>
        </UAlert>

        <UAlert
          v-if="exceptionSummary(log)"
          color="error"
          variant="soft"
          icon="i-lucide-badge-alert"
          :title="exceptionSummary(log)"
          dir="ltr"
        />

        <section v-if="log.exception" class="space-y-3 border-t border-default pt-4">
          <div class="flex flex-wrap items-center gap-3">
            <h3 class="min-w-0 flex-1">
              <button
                type="button"
                class="flex w-full items-center gap-2 py-1 text-start text-sm font-semibold text-highlighted"
                :aria-expanded="exceptionOpen"
                :aria-controls="`${detailsId}-exception`"
                @click="exceptionOpen = !exceptionOpen"
              >
                <UIcon
                  name="i-lucide-chevron-right"
                  :class="[
                    'size-4 shrink-0 text-toned transition-transform',
                    exceptionOpen ? 'rotate-90' : '',
                  ]"
                />
                <span>Exception</span>
              </button>
            </h3>
            <div class="flex items-center gap-2">
              <UButton
                size="sm"
                color="neutral"
                :variant="wrapException ? 'soft' : 'outline'"
                :aria-pressed="wrapException"
                aria-label="Wrap exception"
                icon="i-lucide-wrap-text"
                @click="
                  () => {
                    wrapException = !wrapException;
                  }
                "
              >
                <span class="hidden sm:inline">{{ 'Wrap' }}</span>
              </UButton>
              <UTooltip text="Copy exception">
                <UButton
                  size="sm"
                  color="neutral"
                  variant="outline"
                  icon="i-lucide-copy"
                  aria-label="Copy exception"
                  @click="copyText(exceptionText(log), true)"
                />
              </UTooltip>
            </div>
          </div>

          <pre
            v-if="exceptionOpen"
            :id="`${detailsId}-exception`"
            class="mw-terminal max-h-96 overflow-auto"
            dir="ltr"
            :class="wrapException ? 'whitespace-pre-wrap wrap-break-word' : 'whitespace-pre'"
          ><code>{{ exceptionText(log) }}</code></pre>
        </section>

        <section v-if="detailRows.length > 0" class="space-y-3 border-t border-default pt-4">
          <div class="flex items-center gap-3">
            <h3 class="min-w-0 flex-1">
              <button
                type="button"
                class="flex w-full items-center gap-2 py-1 text-start text-sm font-semibold text-highlighted"
                :aria-expanded="sourceOpen"
                :aria-controls="`${detailsId}-source`"
                @click="sourceOpen = !sourceOpen"
              >
                <UIcon
                  name="i-lucide-chevron-right"
                  :class="[
                    'size-4 shrink-0 text-toned transition-transform',
                    sourceOpen ? 'rotate-90' : '',
                  ]"
                />
                <span>Source and runtime</span>
              </button>
            </h3>
            <UTooltip text="Copy source and runtime">
              <UButton
                size="sm"
                color="neutral"
                variant="outline"
                icon="i-lucide-copy"
                aria-label="Copy source and runtime"
                @click="copyText(sourceJson, true)"
              />
            </UTooltip>
          </div>

          <dl
            v-if="sourceOpen"
            :id="`${detailsId}-source`"
            class="grid gap-2 sm:grid-cols-2"
            dir="ltr"
          >
            <div
              v-for="row in detailRows"
              :key="row.label"
              class="rounded-sm border border-default bg-elevated/40 p-3"
            >
              <dt
                class="inline-flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-toned"
              >
                <UIcon :name="row.icon" class="size-3.5" />
                <span>{{ row.label }}</span>
              </dt>
              <dd class="mt-1 wrap-break-word font-mono text-xs text-default">{{ row.value }}</dd>
            </div>
          </dl>
        </section>

        <section v-if="fieldRows.length > 0" class="space-y-3 border-t border-default pt-4">
          <div class="flex flex-wrap items-center gap-3">
            <h3 class="min-w-0 flex-1">
              <button
                type="button"
                class="flex w-full items-center gap-2 py-1 text-start text-sm font-semibold text-highlighted"
                :aria-expanded="fieldsOpen"
                :aria-controls="`${detailsId}-fields`"
                @click="fieldsOpen = !fieldsOpen"
              >
                <UIcon
                  name="i-lucide-chevron-right"
                  :class="[
                    'size-4 shrink-0 text-toned transition-transform',
                    fieldsOpen ? 'rotate-90' : '',
                  ]"
                />
                <span>Fields</span>
              </button>
            </h3>
            <div class="flex items-center gap-2">
              <UButton
                size="sm"
                color="neutral"
                :variant="wrapFields ? 'soft' : 'outline'"
                :aria-pressed="wrapFields"
                aria-label="Wrap fields"
                icon="i-lucide-wrap-text"
                @click="
                  () => {
                    wrapFields = !wrapFields;
                  }
                "
              >
                <span class="hidden sm:inline">{{ 'Wrap' }}</span>
              </UButton>
              <UTooltip text="Copy fields">
                <UButton
                  size="sm"
                  color="neutral"
                  variant="outline"
                  icon="i-lucide-copy"
                  aria-label="Copy fields"
                  @click="copyText(displayedFieldsJson, true)"
                />
              </UTooltip>
            </div>
          </div>

          <div v-if="fieldsOpen" :id="`${detailsId}-fields`" class="space-y-2">
            <UInput
              v-model="fieldsQuery"
              type="search"
              icon="i-lucide-filter"
              :placeholder="'Filter fields'"
              size="sm"
              class="w-full"
              dir="ltr"
            />

            <UEmpty
              v-if="fieldsQuery && 0 === visibleFieldRows.length"
              size="sm"
              variant="naked"
              icon="i-lucide-filter"
              :title="'No matching fields'"
            />

            <div
              v-for="field in visibleFieldRows"
              :key="field.key"
              class="rounded-sm border border-default bg-elevated/40"
            >
              <button
                type="button"
                class="grid w-full grid-cols-[minmax(7rem,0.45fr)_minmax(0,1fr)] items-center gap-3 px-3 py-2 text-start sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)]"
                dir="ltr"
                :aria-label="`${fieldOpen(field.key) ? 'Hide' : 'Show'} ${field.label}`"
                :aria-expanded="fieldOpen(field.key)"
                @click="toggleField(field.key)"
              >
                <span
                  class="min-w-0 truncate text-[11px] font-semibold uppercase tracking-wide text-toned"
                >
                  {{ field.label }}
                </span>
                <div class="flex min-w-0 items-center justify-end gap-2">
                  <span v-if="field.preview" class="min-w-0 truncate text-xs text-toned">
                    {{ field.preview }}
                  </span>
                  <UIcon
                    :name="fieldOpen(field.key) ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
                    class="size-4 shrink-0 text-toned"
                  />
                </div>
              </button>

              <div v-if="fieldOpen(field.key)" class="border-t border-default/70 px-3 py-3">
                <div class="mb-3 flex items-center justify-between gap-3">
                  <div v-if="field.kind !== 'scalar'" class="w-full">
                    <UInput
                      v-model="fieldFilters[field.key]"
                      type="search"
                      icon="i-lucide-filter"
                      :placeholder="'Filter lines'"
                      size="sm"
                      class="w-full"
                      dir="ltr"
                    />
                  </div>
                  <UButton
                    size="sm"
                    color="neutral"
                    variant="outline"
                    icon="i-lucide-copy"
                    :aria-label="`Copy ${field.label}`"
                    @click="copyText(displayedFieldValue(field), true)"
                  />
                </div>

                <UEmpty
                  v-if="fieldFilter(field.key) && 0 === filteredFieldLineCount(field)"
                  size="sm"
                  variant="naked"
                  icon="i-lucide-filter"
                  :title="'No matching lines'"
                />

                <pre
                  v-if="
                    field.kind === 'json' &&
                    (!fieldFilter(field.key) || filteredFieldLineCount(field) > 0)
                  "
                  class="mw-terminal max-h-96 overflow-auto"
                  dir="ltr"
                  :class="wrapFields ? 'whitespace-pre-wrap wrap-break-word' : 'whitespace-pre'"
                ><code>{{ displayedFieldValue(field) }}</code></pre>
                <pre
                  v-else-if="
                    field.kind === 'text' &&
                    (!fieldFilter(field.key) || filteredFieldLineCount(field) > 0)
                  "
                  class="mw-terminal max-h-96 overflow-auto"
                  dir="ltr"
                  :class="wrapFields ? 'whitespace-pre-wrap wrap-break-word' : 'whitespace-pre'"
                ><code>{{ displayedFieldValue(field) }}</code></pre>
                <p
                  v-else-if="field.kind === 'scalar'"
                  class="wrap-break-word font-mono text-xs text-default"
                  dir="ltr"
                >
                  {{ field.value }}
                </p>
              </div>
            </div>
          </div>
        </section>

        <section class="space-y-3 border-t border-default pt-4">
          <div class="flex flex-wrap items-center gap-3">
            <h3 class="min-w-0 flex-1">
              <button
                type="button"
                class="flex w-full items-center gap-2 py-1 text-start text-sm font-semibold text-highlighted"
                :aria-expanded="rawJsonOpen"
                :aria-controls="`${detailsId}-raw`"
                @click="rawJsonOpen = !rawJsonOpen"
              >
                <UIcon
                  name="i-lucide-chevron-right"
                  :class="[
                    'size-4 shrink-0 text-toned transition-transform',
                    rawJsonOpen ? 'rotate-90' : '',
                  ]"
                />
                <span>Raw data</span>
              </button>
            </h3>
            <div class="flex items-center gap-2">
              <UButton
                size="sm"
                color="neutral"
                :variant="wrapRaw ? 'soft' : 'outline'"
                :aria-pressed="wrapRaw"
                aria-label="Wrap raw data"
                icon="i-lucide-wrap-text"
                @click="
                  () => {
                    wrapRaw = !wrapRaw;
                  }
                "
              >
                <span class="hidden sm:inline">{{ 'Wrap' }}</span>
              </UButton>
              <UTooltip text="Copy raw data">
                <UButton
                  size="sm"
                  color="neutral"
                  variant="outline"
                  icon="i-lucide-copy"
                  aria-label="Copy raw data"
                  @click="copyText(displayedRawJson, true)"
                />
              </UTooltip>
            </div>
          </div>

          <div v-if="rawJsonOpen" :id="`${detailsId}-raw`" class="space-y-3">
            <UInput
              v-model="rawJsonFilter"
              type="search"
              icon="i-lucide-filter"
              :placeholder="'Filter raw data'"
              size="sm"
              class="w-full"
              dir="ltr"
            />

            <UEmpty
              v-if="rawJsonFilter && 0 === filteredRawJsonLineCount"
              size="sm"
              variant="naked"
              icon="i-lucide-filter"
              :title="'No matching lines'"
            />

            <pre
              v-if="!rawJsonFilter || filteredRawJsonLineCount > 0"
              class="mw-terminal max-h-96 overflow-auto"
              dir="ltr"
              :class="wrapRaw ? 'whitespace-pre-wrap wrap-break-word' : 'whitespace-pre'"
            ><code>{{ displayedRawJson }}</code></pre>
          </div>
        </section>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { formatDate, type DateInput } from '~/utils/date';
import { useStorage } from '@vueuse/core';
import { computed, ref, useId } from 'vue';
import StatCard from '~/components/StatCard.vue';
import { filterLogTextLines, formatLogText } from '~/utils/logs';
import type { LogEntry } from '~/types/logs';
import { copyText } from '~/utils';
import { formatRelativeTime } from '~/utils/relativeTime';

const relativeTime = (value: DateInput): string => formatRelativeTime(value, 'en');
const detailsId = useId();

const props = defineProps<{
  modelValue?: boolean;
  log?: LogEntry | null;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void;
}>();

const open = computed({
  get: () => props.modelValue ?? false,
  set: (val: boolean) => emit('update:modelValue', val),
});

type LogLevel = 'debug' | 'info' | 'warning' | 'error';
type LogLevelColor = 'neutral' | 'info' | 'warning' | 'error';
type DetailRow = {
  label: string;
  value: string;
  icon: string;
};
type LogFieldRow = {
  key: string;
  label: string;
  value: string;
  preview: string;
  kind: 'scalar' | 'text' | 'json';
};

const LOG_LEVEL_COLOR: Record<LogLevel, LogLevelColor> = {
  debug: 'neutral',
  info: 'info',
  warning: 'warning',
  error: 'error',
};
const LOG_LEVEL_ICON: Record<LogLevel, string> = {
  debug: 'i-lucide-terminal',
  info: 'i-lucide-info',
  warning: 'i-lucide-triangle-alert',
  error: 'i-lucide-circle-x',
};

const exceptionOpen = useStorage<boolean>('mw_logs_exception_open', false);
const fieldsOpen = useStorage<boolean>('mw_logs_fields_open', true);
const rawJsonOpen = useStorage<boolean>('mw_logs_raw_json_open', false);
const sourceOpen = useStorage<boolean>('mw_logs_source_open', true);
const wrapException = useStorage<boolean>('mw_logs_wrap_exception', false);
const wrapFields = useStorage<boolean>('mw_logs_wrap_fields', false);
const wrapRaw = useStorage<boolean>('mw_logs_wrap_raw', false);

const rawJsonFilter = ref('');
const fieldOpenState = ref<Record<string, boolean>>({});
const fieldFilters = ref<Record<string, string>>({});
const fieldsQuery = ref<string>('');

const getLogLevel = (level: string): LogLevel => {
  switch (level.toLowerCase()) {
    case 'info':
      return 'info';
    case 'warning':
    case 'warn':
      return 'warning';
    case 'error':
    case 'critical':
    case 'fatal':
      return 'error';
    default:
      return 'debug';
  }
};

const logRaw = (log: LogEntry): string => JSON.stringify(log, null, 2);

const exceptionSummary = (log: LogEntry): string => {
  const type = log.exception?.type?.trim() ?? '';
  const message = log.exception?.message?.trim() ?? '';

  if (type && message) {
    return `${type}: ${message}`;
  }

  return type || message;
};

const exceptionText = (log: LogEntry): string =>
  log.exception ? JSON.stringify(log.exception, null, 2) : '';

const rawJson = computed(() => (props.log ? logRaw(props.log) : ''));

const sourceJson = computed(() =>
  JSON.stringify(
    {
      source: props.log?.source ?? null,
      process: props.log?.process ?? null,
      thread: props.log?.thread ?? null,
    },
    null,
    2,
  ),
);

const copyMenuItems = computed(() => [
  [
    {
      label: 'Copy text',
      icon: 'i-lucide-file-text',
      onSelect: () => {
        if (props.log) {
          copyText(formatLogText(props.log));
        }
      },
    },
    {
      label: 'Copy raw',
      icon: 'i-lucide-braces',
      onSelect: () => {
        if (props.log) {
          copyText(rawJson.value);
        }
      },
    },
  ],
]);

const filteredRawJsonLines = computed<Array<string>>(() =>
  filterLogTextLines(rawJson.value, rawJsonFilter.value),
);
const filteredRawJsonLineCount = computed<number>(() => filteredRawJsonLines.value.length);
const displayedRawJson = computed<string>(() =>
  rawJsonFilter.value ? filteredRawJsonLines.value.join('\n') : rawJson.value,
);

const formatDetailValue = (value: unknown): string => {
  if (value === undefined || value === null || value === '') {
    return '';
  }

  if (typeof value === 'string') {
    return value;
  }

  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }

  return JSON.stringify(value);
};

const compactRows = (rows: Array<{ label: string; value: unknown; icon: string }>): DetailRow[] =>
  rows
    .map((row) => ({
      label: row.label,
      value: formatDetailValue(row.value),
      icon: row.icon,
    }))
    .filter((row) => Boolean(row.value));

const formatNameId = (name: unknown, id: unknown): string => {
  const nameValue = formatDetailValue(name);
  const idValue = formatDetailValue(id);
  if (nameValue && idValue) {
    return `${nameValue} / ${idValue}`;
  }

  return nameValue || idValue;
};

const detailRows = computed((): DetailRow[] => {
  if (!props.log) return [];
  return compactRows([
    { label: 'File', value: props.log.source?.file, icon: 'i-lucide-file' },
    { label: 'Line', value: props.log.source?.line, icon: 'i-lucide-hash' },
    {
      label: 'Function',
      value: props.log.source?.function,
      icon: 'i-lucide-code-2',
    },
    { label: 'Module', value: props.log.source?.module, icon: 'i-lucide-box' },
    { label: 'Path', value: props.log.source?.path, icon: 'i-lucide-folder-tree' },
    {
      label: 'Process',
      value: formatNameId(props.log.process?.name, props.log.process?.id),
      icon: 'i-lucide-cpu',
    },
    {
      label: 'Thread',
      value: formatNameId(props.log.thread?.name, props.log.thread?.id),
      icon: 'i-lucide-git-branch',
    },
  ]);
});

const formatFieldValue = (value: unknown): string => {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value, null, 2);
};

const isJsonLike = (value: string): boolean => {
  const trimmed = value.trim();
  return (
    ((trimmed.startsWith('{') && trimmed.endsWith('}')) ||
      (trimmed.startsWith('[') && trimmed.endsWith(']'))) &&
    trimmed.length > 1
  );
};

const isJsonContainer = (value: unknown): value is Record<string, unknown> | unknown[] =>
  Array.isArray(value) || (!!value && typeof value === 'object');

const parseJsonContainerString = (value: string): Record<string, unknown> | unknown[] | null => {
  if (!isJsonLike(value)) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    if (Array.isArray(parsed)) return parsed;
    if (parsed && typeof parsed === 'object') return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
  return null;
};

const fieldRows = computed((): LogFieldRow[] => {
  if (!props.log?.fields) return [];
  const rows: LogFieldRow[] = [];
  for (const [key, rawValue] of Object.entries(props.log.fields)) {
    if (rawValue === undefined || rawValue === null || rawValue === '') continue;
    const jsonValue =
      typeof rawValue === 'string'
        ? parseJsonContainerString(rawValue)
        : isJsonContainer(rawValue)
          ? rawValue
          : null;
    const value = jsonValue ? JSON.stringify(jsonValue, null, 2) : formatFieldValue(rawValue);
    const kind: LogFieldRow['kind'] = jsonValue
      ? 'json'
      : typeof rawValue === 'string'
        ? rawValue.includes('\n')
          ? 'text'
          : 'scalar'
        : 'scalar';
    rows.push({
      key,
      label: key,
      value,
      preview: formatFieldValue(rawValue).replaceAll('\n', ' '),
      kind,
    });
  }
  return rows;
});

const visibleFieldRows = computed((): LogFieldRow[] => {
  const query = fieldsQuery.value.trim().toLowerCase();
  if (!query) {
    return fieldRows.value;
  }

  if (query.includes('.')) {
    const keyMatches = fieldRows.value.filter((field) => field.key.toLowerCase().includes(query));
    if (keyMatches.length > 0) {
      return keyMatches;
    }
  }

  return fieldRows.value.filter((field) => {
    const haystack = `${field.key}\n${field.preview}\n${field.value}`.toLowerCase();
    return haystack.includes(query) || filterLogTextLines(field.value, query).length > 0;
  });
});

const displayedFieldsJson = computed(() => {
  const allFields = props.log?.fields ?? {};
  if (!fieldsQuery.value.trim()) {
    return JSON.stringify(allFields, null, 2);
  }

  const visibleKeys = new Set(visibleFieldRows.value.map((f) => f.key));
  const filtered: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(allFields)) {
    if (visibleKeys.has(key)) {
      filtered[key] = value;
    }
  }

  return JSON.stringify(filtered, null, 2);
});

const fieldOpen = (key: string) => fieldOpenState.value[key] ?? false;

const toggleField = (key: string) => {
  fieldOpenState.value = { ...fieldOpenState.value, [key]: !fieldOpen(key) };
};

const fieldFilter = (key: string) => fieldFilters.value[key] ?? '';

const displayedFieldValue = (field: LogFieldRow): string => {
  const query = fieldFilter(field.key);
  if (!query) return field.value;
  return filterLogTextLines(field.value, query).join('\n');
};

const filteredFieldLineCount = (field: LogFieldRow): number =>
  filterLogTextLines(field.value, fieldFilter(field.key)).length;
</script>
