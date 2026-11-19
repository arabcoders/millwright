<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="nodes">
      <template #actions>
        <div class="flex flex-wrap items-center justify-end gap-3">
          <USwitch
            v-if="registration"
            :model-value="registration.enabled"
            label="Allow new nodes"
            size="sm"
            :loading="savingRegistration"
            :disabled="loading || savingRegistration"
            @update:model-value="changeRegistration"
          />
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-refresh-cw"
            aria-label="Refresh nodes"
            :loading="loading"
            @click="loadNodes"
          >
            Refresh
          </UButton>
        </div>
      </template>
    </PageHeader>

    <UAlert
      v-if="lastError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load nodes"
      :description="lastError.message"
    />
    <div v-if="loading && !allNodes.length" class="space-y-2">
      <USkeleton v-for="index in 4" :key="index" class="h-44 rounded-xl" />
    </div>
    <div v-else-if="allNodes.length" class="mw-card divide-y divide-default" :aria-busy="loading">
      <article
        v-for="node in allNodes"
        :key="'request_id' in node ? node.request_id : node.id"
        class="w-full min-w-0 max-w-full space-y-4 p-4 sm:p-5"
      >
        <div class="flex min-w-0 flex-wrap items-start justify-between gap-3">
          <div class="min-w-0 flex-1 space-y-2">
            <h2 class="min-w-0 text-base font-semibold text-highlighted">
              <button
                v-if="'id' in node"
                type="button"
                class="wrap-anywhere text-start hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                @click="selectNode(node)"
              >
                {{ node.display_name }}
              </button>
              <span v-else class="wrap-anywhere">
                {{ node.capabilities.hostname || 'Unnamed node' }}
              </span>
            </h2>
            <div class="flex flex-wrap items-center gap-2">
              <UBadge
                v-if="'request_id' in node"
                color="warning"
                variant="soft"
                size="sm"
                class="shrink-0 gap-1"
              >
                <UIcon name="i-lucide-user-round-plus" class="size-3.5 shrink-0" />
                Unclaimed
              </UBadge>
              <template v-else>
                <StatusBadge
                  class="shrink-0"
                  :status="
                    node.state === 'online' && node.active_jobs > 0
                      ? 'busy'
                      : node.state === 'online' && node.paused
                        ? 'paused'
                        : node.state
                  "
                  group="node"
                />
                <UBadge color="neutral" variant="subtle" size="sm" icon="i-lucide-repeat-2">
                  {{ node.active_jobs }} active
                </UBadge>
                <span class="inline-flex min-w-0 flex-wrap items-center gap-1 text-xs text-toned">
                  <span>Last seen:</span>
                  <UTooltip
                    v-if="node.last_seen_at"
                    :text="formatDate(node.last_seen_at, 'detail')"
                  >
                    <time :datetime="node.last_seen_at" class="wrap-anywhere">{{
                      formatRelativeTime(node.last_seen_at, 'en')
                    }}</time>
                  </UTooltip>
                  <span v-else>Never</span>
                </span>
              </template>
            </div>
          </div>
          <div class="ms-auto flex shrink-0 flex-wrap items-center justify-end gap-1.5">
            <UButton
              v-if="'request_id' in node"
              size="sm"
              icon="i-lucide-check"
              :loading="claimingId === node.request_id"
              :disabled="claimingId !== null || savingRegistration"
              :title="
                savingRegistration
                  ? 'Wait for registration settings to save.'
                  : claimingId !== null
                    ? 'Wait for the current claim to finish.'
                    : undefined
              "
              @click="claimPending(node.request_id)"
              >Claim</UButton
            >
            <template v-else>
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-pen-line"
                aria-label="Edit node"
                @click.stop="openEdit(node)"
                >Edit</UButton
              >
              <UDropdownMenu
                :items="nodeActions(node)"
                :modal="false"
                :content="{ align: 'end' }"
                :ui="{ content: 'w-72', itemDescription: 'whitespace-normal' }"
              >
                <UButton
                  color="neutral"
                  variant="ghost"
                  size="sm"
                  icon="i-lucide-ellipsis"
                  square
                  :aria-label="`Actions for ${node.display_name}`"
                  :loading="pausingId === node.id || removingId === node.id"
                  :disabled="pausingId !== null || removing"
                />
              </UDropdownMenu>
            </template>
          </div>
        </div>
        <p v-if="'request_id' in node" class="min-w-0 wrap-anywhere font-mono text-xs text-toned">
          Request: {{ node.request_id }}
        </p>
        <template v-else>
          <dl class="grid min-w-0 grid-cols-2 gap-x-6 gap-y-3 text-sm sm:max-w-2xl sm:grid-cols-3">
            <div class="min-w-0 space-y-1">
              <dt class="flex items-center gap-1.5 text-xs text-muted">
                <UIcon name="i-lucide-film" class="size-3.5 shrink-0" aria-hidden="true" />
                Transcodes
              </dt>
              <dd class="tabular-nums">{{ node.active_transcodes }}/{{ node.max_transcodes }}</dd>
            </div>
            <div class="min-w-0 space-y-1">
              <dt class="flex items-center gap-1.5 text-xs text-muted">
                <UIcon name="i-lucide-heart-pulse" class="size-3.5 shrink-0" aria-hidden="true" />
                Health checks
              </dt>
              <dd class="tabular-nums">
                {{ node.active_healthchecks }}/{{ node.max_healthchecks }}
              </dd>
            </div>
            <div class="min-w-0 space-y-1">
              <dt class="flex items-center gap-1.5 text-xs text-muted">
                <UIcon
                  name="i-lucide-chart-no-axes-column"
                  class="size-3.5 shrink-0"
                  aria-hidden="true"
                />
                MKV statistics
              </dt>
              <dd class="tabular-nums">{{ node.active_mkv_stats }}/{{ node.max_mkv_stats }}</dd>
            </div>
          </dl>
          <NodeUsageGauges
            v-if="node.usage"
            :usage="node.usage"
            :cpu-name="node.capabilities?.cpu_model"
            :cpu-cores="node.capabilities?.cpu_cores"
            :gpu-names="node.capabilities?.gpus"
            :offline="node.state !== 'online'"
          />
          <p v-else class="text-sm text-toned">No usage reported yet.</p>
        </template>
      </article>
    </div>

    <UEmpty
      v-else-if="!loading && !lastError"
      icon="i-lucide-server-off"
      title="No nodes connected."
      :description="
        registration?.enabled
          ? 'Start a node and claim it here.'
          : 'Enable Allow new nodes to register a node.'
      "
    />

    <NodeDetailsModal
      v-if="selectedNode"
      :selected-node="selectedNode"
      :ffmpeg-version="ffmpegVersion"
      :format-bytes="formatBytes"
      :format-relative-time="formatRelativeTime"
      @close="selectedNode = null"
    />
    <NodeEditModal
      :node="editNode"
      :saving="savingNode"
      :save-error="saveError"
      @close="editNode = null"
      @save="saveNode"
    />
  </main>
