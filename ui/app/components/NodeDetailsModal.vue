<template>
  <UModal
    :open="Boolean(props.selectedNode)"
    :title="props.selectedNode?.display_name ?? 'Node details'"
    :ui="{
      content: 'w-full max-w-5xl',
      title: 'wrap-anywhere',
      body: 'max-h-[75vh] overflow-y-auto',
    }"
    @update:open="$emit('close')"
  >
    <template #body>
      <div v-if="selectedNode" class="min-w-0 space-y-5 wrap-anywhere">
        <div class="grid min-w-0 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <StatCard label="Status" :value="selectedNode.state" icon="i-lucide-activity" value-wrap>
            <template #value>
              <UBadge
                :color="selectedNode.state === 'online' ? 'success' : 'neutral'"
                variant="soft"
                size="sm"
                >{{ selectedNode.state }}</UBadge
              >
            </template>
          </StatCard>
          <StatCard
            label="New jobs"
            :value="selectedNode.paused ? 'Paused' : 'Active'"
            icon="i-lucide-pause"
            value-wrap
          >
            <template #value>
              <UBadge
                :color="selectedNode.paused ? 'warning' : 'success'"
                variant="soft"
                size="sm"
                >{{ selectedNode.paused ? 'Paused' : 'Active' }}</UBadge
              >
            </template>
          </StatCard>
          <StatCard
            label="Last seen"
            :value="
              selectedNode.last_seen_at
                ? formatRelativeTime(selectedNode.last_seen_at, 'en')
                : 'Never'
            "
            icon="i-lucide-clock"
            :tooltip="
              selectedNode.last_seen_at
                ? formatDate(selectedNode.last_seen_at, 'detail')
                : undefined
            "
            value-wrap
          />
          <StatCard
            label="Host"
            :value="selectedNode.capabilities?.hostname || 'Unknown'"
            icon="i-lucide-cpu"
            value-wrap
          />
          <StatCard
            label="Memory"
            :value="
              selectedNode.capabilities?.memory_gb == null
                ? 'Unknown'
                : `${selectedNode.capabilities.memory_gb.toFixed(1)} GB`
            "
            icon="i-lucide-memory-stick"
            value-wrap
          />
          <StatCard
            label="FFmpeg"
            :value="ffmpegVersion(selectedNode) || 'Unknown'"
            icon="i-lucide-film"
            value-wrap
          />
          <StatCard
            label="MKV statistics tool"
            :value="
              selectedNode.capabilities?.mkvpropedit === undefined
                ? 'Unknown'
                : selectedNode.capabilities.mkvpropedit
                  ? 'Available'
                  : 'Unavailable'
            "
            icon="i-lucide-wrench"
            value-wrap
          />
          <StatCard
            label="Transcode slots"
            :value="`${selectedNode.active_transcodes}/${selectedNode.max_transcodes}`"
            icon="i-lucide-repeat-2"
            value-wrap
            class="tabular-nums"
          />
          <StatCard
            label="Health check slots"
            :value="`${selectedNode.active_healthchecks}/${selectedNode.max_healthchecks}`"
            icon="i-lucide-heart-pulse"
            value-wrap
            class="tabular-nums"
          />
          <StatCard
            label="MKV statistics slots"
            :value="`${selectedNode.active_mkv_stats}/${selectedNode.max_mkv_stats}`"
            icon="i-lucide-chart-no-axes-combined"
            value-wrap
            class="tabular-nums"
          />
          <StatCard
            label="Worker tags"
            :value="workerTagSummary(selectedNode)"
            icon="i-lucide-tags"
            value-wrap
          />
          <StatCard
            label="Workspace"
            :value="selectedNode.workspace_ready ? 'Ready' : 'Unavailable'"
            icon="i-lucide-folder"
            value-wrap
          >
            <template #value>
              <UBadge
                :color="selectedNode.workspace_ready ? 'success' : 'error'"
                variant="soft"
                size="sm"
                >{{ selectedNode.workspace_ready ? 'Ready' : 'Unavailable' }}</UBadge
              >
            </template>
          </StatCard>
          <StatCard
            label="Workspace free"
            :value="
              selectedNode.workspace_free_bytes == null
                ? 'Unknown'
                : formatBytes(selectedNode.workspace_free_bytes)
            "
            icon="i-lucide-hard-drive"
            value-wrap
          />
        </div>

        <section class="space-y-3 border-t border-default pt-5">
          <div class="flex items-center gap-2">
            <UIcon name="i-lucide-gauge" class="size-5 text-toned" aria-hidden="true" />
            <p class="text-base font-semibold text-highlighted">Usage</p>
          </div>
          <NodeUsageGauges
            v-if="selectedNode.usage"
            :usage="selectedNode.usage"
            :cpu-name="selectedNode.capabilities?.cpu_model"
            :cpu-cores="selectedNode.capabilities?.cpu_cores"
            :gpu-names="selectedNode.capabilities?.gpus"
            :offline="selectedNode.state !== 'online'"
          />
          <p v-else class="text-sm text-toned">No usage reported yet.</p>
        </section>

        <section
          v-if="selectedNode.active_job_ids.length"
          class="space-y-3 border-t border-default pt-5"
        >
          <div class="flex items-center gap-2">
            <UIcon name="i-lucide-list-checks" class="size-5 text-toned" aria-hidden="true" />
            <p class="text-base font-semibold text-highlighted">Active jobs</p>
          </div>
          <div class="flex flex-wrap gap-2">
            <UButton
              v-for="jobId in selectedNode.active_job_ids"
              :key="jobId"
              :to="{ path: '/jobs', query: { job: String(jobId) } }"
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-external-link"
            >
              {{ jobNumber(jobId) }}
            </UButton>
          </div>
        </section>
        <section class="space-y-3 border-t border-default pt-5">
          <button
            type="button"
            class="flex w-full flex-wrap items-center justify-between gap-3 text-start"
            :aria-label="capabilitiesOpen ? 'Hide hardware and codecs' : 'Show hardware and codecs'"
            :aria-expanded="capabilitiesOpen"
            @click="capabilitiesOpen = !capabilitiesOpen"
          >
            <div class="flex items-center gap-2">
              <UIcon name="i-lucide-cpu" class="size-5 text-toned" aria-hidden="true" />
              <p class="text-base font-semibold text-highlighted">Hardware and codecs</p>
            </div>
            <div class="flex items-center gap-2">
              <UBadge color="neutral" variant="subtle" size="sm">
                {{
                  capabilityTotal === null ? 'Not fully reported' : `${capabilityTotal} reported`
                }}
              </UBadge>
              <UIcon
                :name="capabilitiesOpen ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
                class="size-4 text-toned"
              />
            </div>
          </button>
          <div v-if="capabilitiesOpen" class="divide-y divide-default">
            <UInput
              v-model="capabilityFilter"
              type="search"
              icon="i-lucide-filter"
              placeholder="Filter"
              size="sm"
              class="mb-3 w-full"
            />
            <UEmpty
              v-if="capabilityFilter && !visibleCapabilityFields.length"
              size="sm"
              variant="naked"
              icon="i-lucide-filter"
              title="No matches"
            />
            <div v-for="field in visibleCapabilityFields" :key="field.key" class="py-3">
              <button
                type="button"
                class="flex w-full items-center justify-between gap-3 text-start"
                :aria-label="`${capabilityFieldOpen(field.key) ? 'Hide' : 'Show'} ${field.label}`"
                :aria-expanded="capabilityFieldOpen(field.key)"
                @click="toggleCapability(field.key)"
              >
                <span class="min-w-0 wrap-anywhere text-sm font-medium text-highlighted">{{
                  field.label
                }}</span>
                <div class="flex min-w-0 items-center justify-end gap-2">
                  <UBadge color="neutral" variant="subtle" size="sm">
                    {{
                      field.items === undefined ? 'Not reported' : `${field.items.length} reported`
                    }}
                  </UBadge>
                  <UIcon
                    :name="
                      capabilityFieldOpen(field.key)
                        ? 'i-lucide-chevron-up'
                        : 'i-lucide-chevron-down'
                    "
                    class="size-4 shrink-0 text-toned"
                  />
                </div>
              </button>
              <div v-if="capabilityFieldOpen(field.key)" class="pt-3">
                <p v-if="field.items === undefined" class="text-sm text-toned">Not reported.</p>
                <UInput
                  v-else-if="field.items.length"
                  v-model="capabilityFilters[field.key]"
                  type="search"
                  icon="i-lucide-filter"
                  placeholder="Filter values"
                  size="sm"
                  class="mb-3 w-full"
                />
                <UEmpty
                  v-if="field.items !== undefined && !filteredCapabilityItems(field).length"
                  size="sm"
                  variant="naked"
                  icon="i-lucide-filter"
                  :title="capabilityFilters[field.key] ? 'No matches' : 'None reported'"
                />
                <div class="max-h-40 overflow-y-auto">
                  <div class="flex flex-wrap gap-1.5">
                    <UBadge
                      v-for="item in filteredCapabilityItems(field)"
                      :key="item"
                      color="neutral"
                      variant="subtle"
                      class="max-w-full wrap-anywhere whitespace-normal"
                      >{{ item }}</UBadge
                    >
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </template>
  </UModal>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue';
