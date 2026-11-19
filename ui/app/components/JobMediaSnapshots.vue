<template>
  <section class="space-y-3">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
        <UIcon name="i-lucide-camera" class="size-5" />
        Media snapshots
      </h3>
      <UButton
        variant="ghost"
        color="neutral"
        size="sm"
        :trailing-icon="visible ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
        :aria-label="visible ? 'Hide media snapshots' : 'Show media snapshots'"
        :aria-expanded="visible"
        @click="visible = !visible"
      />
    </div>
    <div v-if="visible" class="space-y-3">
      <p class="text-sm text-toned">
        These details were recorded when the job ran and may differ from the current file. Input
        shows the file before this attempt or stage. Recorded output may not have replaced the
        library file.
      </p>
      <USelectMenu
        v-if="snapshots.length > 1"
        v-model="selected"
        :items="snapshotOptions"
        value-key="value"
        icon="i-lucide-layers"
        aria-label="Snapshot attempt or stage"
        class="w-full sm:w-64"
      />
      <UEmpty
        v-if="!current"
        variant="naked"
        size="sm"
        icon="i-lucide-camera"
        title="No attempt recorded."
      />
      <template v-else>
        <StatusBadge v-if="current.result" :status="current.result" />
        <div
          class="grid min-w-0 items-start gap-3 xl:grid-cols-2"
          @scroll.capture.passive="syncTableScroll"
        >
          <section v-for="side in sides" :key="side.label" class="mw-frame min-w-0 space-y-3 p-3">
            <h4 class="flex items-center gap-2 font-semibold text-highlighted">
              <UIcon :name="side.icon" class="size-4" />{{ side.label }}
              <UBadge v-if="side.probe" color="neutral" variant="subtle" size="sm">
                {{ side.probe.streams.length }} streams
              </UBadge>
            </h4>
            <template v-if="side.probe">
              <dl class="grid gap-3 text-sm sm:grid-cols-2">
                <div v-for="row in side.metadata" :key="row.key" class="min-w-0">
                  <dt class="flex items-center gap-1 text-xs text-toned">
                    <UIcon :name="row.icon" class="size-3.5" />{{ row.label }}
                  </dt>
                  <dd class="break-all">{{ row.value }}</dd>
                </div>
              </dl>
              <UTable
                data-stream-table
                :data="side.probe.streams"
                :columns="streamColumns"
                :style="{ '--stream-table-width': `${streamTableWidth}px` }"
                :ui="{
                  base: 'table-fixed w-(--stream-table-width)',
                  th: 'whitespace-nowrap wrap-normal',
                  td: 'whitespace-normal wrap-break-word',
                }"
              />
              <UCollapsible :key="`${attempt?.id}-${current.value}-${side.label}`">
                <template #default="{ open: expanded }">
                  <UButton
                    color="neutral"
                    variant="ghost"
                    size="sm"
                    icon="i-lucide-braces"
                    :trailing-icon="expanded ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
                    :aria-label="`${expanded ? 'Hide' : 'Show'} ${side.label.toLowerCase()} snapshot JSON`"
                    >Full snapshot (JSON)</UButton
                  >
                </template>
                <template #content>
                  <pre class="mask-it mw-terminal mt-2 max-h-72 overflow-auto font-mono text-xs">{{
                    JSON.stringify(side.probe, null, 2)
                  }}</pre>
                </template>
              </UCollapsible>
            </template>
            <template v-else>
              <p class="text-sm text-toned">Not recorded.</p>
              <p v-if="side.size !== null" class="text-sm text-toned">
                Recorded file size: {{ formatBytes(side.size) }}
              </p>
            </template>
          </section>
        </div>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useStorage } from '@vueuse/core';
import type { TableColumn } from '@nuxt/ui';
import type { JobAttemptDetail, JobEventRow, ProbeStream } from '~/types/api';
import { formatBitrate, formatBytes } from '~/utils';
import { jobMetadata } from '~/utils/mediaMetadata';
import { jobSnapshots } from '~/utils/jobSnapshots';

