<template>
  <UModal
    :open="open"
    title="Dry run"
    :fullscreen="fullscreen"
    :ui="{
      content: [
        fullscreen ? '' : 'max-w-7xl',
        'data-[state=open]:animate-[fade-in_200ms_var(--ease-out)]',
      ],
      body: fullscreen
        ? 'min-h-0 overflow-y-auto p-4 sm:p-6'
        : 'max-h-[75vh] overflow-y-auto p-4 sm:p-6',
    }"
    @update:open="$emit('update:open', $event)"
    @after:leave="$emit('after:leave')"
  >
    <template #body>
      <div :class="fullscreen ? 'flex h-full min-h-0 flex-col gap-5' : 'space-y-5'">
        <div class="flex min-w-0 items-start gap-3">
          <span class="mw-detail-icon size-10 shrink-0">
            <UIcon name="i-lucide-file-video" class="size-5" aria-hidden="true" />
          </span>
          <div class="min-w-0">
            <UTooltip :text="fileName(filePath)">
              <h2 class="truncate text-lg font-semibold text-highlighted">
                <button
                  v-if="onOpenFile"
                  type="button"
                  class="max-w-full truncate text-start hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                  :aria-label="`Open details for ${fileName(filePath)}`"
                  @click="$emit('open-file')"
                >
                  {{ fileName(filePath) }}
                </button>
                <span v-else>{{ fileName(filePath) }}</span>
              </h2>
            </UTooltip>
            <UTooltip :text="filePath">
              <p class="truncate text-sm text-toned">{{ location }}</p>
            </UTooltip>
            <p v-if="facts" class="truncate text-xs text-dimmed">{{ facts }}</p>
          </div>
        </div>
        <UFormField label="Flow" help="Select a flow to evaluate.">
          <USelectMenu
            v-model="selectedFlowId"
            :items="flowItems"
            value-key="value"
            placeholder="Pick a flow"
            class="w-full"
          />
        </UFormField>
        <UAlert
          v-if="error"
          color="error"
          variant="soft"
          icon="i-lucide-circle-alert"
          title="Dry run failed"
          :description="error"
        />
        <template v-if="flowRow">
          <DecisionTraceFlow
            :key="flowRow.id"
            :definition="flowRow.definition"
            :steps="result?.steps"
            :plugins="plugins"
            :fullscreen="fullscreen"
            :class="fullscreen ? 'min-h-0 flex-1' : ''"
          >
            <template #overlay>
              <div
                v-if="result"
                class="pointer-events-none absolute right-3 bottom-3 z-10 max-w-[calc(100%-1.5rem)] rounded-md bg-elevated shadow-sm"
              >
                <UTooltip
                  :text="result.reason ? formatReason(result.reason) : undefined"
                  :disabled="!result.reason"
                  :ui="{
                    content: 'h-auto max-h-80 max-w-sm items-start overflow-y-auto',
                    text: 'min-w-0 overflow-visible text-clip whitespace-pre-line wrap-break-word',
                  }"
                >
                  <StatusBadge
                    :status="result.status"
                    group="flow"
                    tabindex="0"
                    class="pointer-events-auto"
                  />
                </UTooltip>
              </div>
            </template>
          </DecisionTraceFlow>
          <section v-if="streamRows.length" class="space-y-3">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
                <UIcon name="i-lucide-list-checks" class="size-5" />
                Stream decisions
              </h3>
              <div class="flex flex-wrap items-center gap-1.5">
                <UBadge
                  v-for="entry in streamCounts"
                  :key="entry.outcome"
                  :color="entry.color"
                  variant="subtle"
                  size="sm"
                  class="gap-1"
                  ><UIcon :name="entry.icon" class="size-3.5" />{{ entry.label }}:
                  {{ entry.count }}</UBadge
                >
              </div>
            </div>
            <UCollapsible class="w-full">
              <template #default="{ open: expanded }">
                <UButton
                  type="button"
                  color="neutral"
                  variant="ghost"
                  size="xs"
                  class="flex w-full justify-end"
                  :trailing-icon="expanded ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
                  :aria-label="expanded ? 'Collapse stream decisions' : 'Expand stream decisions'"
                >
                  <UBadge color="neutral" variant="subtle" size="sm">
                    {{ streamRows.length }} streams
                  </UBadge>
                </UButton>
              </template>
              <template #content>
                <UTable
                  :data="streamRows"
                  :columns="streamColumns"
                  caption="Stream decisions"
                  sticky="header"
                  tabindex="0"
                  class="mt-2 max-h-80 overscroll-contain"
                  :ui="{ tbody: '[&>tr]:transition-colors [&>tr:hover]:bg-elevated/60' }"
                >
                  <template #title-cell="{ row }">
                    <UTooltip v-if="row.original.title" :text="row.original.title">
                      <p tabindex="0" class="min-w-32 max-w-64 whitespace-pre-wrap wrap-break-word">
                        {{ row.original.title }}
                      </p>
                    </UTooltip>
                  </template>
                  <template #outcome-cell="{ row }">
                    <StatusBadge :status="row.original.outcome" group="stream" />
                  </template>
                  <template #detail-cell="{ row }">
                    <p class="min-w-48 max-w-xl whitespace-pre-wrap wrap-break-word">
                      {{ row.original.detail }}
                    </p>
                  </template>
                </UTable>
              </template>
            </UCollapsible>
          </section>
          <div v-if="argvPreview">
            <p class="mb-1 text-xs font-semibold text-toned">Generated FFmpeg argv</p>
            <pre class="overflow-x-auto rounded-lg bg-elevated p-3 text-xs text-toned">{{
              argvPreview
            }}</pre>
          </div>
        </template>
      </div>
    </template>
    <template #footer>
      <div class="flex w-full flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <UButton
          type="button"
          color="neutral"
          variant="outline"
          :icon="fullscreen ? 'i-lucide-minimize' : 'i-lucide-maximize'"
          class="justify-center"
          @click="fullscreen = !fullscreen"
        >
          {{ fullscreen ? 'Exit fullscreen' : 'Fullscreen' }}
        </UButton>
        <UButton
          type="button"
          color="primary"
          icon="i-lucide-play"
          :loading="running"
          :disabled="!flowRow || running"
          @click="$emit('run')"
          class="justify-center"
          >{{ running ? 'Running dry run' : result ? 'Run again' : 'Run dry run' }}</UButton
        >
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import StatusBadge from '~/components/StatusBadge.vue';
import DecisionTraceFlow from '~/components/DecisionTraceFlow.vue';
import { computed } from 'vue';
import { useStorage } from '@vueuse/core';
import type { TableColumn } from '@nuxt/ui';
import { statusInfo } from '~/utils/status';
import { formatReason } from '~/utils/jobList';
import type { DryRunResult, FlowRow, PluginRow, StreamLogEntry } from '~/types/api';

