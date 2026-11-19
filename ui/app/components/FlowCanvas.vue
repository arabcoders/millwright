<template>
  <div class="h-full w-full min-h-0 min-w-0">
    <div
      class="relative h-full w-full min-h-0 min-w-0 overflow-hidden rounded-lg border border-default"
    >
      <div
        v-if="!nodesDraft.length"
        class="pointer-events-none absolute inset-x-0 top-4 z-10 text-center text-sm text-toned"
      >
        Steps you add will appear here.
      </div>
      <VueFlow
        :nodes="nodes"
        :edges="edges"
        fit-view-on-init
        @init="canvas = $event"
        @node-click="clickNode"
        @pane-click="emit('select', null)"
        :nodes-draggable="movable"
        :nodes-connectable="!readonly"
        :edges-updatable="!readonly"
        :nodes-focusable="false"
        :delete-key-code="null"
        @node-drag-stop="drag"
        @connect="connect"
        @edge-update="
          ({ edge, connection }) => !readonly && emit('updateEdge', edge.id, connection)
        "
        @edge-click="removeEdge"
        ><template #node-flow="{ data }"
          ><div
            class="relative min-w-64 rounded-md border px-3 py-2 text-sm shadow-sm backdrop-blur-md"
            :class="[
              data.border,
              data.added
                ? 'bg-success/10'
                : data.selected
                  ? 'bg-primary/10'
                  : 'bg-(--mw-floating-bg)',
              data.selected ? 'ring-2 ring-primary' : data.added ? 'ring-2 ring-success' : '',
            ]"
          >
            <div
              v-if="data.id !== 'start'"
              class="absolute text-[10px] text-toned"
              :class="horizontal ? '-left-9 top-1' : '-top-5 left-1'"
            >
              input
            </div>
            <Handle
              v-if="data.id !== 'start'"
              type="target"
              :position="horizontal ? Position.Left : Position.Top"
              aria-label="Input connector"
            />
            <UTooltip v-if="data.id !== 'start' && !readonly" text="Remove step">
              <UButton
                type="button"
                color="error"
                variant="soft"
                size="xs"
                icon="i-lucide-trash-2"
                square
                class="nodrag absolute -right-2 -top-2"
                aria-label="Remove step"
                @click.stop="emit('remove', data.id)"
              />
            </UTooltip>
            <div class="flex items-center justify-between gap-2">
              <button
                type="button"
                class="nodrag text-start font-medium hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                :class="{ 'text-primary': data.entry }"
                :aria-label="`${readonly ? 'Inspect' : 'Edit'} ${data.label}`"
                :aria-pressed="data.selected"
                @click.stop="emit('select', { kind: 'node', id: data.id })"
                @keydown="inspectKey($event, data.id)"
              >
                {{ data.label }}
              </button>
              <UBadge v-if="data.added" color="success" variant="subtle" size="xs">New</UBadge>
              <StatusBadge v-if="data.step" :status="data.step.outcome" group="outcome" />
            </div>
            <div class="max-w-56 wrap-break-word text-xs text-toned">{{ data.subtitle }}</div>
            <ReasonDisplay
              v-if="data.step?.reason"
              :reason="data.step.reason"
              class="mt-1 max-w-64"
            />
            <p
              v-if="data.step?.error"
              class="max-w-56 whitespace-pre-wrap wrap-anywhere text-xs text-error"
            >
              {{ data.step.error }}
            </p>
            <div
              v-if="data.ports.length"
              class="mt-2"
              :class="
                horizontal
                  ? 'flex flex-col items-end gap-2'
                  : data.ports.length === 1
                    ? 'flex justify-center'
                    : data.kind === 'condition'
                      ? 'flex justify-between'
                      : 'flex justify-end gap-3'
              "
            >
              <span
                v-for="port in data.ports"
                :key="port"
                class="relative text-[10px] text-toned"
                :class="horizontal ? 'pr-2' : 'pb-2'"
                ><Handle
                  type="source"
                  :position="horizontal ? Position.Right : Position.Bottom"
                  :id="port"
                  :aria-label="`${port === 'plan' ? statusInfo(port, 'outcome').label : port} output`"
                />{{ port === 'plan' ? statusInfo(port, 'outcome').label : port }}</span
              >
            </div>
          </div></template
        ></VueFlow
      >
      <slot name="overlay" />
    </div>
  </div>
