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
            aria-label="Reload nodes"
            :loading="loading"
            @click="loadNodes"
          >
            Reload
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
      <USkeleton v-for="index in 4" :key="index" class="h-20 rounded-xl" />
    </div>
    <div v-else-if="allNodes.length" class="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
      <article
        v-for="node in allNodes"
        :key="'request_id' in node ? node.request_id : node.id"
        class="mw-card flex h-full flex-col gap-3 p-4"
        :class="{ 'border-warning': 'request_id' in node }"
      >
        <div class="flex min-w-0 items-center gap-2">
          <div class="min-w-0">
            <button
              v-if="'id' in node"
              type="button"
              class="truncate text-start font-semibold text-highlighted hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
              @click="selectNode(node)"
            >
              {{ node.display_name }}
            </button>
            <h2 v-else class="truncate font-semibold text-highlighted">
              {{ node.capabilities.hostname || 'Unnamed node' }}
            </h2>
          </div>
          <UBadge
            v-if="'request_id' in node"
            color="warning"
            variant="soft"
            size="sm"
            class="ms-auto shrink-0 gap-1"
          >
            <UIcon name="i-lucide-user-round-plus" class="size-3.5 shrink-0" />
            Unclaimed
          </UBadge>
          <template v-else>
            <span
              class="ms-auto flex shrink-0 items-center gap-1 rounded-md bg-elevated px-2 py-0.5 text-xs text-toned"
            >
              <UIcon name="i-lucide-repeat-2" class="size-3" />
              {{ node.active_jobs }} active
            </span>
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
          </template>
        </div>

        <dl v-if="'id' in node" class="grid grid-cols-3 gap-2 text-xs text-toned">
          <div>
            <dt>Transcodes</dt>
            <dd>{{ node.active_transcodes }}/{{ node.max_transcodes }}</dd>
          </div>
          <div>
            <dt>Health checks</dt>
            <dd>{{ node.active_healthchecks }}/{{ node.max_healthchecks }}</dd>
          </div>
          <div>
            <dt>MKV statistics</dt>
            <dd>{{ node.active_mkv_stats }}/{{ node.max_mkv_stats }}</dd>
          </div>
        </dl>
        <NodeUsageGauges
          :usage="'id' in node ? node.usage : null"
          :cpu-name="node.capabilities?.cpu_model"
          :cpu-cores="node.capabilities?.cpu_cores"
          :gpu-names="node.capabilities?.gpus"
          :offline="'id' in node && node.state !== 'online'"
        />
        <div class="mt-auto flex flex-wrap items-center justify-between gap-2">
          <template v-if="'request_id' in node">
            <p class="min-w-0 truncate font-mono text-xs text-toned">
              Request: {{ node.request_id }}
            </p>
            <UButton
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
          </template>
          <template v-else>
            <div class="flex min-w-0 items-center gap-1 text-xs text-toned">
              <span class="shrink-0">Last seen:</span>
              <UTooltip v-if="node.last_seen_at" :text="formatDate(node.last_seen_at, 'detail')">
                <time :datetime="node.last_seen_at" class="truncate">{{
                  formatRelativeTime(node.last_seen_at, 'en')
                }}</time>
              </UTooltip>
              <span v-else>never</span>
            </div>
            <div class="flex flex-wrap items-center justify-end gap-2">
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-pen-line"
                aria-label="Edit node"
                @click.stop="openEdit(node)"
                >Edit</UButton
              >
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                :icon="node.paused ? 'i-lucide-play' : 'i-lucide-pause'"
                :loading="pausingId === node.id"
                :disabled="pausingId !== null"
                @click.stop="pauseNode(node)"
              >
                {{ node.paused ? 'Resume' : 'Pause' }}
              </UButton>
              <UTooltip
                :disabled="!node.active_jobs && !removing"
                :text="
                  node.active_jobs
                    ? 'Wait for active work to finish before removing this node.'
                    : 'Wait for the current node removal to finish.'
                "
              >
                <span class="inline-flex">
                  <UButton
                    color="neutral"
                    variant="outline"
                    size="sm"
                    icon="i-lucide-trash-2"
                    :loading="removingId === node.id"
                    :disabled="removing || node.active_jobs > 0"
                    @click.stop="openRemoveConfirm(node)"
                  >
                    Remove node
                  </UButton>
                </span>
              </UTooltip>
            </div>
          </template>
        </div>
      </article>
    </div>

    <div
      v-else-if="!loading && !lastError"
      class="mw-card flex flex-col items-center gap-1 py-12 text-center"
    >
      <UIcon name="i-lucide-server-off" class="size-6 text-toned" />
      <p class="text-sm text-toned">
        {{
          registration?.enabled
            ? 'No nodes connected. Start a node and claim it here.'
            : 'No nodes connected. Enable Allow new nodes to register a node.'
        }}
      </p>
    </div>

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
      @close="editNode = null"
      @save="saveNode"
    />
  </main>
</template>

<script setup lang="ts">
import StatusBadge from '~/components/StatusBadge.vue';
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
  try {
    const updated = await update(node.id, changes);
    if (selectedNode.value?.id === updated.id) selectedNode.value = updated;
    editNode.value = null;
  } catch {
  } finally {
    savingNode.value = false;
  }
};

const ffmpegVersion = (node: NodeRow): string =>
  (node.capabilities?.ffmpeg_version ?? '').split(' ').slice(0, 3).join(' ');
</script>