const props = defineProps<{
  attempt: JobAttemptDetail | null;
  events: JobEventRow[];
}>();
const visible = useStorage('job_media_snapshots_visible', true);
const selected = ref('attempt');
const syncedScrollPositions = new WeakMap<HTMLElement, number>();
const syncTableScroll = (event: Event): void => {
  const source = event.target;
  const container = event.currentTarget;
  if (
    !(source instanceof HTMLElement) ||
    !(container instanceof HTMLElement) ||
    !source.hasAttribute('data-stream-table')
  )
    return;
  const syncedPosition = syncedScrollPositions.get(source);
  syncedScrollPositions.delete(source);
  // Mirrored scroll events must not push an older position back into the table being scrolled.
  if (syncedPosition !== undefined && Math.abs(source.scrollLeft - syncedPosition) < 1) return;
  for (const table of container.querySelectorAll<HTMLElement>('[data-stream-table]')) {
    if (table !== source && Math.abs(table.scrollLeft - source.scrollLeft) > 1) {
      table.scrollLeft = source.scrollLeft;
      syncedScrollPositions.set(table, table.scrollLeft);
    }
  }
};
const snapshots = computed(() => jobSnapshots(props.attempt, props.events));
const snapshotOptions = computed(() =>
  snapshots.value.map(({ value, label }) => ({ value, label })),
);
const current = computed(() =>
  snapshots.value.find((snapshot) => snapshot.value === selected.value),
);
const sides = computed(() => {
  const snapshot = current.value;
  if (!snapshot) return [];
  return [
    { label: 'Input', icon: 'i-lucide-log-in', probe: snapshot.input, size: snapshot.sourceSize },
    {
      label: 'Output',
      icon: 'i-lucide-log-out',
      probe: snapshot.output,
      size: snapshot.outputSize,
    },
  ].map((side) => ({
    ...side,
    metadata: jobMetadata(null, { input_probe: side.probe, output_probe: null, result: null }),
  }));
});
watch(
  () => props.attempt?.id,
  () => {
    selected.value = 'attempt';
  },
);

const streamColumns: TableColumn<ProbeStream>[] = [
  { accessorKey: 'index', header: '#', size: 48 },
  {
    accessorKey: 'codec_type',
    header: 'Type',
    size: 96,
    cell: ({ row }) =>
      row.original.disposition_attached_pic ? 'Artwork' : row.original.codec_type,
  },
  {
    accessorKey: 'codec',
    header: 'Codec',
    size: 192,
    meta: { class: { td: 'whitespace-nowrap wrap-normal' } },
  },
  { accessorKey: 'language', header: 'Language', size: 96 },
  { accessorKey: 'title', header: 'Title', size: 288, meta: { class: { td: 'mask-it' } } },
  {
    id: 'resolution',
    header: 'Resolution',
    size: 128,
    cell: ({ row }) =>
      row.original.width && row.original.height
        ? `${row.original.width}×${row.original.height}`
        : '',
  },
  { accessorKey: 'channels', header: 'Channels', size: 96 },
  {
    accessorKey: 'bit_rate',
    header: 'Bitrate',
    size: 112,
    cell: ({ row }) =>
      row.original.codec_type === 'attachment' ? '' : formatBitrate(row.original.bit_rate),
  },
  {
    accessorKey: 'fps',
    header: 'FPS',
    size: 80,
    cell: ({ row }) => (row.original.fps == null ? '' : row.original.fps.toFixed(2)),
  },
  { accessorKey: 'channel_layout', header: 'Layout', size: 160 },
  { accessorKey: 'sample_rate', header: 'Sample rate (Hz)', size: 160 },
  { accessorKey: 'pix_fmt', header: 'Pixel format', size: 144 },
  { accessorKey: 'profile', header: 'Profile', size: 160 },
  {
    id: 'dispositions',
    header: 'Dispositions',
    size: 192,
    cell: ({ row }) =>
      [
        row.original.disposition_default ? 'Default' : '',
        row.original.disposition_forced ? 'Forced' : '',
        row.original.disposition_commentary ? 'Commentary' : '',
        row.original.disposition_attached_pic ? 'Attached picture' : '',
      ]
        .filter(Boolean)
        .join(', '),
  },
  {
    accessorKey: 'filename',
    header: 'Filename',
    size: 288,
  },
  {
    accessorKey: 'mimetype',
    header: 'MIME type',
    size: 192,
  },
];
for (const column of streamColumns) {
  column.meta = { ...column.meta, style: { th: { width: `${column.size}px` } } };
}
const streamTableWidth = streamColumns.reduce((width, column) => width + (column.size ?? 0), 0);
</script>