</template>

<script setup lang="ts">
import StatusBadge from '~/components/StatusBadge.vue';
import type { DropdownMenuItem } from '@nuxt/ui';
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import NodeDetailsModal from '~/components/NodeDetailsModal.vue';
import NodeEditModal from '~/components/NodeEditModal.vue';
import NodeUsageGauges from '~/components/NodeUsageGauges.vue';
import { formatBytes } from '~/utils';
import { formatDate } from '~/utils/date';
import { formatRelativeTime } from '~/utils/relativeTime';
import { useEvents } from '~/composables/useEvents';
import { useNodes } from '~/composables/useNodes';
import { useDialog } from '~/composables/useDialog';
import type { NodeChanges, NodeRow } from '~/types/api';

const {
  items: nodes,
  registration,
  isLoading: loading,
  lastError,
  ensure: ensureNodes,
  reload: reloadNodes,
  update,
  deleteNode,
  claimNode,
  allowRegistration,
  setRegistration,
  setStatus,
  setUsage,
} = useNodes();
const { confirmDialog } = useDialog();
const allNodes = computed(() => [...nodes.value, ...(registration.value?.pending ?? [])]);

const loadNodes = async (): Promise<void> => {
  await reloadNodes();
  if (selectedNode.value)
    selectedNode.value = nodes.value.find((node) => node.id === selectedNode.value?.id) ?? null;
  const id = Number(route.query.node);
  if (Number.isInteger(id)) selectNode(nodes.value.find((node) => node.id === id));
};

const { on } = useEvents();

onMounted(() => {
  void ensureNodes()
    .then(() => {
      const id = Number(route.query.node);
      if (Number.isInteger(id)) selectNode(nodes.value.find((node) => node.id === id));
    })
    .catch(() => {});
});

