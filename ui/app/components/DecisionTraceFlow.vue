<template>
  <div
    class="w-full min-w-0"
    :class="fullscreen ? 'flex h-full min-h-0 flex-col gap-4' : 'space-y-4'"
  >
    <div
      class="w-full min-w-0"
      :class="fullscreen ? 'min-h-200 flex-1 lg:min-h-136' : 'h-200 lg:h-136'"
    >
      <USplitter
        :key="desktop ? 'columns' : 'rows'"
        :items="panels"
        :orientation="desktop ? 'horizontal' : 'vertical'"
        :auto-save-id="desktop ? 'flow-viewer-columns' : 'flow-viewer-rows'"
        class="h-full w-full"
        :ui="{ handle: 'bg-border data-[state=hover]:bg-primary data-[state=drag]:bg-primary' }"
      >
        <template #canvas>
          <FlowCanvas
            :key="fullscreen ? 'full' : 'windowed'"
            :nodes-draft="draftNodes"
            :plugins="plugins"
            :selected="selectedId ? { kind: 'node', id: selectedId } : null"
            :positions="definition._ui?.nodes ?? {}"
            :entry="definition.entry"
            :entry-condition="gate"
            :edges="definition.edges"
            readonly
            :movable="movable && nodesMovable"
            :taken-edges="[...path.taken]"
            :reached="[...path.reached]"
            :steps="steps"
            @select="selectedId = $event?.id ?? null"
            @move="moveNode"
          >
            <template #overlay>
              <div
                v-if="trace.length || movable"
                class="pointer-events-none absolute bottom-14 left-3 top-3 z-10 flex w-80 max-w-[calc(100%-1.5rem)] flex-col items-start gap-2"
              >
                <div class="flex flex-wrap gap-2">
                  <UButton
                    v-if="trace.length"
                    type="button"
                    color="neutral"
                    variant="outline"
                    size="xs"
                    icon="i-lucide-list-ordered"
                    :trailing-icon="stepsOpen ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
                    :aria-expanded="stepsOpen"
                    class="pointer-events-auto shrink-0 bg-elevated shadow-sm"
                    @click="stepsOpen = !stepsOpen"
                    >Toggle steps</UButton
                  >
                  <UButton
                    v-if="movable"
                    type="button"
                    :color="nodesMovable ? 'primary' : 'neutral'"
                    variant="outline"
                    size="xs"
                    icon="i-lucide-move"
                    :aria-pressed="nodesMovable"
                    class="pointer-events-auto shrink-0 bg-elevated shadow-sm"
                    @click="nodesMovable = !nodesMovable"
                    >Move nodes</UButton
                  >
                </div>
                <ol
                  v-if="trace.length && stepsOpen"
                  aria-label="Steps"
                  tabindex="0"
                  class="pointer-events-auto w-full min-h-0 space-y-3 overflow-y-auto overscroll-contain rounded-lg border border-default bg-elevated p-3 shadow-lg"
                >
                  <li
                    v-for="(step, index) in trace"
                    :key="index"
                    class="flex items-start gap-2 text-sm"
                  >
                    <span class="font-mono text-toned">{{ index + 1 }}.</span>
                    <div class="min-w-0 flex-1 wrap-break-word">
                      <StatusBadge :status="step.outcome" group="outcome" /><span
                        class="ml-2 font-medium text-default"
                        >{{ step.title }}</span
                      >
                      <ReasonDisplay :reason="step.detail" class="mt-1" />
                      <p v-if="step.error" class="text-error">{{ step.error }}</p>
                      <p v-if="step.destination" class="text-xs text-toned">
                        {{ step.outcome === 'deferred' ? 'Continues after verification' : 'Next' }}:
                        {{ step.destination }}
                      </p>
                    </div>
                  </li>
                </ol>
              </div>
              <slot name="overlay" />
            </template>
          </FlowCanvas>
        </template>
        <template #settings>
          <div class="h-full w-full min-h-0 min-w-0 overflow-y-auto pl-3">
            <div class="flex min-h-full w-full min-w-0 flex-col gap-4">
              <StepInspector
                class="flex-1"
                :node="selectedNode"
                :entry-condition="gate"
                :entry-selected="selectedId === 'start'"
                :plugins="plugins"
                :codec-options="emptyCodecOptions"
                :facet-options="[]"
                :facts="[]"
                :notification-targets="[]"
                notification-target-error=""
                :stored-when="
                  selectedId === 'start'
                    ? (definition.when ?? undefined)
                    : selectedDefinition?.type === 'condition'
                      ? selectedDefinition.when
                      : undefined
                "
                readonly
              />
              <section
                v-if="selectedStep"
                class="space-y-2 rounded-lg border border-default p-4 text-sm"
              >
                <h3 class="font-semibold text-highlighted">Decision</h3>
                <StatusBadge :status="selectedStep.outcome" group="outcome" />
                <ReasonDisplay v-if="selectedStep.reason" :reason="selectedStep.reason" />
                <p v-if="selectedStep.error" class="text-error">{{ selectedStep.error }}</p>
                <p v-if="selectedStep.outcome === 'deferred'" class="text-toned">
                  Waits until the output is verified.
                </p>
                <template v-if="selectedStep.facets && Object.keys(selectedStep.facets).length">
                  <UCollapsible :key="selectedId ?? ''">
                    <template #default="{ open }">
                      <UButton
                        color="neutral"
                        variant="ghost"
                        size="xs"
                        class="flex w-full justify-end"
                        :trailing-icon="open ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
                        >Facets (JSON)</UButton
                      >
                    </template>
                    <template #content>
                      <pre class="mt-2 text-xs whitespace-pre-wrap wrap-break-word">{{
                        JSON.stringify(selectedStep.facets, null, 2)
                      }}</pre>
                    </template>
                  </UCollapsible>
                </template>
              </section>
              <p
                v-if="!selectedId"
                class="flex-1 rounded-lg border border-default p-4 text-sm text-toned"
              >
                Select Start or a step to see its settings.
              </p>
            </div>
          </div>
        </template>
      </USplitter>
    </div>
    <div
      v-if="definition.actions?.length"
      class="space-y-2 rounded-lg border border-default p-3 text-sm"
    >
      <div>
        <UCollapsible>
          <template #default="{ open }">
            <UButton
              color="neutral"
              variant="ghost"
              size="xs"
              class="flex w-full justify-end"
              :trailing-icon="open ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
              >Flow actions (JSON)</UButton
            >
          </template>
          <template #content>
            <pre class="mt-2 text-xs whitespace-pre-wrap wrap-break-word">{{
              JSON.stringify(definition.actions, null, 2)
            }}</pre>
          </template>
        </UCollapsible>
      </div>
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue';
import { useMediaQuery, useStorage } from '@vueuse/core';
import type { SplitterItem } from '@nuxt/ui';
import type {
  CodecOptions,
  FlowDefinition,
  FlowNodeResult,
  PluginRow,
  FlowUiNodePos,
} from '~/types/api';
import type { DraftNode } from '~/utils/stepDraft';
import { conditionFromWhen, conditionSummary } from '~/utils/condition';
import { tracePath, tracePort } from '~/utils/flowTrace';
import ReasonDisplay from './ReasonDisplay.vue';
import FlowCanvas from './FlowCanvas.vue';
import StepInspector from './StepInspector.vue';
import StatusBadge from './StatusBadge.vue';