const props = defineProps<{
  open: boolean;
  flows: FlowRow[];
  filePath: string;
  location: string;
  facts: string;
  running: boolean;
  result: DryRunResult | null;
  error: string;
  argvPreview: string;
  plugins: PluginRow[];
  onOpenFile?: () => void;
}>();
const fullscreen = useStorage('flow_dry_run_fullscreen', false);
const selectedFlowId = defineModel<number | undefined>('selectedFlowId', { default: undefined });
const flowRow = computed(() =>
  props.flows.find((candidate) => candidate.id === selectedFlowId.value),
);
const flowItems = computed(() =>
  (props.flows ?? []).map((flow) => ({ label: flow.name, value: flow.id })),
);
const streamRows = computed(() =>
  [...(props.result?.stream_log ?? [])].sort((a, b) => a.stream_index - b.stream_index),
);
const streamCounts = computed(() => {
  const counts = new Map<string, number>();
  for (const stream of streamRows.value)
    counts.set(stream.outcome, (counts.get(stream.outcome) ?? 0) + 1);
  return Array.from(counts, ([outcome, count]) => ({
    outcome,
    count,
    ...statusInfo(outcome, 'stream'),
  }));
});
const streamColumns: TableColumn<StreamLogEntry>[] = [
  { accessorKey: 'stream_index', header: 'Stream' },
  { accessorKey: 'codec', header: 'Codec', cell: ({ row }) => row.original.codec || 'Unknown' },
  {
    accessorKey: 'language',
    header: 'Language',
    cell: ({ row }) => row.original.language || 'Unavailable',
  },
  { accessorKey: 'title', header: 'Title' },
  { accessorKey: 'outcome', header: 'Decision' },
  { accessorKey: 'detail', header: 'Reason' },
];
const fileName = (path: string): string => path.split(/[\\/]/).at(-1) || path;
defineEmits<{
  'update:open': [open: boolean];
  'open-file': [];
  run: [];
  'after:leave': [];
}>();
</script>