const route = useRoute();
watch(
  () => route.query.node,
  (value) => {
    const id = Number(value);
    if (Number.isInteger(id)) selectNode(nodes.value.find((node) => node.id === id));
  },
);

const stopEvents = on((event) => {
  if (event.type === 'node.registration') {
    setRegistration(event.payload);
    return;
  }
  if (event.type === 'node.usage') {
    setUsage(event.payload.name, event.payload.usage, event.payload.last_seen_at);
    return;
  }
  if (event.type !== 'node.status') {
    return;
  }
  if (
    event.payload.state === 'online' &&
    !nodes.value.some((node) => node.name === event.payload.name)
  ) {
    void reloadNodes();
    return;
  }
  setStatus(
    event.payload.name,
    event.payload.state,
    event.payload.active_jobs,
    event.payload.active_job_ids,
    event.payload.active_transcodes,
    event.payload.active_healthchecks,
    event.payload.active_mkv_stats,
  );
});

onBeforeUnmount(() => {
  stopEvents();
});

const selectedNode = ref<NodeRow | null>(null);
const editNode = ref<NodeRow | null>(null);
const removingId = ref<number | null>(null);
const removing = computed(() => removingId.value !== null);
const savingRegistration = ref(false);
const claimingId = ref<string | null>(null);

const changeRegistration = async (enabled: boolean): Promise<void> => {
  if (savingRegistration.value) return;
  savingRegistration.value = true;
  try {
    await allowRegistration(enabled);
  } catch {
  } finally {
    savingRegistration.value = false;
  }
};

const claimPending = async (requestId: string): Promise<void> => {
  if (claimingId.value !== null) return;
  claimingId.value = requestId;
  try {
    await claimNode(requestId);
  } catch {
  } finally {
    claimingId.value = null;
  }
};

const selectNode = (node: NodeRow | undefined): void => {
  selectedNode.value = node ?? null;
};

const openEdit = (node: NodeRow): void => {
  saveError.value = '';
  editNode.value = node;
};

const openRemoveConfirm = async (node: NodeRow): Promise<void> => {
  if (removing.value || node.active_jobs > 0) return;
  const result = await confirmDialog({
    title: 'Remove node?',
    message: `Removing the node will keep its configuration, files, logs, and job history.`,
    confirmText: 'Remove node',
    confirmColor: 'error',
  });
  if (!result.status || removing.value) return;
  removingId.value = node.id;
  try {
    await deleteNode(node.id);
    if (selectedNode.value?.id === node.id) selectedNode.value = null;
    if (editNode.value?.id === node.id) editNode.value = null;
  } catch {
  } finally {
    removingId.value = null;
  }
};

const savingNode = ref(false);
const saveError = ref('');
const pausingId = ref<number | null>(null);

const pauseNode = async (node: NodeRow): Promise<void> => {
  if (pausingId.value !== null) return;
  pausingId.value = node.id;
  try {
    await update(node.id, { paused: !node.paused });
  } catch {
  } finally {
    pausingId.value = null;
  }
};

const saveNode = async (changes: NodeChanges): Promise<void> => {
  const node = editNode.value;
  if (!node || savingNode.value) return;
  savingNode.value = true;
  saveError.value = '';
  try {
    const updated = await update(node.id, changes);
    if (selectedNode.value?.id === updated.id) selectedNode.value = updated;
    editNode.value = null;
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : 'Could not save the node.';
  } finally {
    savingNode.value = false;
  }
};

const nodeActions = (node: NodeRow): DropdownMenuItem[][] => [
  [
    {
      label: node.paused ? 'Resume' : 'Pause',
      icon: node.paused ? 'i-lucide-play' : 'i-lucide-pause',
      disabled: pausingId.value !== null || removing.value,
      onSelect: () => pauseNode(node),
    },
  ],
  [
    {
      label: 'Remove node',
      icon: 'i-lucide-trash-2',
      color: 'error',
      description: node.active_jobs
        ? 'Wait for active work to finish before removing this node.'
        : undefined,
      disabled: removing.value || pausingId.value !== null || node.active_jobs > 0,
      onSelect: () => openRemoveConfirm(node),
    },
  ],
];

const ffmpegVersion = (node: NodeRow): string =>
  (node.capabilities?.ffmpeg_version ?? '').split(' ').slice(0, 3).join(' ');
</script>
