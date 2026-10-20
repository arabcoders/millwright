<template>
  <div class="w-full min-w-0 space-y-3 border-t border-muted pt-3">
    <div class="flex flex-wrap items-center gap-2">
      <UButton
        v-if="jobId !== null"
        color="neutral"
        variant="ghost"
        size="xs"
        icon="i-lucide-scroll-text"
        @click="$emit('open', jobId)"
        >Job details and logs</UButton
      >
      <div class="ml-auto flex items-center gap-2">
        <UButton
          color="neutral"
          variant="outline"
          size="xs"
          icon="i-lucide-refresh-cw"
          :loading="loading"
          @click="load"
          >Refresh</UButton
        >
      </div>
    </div>
    <UAlert
      v-if="error || metadata?.error"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load details"
      :description="error ?? metadata?.error ?? undefined"
    />
    <USkeleton v-if="loading && !metadata" class="h-16 rounded-lg" />
    <p
      v-else-if="metadata && !groups.length && !error && !metadata.error"
      class="py-2 text-sm text-toned"
    >
      No temporary files remain.
    </p>
    <section v-for="group in groups" :key="group.label" class="space-y-2">
      <div class="flex flex-wrap items-center gap-2">
        <h3 class="text-sm font-semibold text-highlighted">
          {{ group.label }}
        </h3>
        <StatusBadge v-if="group.attempt" :status="group.attempt.status" />
        <span
          v-if="group.attempt?.result && group.attempt.result !== group.attempt.status"
          class="text-xs text-toned"
          >{{ group.attempt.result }}</span
        >
        <span v-if="group.attempt?.node_name" class="text-xs text-toned">{{
          group.attempt.node_name
        }}</span>
      </div>
      <UTable
        :data="group.files"
        :columns="columns"
        :aria-label="group.label"
        tabindex="0"
        class="mw-frame"
        :ui="{
          base: 'w-full min-w-[30rem] table-fixed',
          th: 'px-3 py-2 text-xs',
          td: 'px-3 py-2 text-xs align-top',
          tbody: '[&>tr]:transition-colors [&>tr:hover]:bg-elevated/60',
        }"
      >
        <template #path-cell="{ row }">
          <span class="whitespace-normal break-all font-mono">{{ row.original.path }}</span>
        </template>
        <template #size_bytes-cell="{ row }">
          <span class="whitespace-nowrap tabular-nums">{{
            formatBytes(row.original.size_bytes)
          }}</span>
        </template>
        <template #modified_at-cell="{ row }">
          <time :datetime="row.original.modified_at" class="whitespace-nowrap">{{
            formatDate(row.original.modified_at, 'detail')
          }}</time>
        </template>
      </UTable>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import type { TableColumn } from '@nuxt/ui';
import { useWorkspaces } from '~/composables/useWorkspaces';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import { formatBytes } from '~/utils';
import { formatDate } from '~/utils/date';
import type { WorkspaceDetail } from '~/types/api';

const props = defineProps<{ workspaceId: number; jobId: number | null }>();
defineEmits<{ open: [id: number] }>();
const { detail, snapshot } = useWorkspaces();
const { messageFor } = useApiErrorMessage();
const manual = ref<WorkspaceDetail | null>(null);
const columns: TableColumn<WorkspaceDetail['files'][number]>[] = [
  { accessorKey: 'path', header: 'Path' },
  {
    accessorKey: 'size_bytes',
    header: 'Size',
    meta: { class: { th: 'w-24' } },
  },
  {
    accessorKey: 'modified_at',
    header: 'Modified',
    meta: { class: { th: 'w-48' } },
  },
];
const metadata = computed(() => {
  const sampled = snapshot.value?.items.find((item) => item.workspace_id === props.workspaceId);
  if (sampled && (!manual.value || sampled.sampled_at >= manual.value.sampled_at)) return sampled;
  return manual.value;
});
const loading = ref(false);
const error = ref<string | null>(null);
const load = async (): Promise<void> => {
  if (loading.value) return;
  loading.value = true;
  error.value = null;
  try {
    manual.value = await detail(props.workspaceId);
  } catch (cause) {
    error.value = messageFor(cause instanceof Error ? cause.message : undefined);
  } finally {
    loading.value = false;
  }
};
const groups = computed(() => {
  if (!metadata.value) return [];
  const current = metadata.value;
  const numbers = new Set<number>();
  for (const file of current.files) {
    const number = file.path.match(/\/attempts\/(\d+)\//)?.[1];
    if (number !== undefined) numbers.add(Number(number));
  }
  return [
    {
      label: 'Source and workspace files',
      attempt: undefined,
      files: current.files.filter((file) => !file.path.includes('/attempts/')),
    },
    ...[...numbers]
      .sort((a, b) => a - b)
      .map((number) => ({
        label: `Attempt ${number}`,
        attempt: current.attempts.find((attempt) => attempt.attempt_number === number),
        files: current.files.filter((file) => file.path.includes(`/attempts/${number}/`)),
      })),
  ].filter((group) => group.files.length > 0);
});
onMounted(() => {
  if (!metadata.value) void load();
});
</script>