const props = withDefaults(
  defineProps<{
    definition: FlowDefinition;
    steps?: FlowNodeResult[];
    plugins?: PluginRow[];
    movable?: boolean;
    fullscreen?: boolean;
  }>(),
  { steps: undefined, plugins: () => [], movable: false, fullscreen: false },
);
const emit = defineEmits<{ move: [string, FlowUiNodePos] }>();
const selectedId = ref<string | null>(null);
const stepsOpen = useStorage('flow_viewer_steps_open', false);
const nodesMovable = useStorage('flow_viewer_move_nodes', false);
const gate = computed(() => conditionFromWhen(props.definition.when));
const emptyCodecOptions: CodecOptions = { videoEncoders: [], audioEncoders: [], audioDecoders: [] };
const desktop = useMediaQuery('(min-width: 1024px)');
const wide = useMediaQuery('(min-width: 1536px)');
const panels = computed<SplitterItem[]>(() => [
  {
    slot: 'canvas',
    minSize: desktop.value ? 20 : 69,
    defaultSize: wide.value ? 80 : 70,
    class: 'min-h-0 min-w-0',
  },
  { slot: 'settings', minSize: 15, defaultSize: wide.value ? 20 : 30, class: 'min-h-0 min-w-0' },
]);
const draftNodes = computed<DraftNode[]>(() =>
  props.definition.nodes.map((node) => {
    if (node.type === 'plugin')
      return {
        uid: node.id,
        kind: 'plugin',
        plugin: node.plugin,
        configVersion: node.config_version ?? 1,
        inputs: {},
        rawInputs: JSON.parse(JSON.stringify(node.inputs ?? {})),
        initialInputs: {},
      };
    if (node.type === 'condition')
      return { uid: node.id, kind: 'condition', condition: conditionFromWhen(node.when) };
    return { uid: node.id, kind: 'terminal', outcome: node.outcome };
  }),
);
const selectedNode = computed(
  () => draftNodes.value.find((node) => node.uid === selectedId.value) ?? null,
);
const selectedDefinition = computed(() =>
  props.definition.nodes.find((node) => node.id === selectedId.value),
);
const selectedStep = computed(() => props.steps?.find((step) => step.node === selectedId.value));
const moveNode = (id: string, position: FlowUiNodePos) => emit('move', id, position);
const path = computed(() => tracePath(props.definition, props.steps ?? []));
const nodeLabel = (id: string): string => {
  const node = props.definition.nodes.find((candidate) => candidate.id === id);
  if (!node) return id;
  if (node.type === 'condition') return conditionSummary(conditionFromWhen(node.when));
  if (node.type === 'plugin')
    return (
      props.plugins.find((plugin) => plugin.manifest.id === node.plugin)?.manifest.name ??
      node.plugin
    );
  return `Terminal (${node.outcome})`;
};
const trace = computed(() =>
  (props.steps ?? []).map((entry) => {
    const node = props.definition.nodes.find((candidate) => candidate.id === entry.node);
    const port = entry.outcome === 'deferred' ? 'next' : tracePort(node, entry);
    const destination = props.definition.edges.find(
      (edge) => edge.src === entry.node && edge.port === port,
    )?.dst;
    if (!node)
      return {
        outcome: entry.outcome,
        title: entry.step || entry.node,
        detail: entry.reason ?? '',
        error: entry.error,
        destination: destination ? nodeLabel(destination) : undefined,
      };
    if (node.type === 'condition')
      return {
        outcome: entry.outcome,
        title: nodeLabel(entry.node),
        detail:
          entry.reason ??
          (entry.outcome === 'true'
            ? 'Matched.'
            : entry.outcome === 'false'
              ? 'Did not match.'
              : ''),
        error: entry.error,
        destination: destination ? nodeLabel(destination) : undefined,
      };
    if (node.type === 'plugin')
      return {
        outcome: entry.outcome,
        title: nodeLabel(entry.node),
        detail:
          entry.reason ??
          (entry.outcome === 'deferred'
            ? 'Waits until the output is verified.'
            : ({ plan: 'Conversion requested.', skip: 'Skipped.' }[entry.outcome] ??
              `Returned ${entry.outcome}.`)),
        error: entry.error,
        destination: destination ? nodeLabel(destination) : undefined,
      };
    return {
      outcome: entry.outcome,
      error: entry.error,
      title: 'Terminal',
      detail:
        entry.reason ??
        (destination
          ? `Final outcome: ${entry.outcome}. More steps follow.`
          : `Final outcome: ${entry.outcome}.`),
      destination: destination ? nodeLabel(destination) : undefined,
    };
  }),
);
</script>
