<template>
  <article class="mw-card-padded flex flex-wrap items-center gap-3 text-default">
    <UCheckbox
      v-if="selectable"
      :model-value="selected"
      :disabled="busy || (!workspace.eligible && !selected)"
      :aria-label="`Select workspace ${workspace.workspace_id}`"
      @update:model-value="$emit('select')"
    />
    <JobProgressRing
      v-if="active && workspace.status"
      :status="workspace.status"
      :percent="percent"
    />
    <span
      v-else
      class="flex size-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary"
    >
      <UIcon name="i-lucide-folder-tree" class="size-5" />
    </span>
    <div class="min-w-0 flex-1">
      <button
        v-if="workspace.job_id !== null"
        type="button"
        class="block max-w-full truncate text-left text-sm font-semibold text-highlighted hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
        @click="$emit('open', workspace.job_id)"
      >
        Job {{ jobNumber(workspace.job_id) }}: {{ workspace.filename ?? 'File unavailable' }}
      </button>
      <h2 v-else class="text-sm font-semibold text-highlighted">
        Workspace {{ workspace.workspace_id }}: Job unavailable
      </h2>
      <div class="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-toned">
        <StatusBadge v-if="workspace.status" :status="workspace.status" />
        <UBadge v-if="phase" color="neutral" variant="subtle" size="sm">{{ phase }}</UBadge>
        <UBadge color="neutral" variant="subtle" size="sm" icon="i-lucide-server">{{
          workspace.node_name ?? workspace.node_id ?? 'No node'
        }}</UBadge>
        <span
          >{{ bytes(workspace.size_bytes) }} used · Input {{ bytes(workspace.source_bytes) }} ·
          Outputs {{ bytes(workspace.output_bytes) }}</span
        >
      </div>
      <p class="mt-1 text-xs text-toned">{{ workspace.reason }}</p>
      <p v-if="workspace.last_activity_at" class="mt-1 text-xs text-dimmed">
        Last filesystem activity
        {{ formatDate(workspace.last_activity_at, 'detail') }}
      </p>
    </div>
    <UButton
      color="neutral"
      variant="ghost"
      size="sm"
      :trailing-icon="expanded ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
      :aria-label="expanded ? 'Hide details' : 'Show details'"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    />
    <WorkspaceFileList
      v-if="expanded"
      :workspace-id="workspace.workspace_id"
      :job-id="workspace.job_id"
      @open="$emit('open', $event)"
    />
  </article>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import type { WorkspaceRow } from '~/types/api';
import { ACTIVE_JOB_STATUSES } from '~/types/api';
import { formatBytes, jobNumber } from '~/utils';
import { formatDate } from '~/utils/date';
import { progressPercent } from '~/utils/progress';

const props = withDefaults(
  defineProps<{
    workspace: WorkspaceRow;
    selectable?: boolean;
    selected?: boolean;
    busy?: boolean;
  }>(),
  { selectable: false, selected: false, busy: false },
);
defineEmits<{ open: [id: number]; select: [] }>();
const expanded = ref(false);
const active = computed(() =>
  ACTIVE_JOB_STATUSES.some((status) => status === props.workspace.status),
);
const phase = computed(() => {
  const value = props.workspace.progress?.['phase'];
  return typeof value === 'string' ? value : null;
});
const bytes = (value: number | null): string =>
  value === null ? 'Unavailable' : formatBytes(value);
const percent = computed(() => progressPercent(props.workspace.progress));
</script>
