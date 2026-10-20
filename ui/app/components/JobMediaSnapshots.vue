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
      <p v-if="!current" class="text-sm text-toned">No attempt recorded.</p>
      <template v-else>
        <StatusBadge v-if="current.result" :status="current.result" />
        <div class="grid items-start gap-3 xl:grid-cols-2">
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
              <div class="max-h-80 overflow-auto">
                <UTable
                  :data="side.probe.streams"
                  :columns="streamColumns"
                  :ui="{ tbody: '[&>tr]:transition-colors [&>tr:hover]:bg-elevated/60' }"
                />
              </div>
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
                  <pre class="mt-2 max-h-72 overflow-auto font-mono text-xs text-toned">{{
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
  { accessorKey: 'index', header: '#' },
  {
    accessorKey: 'codec_type',
    header: 'Type',
    cell: ({ row }) =>
      row.original.disposition_attached_pic ? 'Artwork' : row.original.codec_type,
  },
  { accessorKey: 'codec', header: 'Codec' },
  { accessorKey: 'language', header: 'Language' },
  { accessorKey: 'title', header: 'Title' },
  {
    id: 'resolution',
    header: 'Resolution',
    cell: ({ row }) =>
      row.original.width && row.original.height
        ? `${row.original.width}×${row.original.height}`
        : '',
  },
  { accessorKey: 'fps', header: 'FPS' },
  { accessorKey: 'pix_fmt', header: 'Pixel format' },
  { accessorKey: 'profile', header: 'Profile' },
  { accessorKey: 'channels', header: 'Channels' },
  { accessorKey: 'channel_layout', header: 'Layout' },
  { accessorKey: 'sample_rate', header: 'Sample rate (Hz)' },
  {
    accessorKey: 'bit_rate',
    header: 'Bitrate',
    cell: ({ row }) =>
      row.original.codec_type === 'attachment' ? '' : formatBitrate(row.original.bit_rate),
  },
  {
    id: 'dispositions',
    header: 'Dispositions',
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
  { accessorKey: 'filename', header: 'Filename' },
  { accessorKey: 'mimetype', header: 'MIME type' },
];
</script>