import { useStorage } from '@vueuse/core';
import type { NodeRow } from '~/types/api';
import NodeUsageGauges from './NodeUsageGauges.vue';
import { jobNumber } from '~/utils';
import { formatDate } from '~/utils/date';

const props = defineProps<{
  selectedNode: NodeRow | null;
  ffmpegVersion: (node: NodeRow) => string | undefined;
  formatBytes: (value: number) => string;
  formatRelativeTime: (value: string, locale: string) => string;
}>();

defineEmits<{ close: [] }>();

const capabilitiesOpen = useStorage('node_capabilities_open', true);
const capabilityFilter = ref('');
const capabilityFilters = ref<Record<string, string>>({});
const openCapabilityFields = ref<Record<string, boolean>>({});
const workerTagSummary = (node: NodeRow): string => {
  if (node.capabilities?.worker_tags === undefined) return 'Unknown';
  const tags = (node.capabilities?.worker_tags ?? [])
    .map((tag) => tag.toUpperCase())
    .filter((tag): tag is 'CPU' | 'GPU' => tag === 'CPU' || tag === 'GPU');
  return [...new Set(tags)].join(' · ') || 'None';
};
type CapabilityField = { key: string; label: string; items: string[] | undefined };
const capabilityFields = computed<CapabilityField[]>(() => {
  const caps = props.selectedNode?.capabilities;
  return [
    { key: 'gpus', label: 'GPUs', items: caps?.gpus },
    { key: 'encoders', label: 'Encoders', items: caps?.encoders },
    { key: 'decoders', label: 'Decoders', items: caps?.decoders },
    { key: 'hwaccels', label: 'Hardware acceleration', items: caps?.hwaccels },
  ];
});
const capabilityTotal = computed(() =>
  capabilityFields.value.some((field) => field.items === undefined)
    ? null
    : capabilityFields.value.reduce((total, field) => total + (field.items?.length ?? 0), 0),
);
const visibleCapabilityFields = computed(() =>
  capabilityFields.value.filter(
    (field) =>
      !capabilityFilter.value ||
      field.label.toLowerCase().includes(capabilityFilter.value.toLowerCase()),
  ),
);
const toggleCapability = (key: string): void => {
  openCapabilityFields.value[key] = !openCapabilityFields.value[key];
};
const capabilityFieldOpen = (key: string): boolean => Boolean(openCapabilityFields.value[key]);
const filteredCapabilityItems = (field: CapabilityField): string[] => {
  const query = capabilityFilters.value[field.key]?.toLowerCase() ?? '';
  return (field.items ?? []).filter((item) => !query || item.toLowerCase().includes(query));
};
</script>
