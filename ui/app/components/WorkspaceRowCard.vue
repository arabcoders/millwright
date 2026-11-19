<template>
  <article
    class="grid min-w-0 gap-3 p-4 text-default sm:p-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,0.8fr)_auto]"
    :class="{ 'bg-primary/5': selected }"
  >
    <div class="flex min-w-0 items-start gap-3">
      <JobProgressRing
        v-if="active && workspace.status"
        layout="icon"
        :status="workspace.status"
        :percent="percent"
      />
      <div class="min-w-0 flex-1">
        <h2 class="wrap-anywhere text-base font-semibold text-highlighted">
          <button
            v-if="workspace.job_id !== null"
            type="button"
            class="block w-full text-left hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
            @click="$emit('open', workspace.job_id)"
          >
            Job {{ jobNumber(workspace.job_id) }}: {{ workspace.filename ?? 'File unavailable' }}
          </button>
          <template v-else>Workspace {{ workspace.workspace_id }}: Job unavailable</template>
        </h2>
        <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-toned">
          <StatusBadge v-if="workspace.status" :status="workspace.status" />
          <UBadge
            v-if="phase"
            color="neutral"
            variant="subtle"
            size="sm"
            class="wrap-anywhere whitespace-normal"
            >{{ phase }}</UBadge
          >
          <span class="inline-flex min-w-0 items-start gap-1.5">
            <UIcon name="i-lucide-server" class="size-3.5 shrink-0" aria-hidden="true" />
            <span class="wrap-anywhere">{{
              workspace.node_name ?? workspace.node_id ?? 'No node'
            }}</span>
          </span>
        </div>
        <p class="mt-2 wrap-anywhere text-xs text-toned">{{ workspace.reason }}</p>
      </div>
    </div>
    <dl class="grid min-w-0 grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
      <div class="min-w-0 space-y-1">
        <dt class="flex items-center gap-1.5 text-xs text-muted">
          <UIcon name="i-lucide-hard-drive" class="size-3.5 shrink-0" aria-hidden="true" />
          Size used
        </dt>
        <dd class="tabular-nums">{{ bytes(workspace.size_bytes) }}</dd>
      </div>
      <div class="min-w-0 space-y-1">
        <dt class="flex items-center gap-1.5 text-xs text-muted">
          <UIcon name="i-lucide-file-input" class="size-3.5 shrink-0" aria-hidden="true" />
          Input
        </dt>
        <dd class="tabular-nums">{{ bytes(workspace.source_bytes) }}</dd>
      </div>
      <div class="min-w-0 space-y-1">
        <dt class="flex items-center gap-1.5 text-xs text-muted">
          <UIcon name="i-lucide-file-output" class="size-3.5 shrink-0" aria-hidden="true" />
          Outputs
        </dt>
        <dd class="tabular-nums">{{ bytes(workspace.output_bytes) }}</dd>
      </div>
      <div v-if="workspace.last_activity_at" class="col-span-2 min-w-0 space-y-1 sm:col-span-3">
        <dt class="flex items-center gap-1.5 text-xs text-muted">
          <UIcon name="i-lucide-clock-3" class="size-3.5 shrink-0" aria-hidden="true" />
          Last filesystem activity
        </dt>
        <dd class="text-xs text-toned">
          <time :datetime="workspace.last_activity_at">{{
            formatDate(workspace.last_activity_at, 'detail')
          }}</time>
        </dd>
      </div>
    </dl>
    <div class="flex items-center justify-end gap-1.5 xl:col-start-3 xl:row-start-1 xl:self-start">
      <UCheckbox
        v-if="selectable"
        class="shrink-0"
        :model-value="selected"
        :disabled="busy || (!workspace.eligible && !selected)"
        :title="
          busy ? 'Wait for the current action.' : !workspace.eligible ? workspace.reason : undefined
        "
        :aria-label="`Select workspace ${workspace.workspace_id}`"
        @update:model-value="$emit('select')"
      />
      <UButton
        color="neutral"
        variant="ghost"
        size="sm"
        :trailing-icon="expanded ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
        :aria-label="expanded ? 'Hide details' : 'Show details'"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      />
    </div>
    <WorkspaceFileList
      v-if="expanded"
      class="xl:col-span-3"
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