</template>
<script setup lang="ts">
import ReasonDisplay from './ReasonDisplay.vue';
import { computed, shallowRef, watch } from 'vue';
import { Handle, MarkerType, Position, VueFlow } from '@vue-flow/core';
import type {
  Connection,
  Edge,
  Node,
  NodeDragEvent,
  NodeMouseEvent,
  VueFlowStore,
} from '@vue-flow/core';
import type { FlowEdge, FlowNodeResult, FlowUiNodePos, PluginRow } from '~/types/api';
import type { DraftNode, CanvasSelection } from '~/utils/stepDraft';
import { nodePorts } from '~/utils/stepDraft';
import { conditionSummary, type DraftCondition } from '~/utils/condition';
import { statusInfo } from '~/utils/status';
import StatusBadge from './StatusBadge.vue';
import '@vue-flow/core/dist/style.css';
const props = defineProps<{
  nodesDraft: DraftNode[];
  plugins: PluginRow[];
  selected: CanvasSelection | null;
  addedNodeIds?: string[];
  positions: Record<string, FlowUiNodePos>;
  entry: string | null;
  edges: FlowEdge[];
  readonly?: boolean;
  movable?: boolean;
  takenEdges?: string[];
  reached?: string[];
  steps?: FlowNodeResult[];
  entryCondition?: DraftCondition;
}>();
const emit = defineEmits<{
  select: [CanvasSelection | null];
  remove: [string];
  move: [string, FlowUiNodePos];
  connect: [Connection];
  updateEdge: [id: string, connection: Connection];
  removeEdge: [string];
}>();
const readonly = computed(() => props.readonly ?? false);
const canvas = shallowRef<VueFlowStore>();
const movable = computed(() => props.movable ?? !readonly.value);
const plugin = (id: string) => props.plugins.find((p) => p.manifest.id === id);
const horizontal = computed(() => {
  const start = props.positions.start;
  const entry = props.entry ? props.positions[props.entry] : undefined;
  return Boolean(start && entry && Math.abs(entry.x - start.x) > Math.abs(entry.y - start.y));
});
watch(horizontal, () => canvas.value?.updateNodeInternals(), { flush: 'post' });
const borderFor = (id: string) => {
  if (!props.steps) return 'border-default';
  const step = props.steps.find((step) => step.node === id);
  if (!step) return props.reached?.includes(id) ? 'border-info' : 'border-default opacity-45';
  return {
    success: 'border-success',
    error: 'border-error',
    warning: 'border-warning',
    info: 'border-info',
    neutral: 'border-neutral',
  }[statusInfo(step.outcome, 'outcome').color];
};
const nodes = computed<Node[]>(() => [
  {
    id: 'start',
    type: 'flow',
    deletable: false,
    position: { ...(props.positions.start ?? { x: 80, y: 40 }) },
    data: {
      id: 'start',
      label: 'Start',
      subtitle: !props.entry
        ? 'Connect to set entry'
        : props.entryCondition &&
            (props.entryCondition.rows.length || props.entryCondition.mode === 'custom')
          ? `Run only when ${conditionSummary(props.entryCondition)}`
          : 'Any media file',
      ports: ['next'],
      selected: props.selected?.id === 'start',
      border: props.steps && !props.steps.length ? 'border-default opacity-45' : 'border-default',
    },
  },
  ...props.nodesDraft.map((node, index) => {
    const data =
      node.kind === 'plugin'
        ? {
            label: (plugin(node.plugin)?.manifest.name ?? node.plugin) || 'Plugin',
            subtitle:
              plugin(node.plugin)?.manifest.kind ?? (readonly.value ? 'Plugin' : 'Choose a plugin'),
            ports: [
              ...new Set([
                ...nodePorts(node, plugin(node.plugin)),
                ...props.edges.filter((edge) => edge.src === node.uid).map((edge) => edge.port),
              ]),
            ],
          }
        : node.kind === 'condition'
          ? {
              label: 'Condition',
              subtitle: conditionSummary(node.condition),
              ports: ['true', 'false'],
            }
          : { label: 'Result', subtitle: node.outcome, ports: ['next'] };
    return {
      id: node.uid,
      type: 'flow',
      position: {
        ...(props.positions[node.uid] ?? {
          x: 80 + (index % 3) * 320,
          y: 220 + Math.floor(index / 3) * 180,
        }),
      },
      data: {
        ...data,
        id: node.uid,
        entry: node.uid === props.entry,
        selected: props.selected?.kind === 'node' && props.selected.id === node.uid,
        added: props.addedNodeIds?.includes(node.uid) ?? false,
        kind: node.kind,
        border: borderFor(node.uid),
        step: props.steps?.find((step) => step.node === node.uid),
      },
    };
  }),
]);
const entered = computed(
  () =>
    props.steps?.some((step) => step.node === props.entry && step.outcome !== 'deferred') ?? false,
);
const edges = computed<Edge[]>(() => [
  ...(props.entry
    ? [
        {
          id: `start->${props.entry}`,
          source: 'start',
          sourceHandle: 'next',
          target: props.entry,
          markerEnd: MarkerType.ArrowClosed,
          label: 'entry',
          labelStyle: { fill: 'var(--ui-text-toned)', fontSize: '11px' },
          labelBgStyle: { fill: 'var(--ui-bg-elevated)' },
          style: entered.value ? { stroke: 'var(--ui-info)', strokeWidth: 3 } : undefined,
          animated: entered.value,
        },
      ]
    : []),
  ...props.edges.map((e) => {
    const label = e.port === 'plan' ? statusInfo(e.port, 'outcome').label : e.port;
    const step = props.steps?.find((step) => step.node === e.src);
    const taken = props.takenEdges?.includes(`${e.src}:${e.port}:${e.dst}`) ?? false;
    const deferred = step?.outcome === 'deferred' && e.port === 'next';
    const info = step ? statusInfo(step.outcome, 'outcome') : undefined;
    const color =
      taken || deferred
        ? info?.color === 'neutral'
          ? 'var(--ui-text-dimmed)'
          : `var(--ui-${info?.color ?? 'info'})`
        : 'var(--ui-border-accented)';
    const feedback = taken
      ? info?.label
      : deferred
        ? 'Deferred'
        : props.steps
          ? step
            ? 'Not taken'
            : 'Not evaluated'
          : '';
    return {
      id: `${e.src}:${e.port}->${e.dst}`,
      source: e.src,
      sourceHandle: e.port,
      target: e.dst,
      markerEnd: { type: MarkerType.ArrowClosed, color },
      animated: taken,
      label: feedback ? `${label} · ${feedback}` : label,
      labelStyle: {
        fill: taken || deferred ? color : 'var(--ui-text-toned)',
        fontSize: '11px',
        fontWeight: taken ? 600 : 400,
      },
      labelBgStyle: { fill: 'var(--ui-bg-elevated)' },
      style: {
        stroke: color,
        strokeWidth: taken ? 3 : 1.5,
        strokeDasharray: deferred ? '6 4' : undefined,
      },
    };
  }),
]);
const clickNode = (e: NodeMouseEvent) => emit('select', { kind: 'node', id: e.node.id });
const inspectKey = (event: KeyboardEvent, id: string) => {
  if (event.target !== event.currentTarget) return;
  if (!readonly.value && id !== 'start' && ['Delete', 'Backspace'].includes(event.key)) {
    event.preventDefault();
    event.stopPropagation();
    emit('remove', id);
    return;
  }
  if (!['Enter', ' '].includes(event.key)) return;
  event.preventDefault();
  event.stopPropagation();
  emit('select', { kind: 'node', id });
};
const drag = (e: NodeDragEvent) =>
  movable.value &&
  emit('move', e.node.id, { x: Math.round(e.node.position.x), y: Math.round(e.node.position.y) });
const connect = (c: Connection) => !readonly.value && emit('connect', c);
const removeEdge = (e: { edge: Edge }) => !readonly.value && emit('removeEdge', e.edge.id);
</script>

<style scoped>
:deep(.vue-flow__handle) {
  width: 14px;
  height: 4px;
  min-width: 0;
  min-height: 0;
  border: 0;
  border-radius: 9999px;
  background: var(--ui-primary);
}

:deep(.vue-flow__handle::before) {
  /* Expand the click target without moving the measured edge anchor off the visible bar. */
  position: absolute;
  inset: -8px -4px;
  content: '';
}

:deep(.vue-flow__handle-left),
:deep(.vue-flow__handle-right) {
  width: 4px;
  height: 14px;
}

:deep(.vue-flow__handle-left::before),
:deep(.vue-flow__handle-right::before) {
  inset: -4px -8px;
}
</style>
