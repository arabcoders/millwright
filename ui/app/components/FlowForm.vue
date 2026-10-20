<template>
  <form
    id="flowForm"
    :class="fullscreen ? 'flex h-full min-h-0 flex-col gap-5' : 'space-y-5'"
    @submit.prevent="submit"
  >
    <UFormField label="Flow name">
      <UFieldGroup class="w-full">
        <UInput v-model="name" class="min-w-0 flex-1" :ui="{ root: 'w-full' }" />
        <UDropdownMenu :items="addItems" aria-label="Add node">
          <UButton
            color="neutral"
            variant="outline"
            icon="i-lucide-plus"
            trailing-icon="i-lucide-chevron-down"
            aria-label="Add node"
            :disabled="disabled"
            >Add node</UButton
          >
        </UDropdownMenu>
      </UFieldGroup>
    </UFormField>
    <div :class="fullscreen ? 'min-h-200 flex-1 lg:min-h-136' : 'h-200 lg:h-136'">
      <USplitter
        id="flow-editor-splitter"
        :key="desktop ? 'columns' : 'rows'"
        :items="panels"
        :orientation="desktop ? 'horizontal' : 'vertical'"
        :auto-save-id="desktop ? 'flow-editor-columns' : 'flow-editor-rows'"
        class="h-full w-full"
        :ui="{ handle: 'bg-border data-[state=hover]:bg-primary data-[state=drag]:bg-primary' }"
      >
        <template #canvas>
          <FlowCanvas
            :key="fullscreen ? 'full' : 'windowed'"
            :nodes-draft="nodes"
            :plugins="plugins"
            :selected="selected"
            :added-node-ids="addedNodeIds"
            :positions="positions"
            :entry="entry"
            :entry-condition="when"
            :edges="edges"
            :readonly="disabled"
            :movable="!disabled"
            @select="selected = $event"
            @remove="removeNode"
            @move="move"
            @connect="connect"
            @remove-edge="removeEdge"
          />
        </template>
        <template #settings>
          <div class="h-full min-w-0 flex-1 overflow-y-auto pl-3">
            <StepInspector
              v-model:node="selectedNode"
              v-model:entry-condition="when"
              :entry-selected="selected?.id === 'start'"
              :plugins="plugins"
              :codec-options="codecOptions"
              :facet-options="facetOptions"
              :facts="props.facts"
              :notification-targets="props.notificationTargets"
              :notification-target-error="props.notificationTargetError"
            />
            <p v-if="!selected" class="rounded-lg border border-default p-4 text-sm text-toned">
              Select Start or a step to edit its settings.
            </p>
          </div>
        </template>
      </USplitter>
    </div>
  </form>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useMediaQuery } from '@vueuse/core';
import type { DropdownMenuItem, SplitterItem } from '@nuxt/ui';
import { conditionFromWhen, conditionProblem, conditionToWhen } from '~/utils/condition';
import { arrangeFlow, type FlowOrientation } from '~/utils/flowLayout';
import {
  initializeInputs,
  buildInputs,
  newConditionNode,
  newPluginNode,
  newTerminalNode,
  type CanvasSelection,
  type DraftNode,
} from '~/utils/stepDraft';
import type {
  FlowEdge,
  FlowNode,
  FlowRow,
  FlowUiNodePos,
  PluginInputField,
  PluginKind,
  PluginRow,
  FlowFact,
  CodecOptions,
} from '~/types/api';
import FlowCanvas from './FlowCanvas.vue';
import StepInspector from './StepInspector.vue';
const props = defineProps<{
  flow: FlowRow | null;
  plugins: PluginRow[];
  codecOptions: CodecOptions;
  facts: FlowFact[];
  notificationTargets: string[];
  notificationTargetError: string;
  fullscreen: boolean;
  disabled?: boolean;
}>();
const emit = defineEmits<{
  submit: [
    {
      name: string;
      definition: {
        entry: string;
        nodes: FlowNode[];
        edges: FlowEdge[];
        actions?: unknown[];
        when?: import('~/types/api').WhenFacets | null;
        _ui: { nodes: Record<string, FlowUiNodePos> };
      };
    },
  ];
}>();
const notify = useNotification();
const root = props.flow
  ? (JSON.parse(JSON.stringify(props.flow.definition)) as FlowRow['definition'])
  : undefined;
const name = ref(props.flow?.name ?? '');
const entry = ref(root?.entry ?? '');
const when = ref(conditionFromWhen(root?.when));
const initialWhen = JSON.stringify(when.value);
const edges = ref<FlowEdge[]>(root?.edges ?? []);
const positions = ref<Record<string, FlowUiNodePos>>(root?._ui?.nodes ?? {});
const nodes = ref<DraftNode[]>([]);
const selected = ref<CanvasSelection | null>(null);
const addedNodeIds = ref<string[]>([]);
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
const plugin = (id: string) => props.plugins.find((p) => p.manifest.id === id);
const fields = (id: string): PluginInputField[] => plugin(id)?.inputs ?? [];
for (const n of root?.nodes ?? []) {
  if (n.type === 'plugin') {
    const d = newPluginNode();
    d.uid = n.id;
    d.plugin = n.plugin;
    d.configVersion = n.config_version ?? 1;
    d.rawInputs = JSON.parse(JSON.stringify(n.inputs ?? {}));
    initializeInputs(d, fields(n.plugin));
    nodes.value.push(d);
  } else if (n.type === 'condition')
    nodes.value.push({ uid: n.id, kind: 'condition', condition: conditionFromWhen(n.when) });
  else nodes.value.push({ uid: n.id, kind: 'terminal', outcome: n.outcome });
}
const selectedNode = computed({
  get: () =>
    selected.value ? (nodes.value.find((n) => n.uid === selected.value?.id) ?? null) : null,
  set: () => undefined,
});
watch(
  () => props.plugins,
  () => {
    for (const node of nodes.value)
      if (node.kind === 'plugin') initializeInputs(node, fields(node.plugin));
  },
  { deep: true },
);
const facetOptions = computed(() => {
  const target = selectedNode.value?.kind === 'condition' ? selectedNode.value.uid : null;
  const options = new Set(props.facts.map((fact) => fact.key));
  if (!target || !entry.value) return [...options];

  const reaches = (start: string) => {
    const seen = new Set<string>();
    const pending = [start];
    while (pending.length) {
      const current = pending.pop()!;
      if (seen.has(current)) continue;
      seen.add(current);
      for (const edge of edges.value) if (edge.src === current) pending.push(edge.dst);
    }
    return seen;
  };
  if (!reaches(entry.value).has(target)) return [...options];

  const producers = new Map<string, Set<string>>();
  for (const node of nodes.value) {
    if (node.kind !== 'plugin') continue;
    const manifest = plugin(node.plugin)?.manifest;
    if (!manifest) continue;
    const namespace = manifest.id.split('.')[1] ?? manifest.id;
    for (const key of Object.keys(manifest.facets)) {
      const facet = `${namespace}.${key}`;
      producers.set(facet, new Set([...(producers.get(facet) ?? []), node.uid]));
    }
  }
  for (const [facet, producerIds] of producers) {
    const pending = [entry.value];
    const seen = new Set<string>();
    while (pending.length) {
      const current = pending.pop()!;
      if (seen.has(current)) continue;
      seen.add(current);
      for (const edge of edges.value) {
        if (edge.src !== current || (edge.port === 'match' && producerIds.has(edge.src))) continue;
        pending.push(edge.dst);
      }
    }
    if (!seen.has(target)) options.add(facet);
  }
  return [...options];
});
const facetTypes = computed(
  () =>
    new Map([
      ...props.facts.map((fact) => [fact.key, fact.type] as const),
      ...props.plugins.flatMap((p) =>
        Object.entries(p.manifest.facets).map(([key, type]) => {
          const namespace = p.manifest.id.split('.')[1] ?? p.manifest.id;
          return [`${namespace}.${key}`, type] as const;
        }),
      ),
    ]),
);
const addPlugin = (id: string) => {
  if (props.disabled) return;
  const n = newPluginNode(new Set(nodes.value.map((node) => node.uid)));
  n.plugin = id;
  n.configVersion = plugin(id)!.manifest.config_version;
  initializeInputs(n, fields(id));
  nodes.value.push(n);
  addedNodeIds.value.push(n.uid);
  if (nodes.value.length === 1) entry.value = n.uid;
  selected.value = { kind: 'node', id: n.uid };
};
const addNode = (kind: 'condition' | 'terminal') => {
  if (props.disabled) return;
  const used = new Set(nodes.value.map((node) => node.uid));
  const n = kind === 'condition' ? newConditionNode(used) : newTerminalNode(used);
  nodes.value.push(n);
  addedNodeIds.value.push(n.uid);
  if (nodes.value.length === 1) entry.value = n.uid;
  selected.value = { kind: 'node', id: n.uid };
};
const arrange = (orientation: FlowOrientation = 'vertical') => {
  if (props.disabled) return;
  positions.value = arrangeFlow(
    {
      entry: entry.value,
      nodes: nodes.value.map((node) => ({ id: node.uid })),
      edges: edges.value,
    },
    orientation,
  );
};
const canArrange = () => Boolean(nodes.value.length && entry.value);
const dirty = useDirtyState(
  computed(() => ({
    name: name.value,
    entry: entry.value,
    when: when.value,
    nodes: nodes.value,
    edges: edges.value,
    positions: positions.value,
  })),
);
dirty.markClean();
defineExpose({ arrange, canArrange, isDirty: dirty.isDirty });
const addItems = computed<DropdownMenuItem[][]>(() => {
  const names = new Map<string, number>();
  for (const plugin of props.plugins)
    names.set(plugin.manifest.name, (names.get(plugin.manifest.name) ?? 0) + 1);
  const groups: Array<[PluginKind, string]> = [
    ['filter', 'Filters'],
    ['inspector', 'Inspectors'],
    ['transcode', 'Transcodes'],
    ['action', 'Actions'],
  ];
  return [
    ...groups
      .map(([kind, label]) => [
        { label, type: 'label' as const },
        ...props.plugins
          .filter((plugin) => plugin.manifest.kind === kind)
          .map((plugin) => ({
            label:
              names.get(plugin.manifest.name)! > 1
                ? `${plugin.manifest.name} (${plugin.manifest.kind})`
                : plugin.manifest.name,
            onSelect: () => addPlugin(plugin.manifest.id),
          })),
      ])
      .filter((group) => group.length > 1),
    [
      { label: 'Flow nodes', type: 'label' },
      { label: 'Add condition', onSelect: () => addNode('condition') },
      { label: 'Add result', onSelect: () => addNode('terminal') },
    ],
  ];
});
const removeNode = (id: string) => {
  if (props.disabled) return;
  nodes.value = nodes.value.filter((n) => n.uid !== id);
  addedNodeIds.value = addedNodeIds.value.filter((addedId) => addedId !== id);
  if (selected.value?.id === id) selected.value = null;
  edges.value = edges.value.filter((e) => e.src !== id && e.dst !== id);
  if (entry.value === id) entry.value = '';
};
const move = (id: string, pos: FlowUiNodePos) => {
  if (props.disabled) return;
  positions.value[id] = pos;
};
const removeEdge = (id: string) => {
  if (props.disabled) return;
  if (id.startsWith('start->')) entry.value = '';
  else edges.value = edges.value.filter((e) => `${e.src}:${e.port}->${e.dst}` !== id);
};
const nodeById = (id: string) => nodes.value.find((node) => node.uid === id);
const isAction = (id: string) => {
  const node = nodeById(id);
  return node?.kind === 'plugin' && plugin(node.plugin)?.manifest.kind === 'action';
};
const downstream = (id: string): string[] => {
  const seen = new Set<string>();
  const pending = [id];
  while (pending.length) {
    const current = pending.pop()!;
    for (const edge of edges.value.filter((candidate) => candidate.src === current)) {
      if (!seen.has(edge.dst)) {
        seen.add(edge.dst);
        pending.push(edge.dst);
      }
    }
  }
  return [...seen];
};
const connect = (c: {
  source: string | null;
  sourceHandle?: string | null;
  target: string | null;
}) => {
  if (props.disabled) return;
  if (!c.source || !c.target || !c.sourceHandle) return;
  const source = c.source;
  const target = c.target;
  if (source === 'start') entry.value = target;
  else {
    const terminalStage =
      nodeById(source)?.kind === 'terminal' ||
      nodes.value.some(
        (node) =>
          node.uid !== source && downstream(node.uid).includes(source) && node.kind === 'terminal',
      );
    if (terminalStage && (!isAction(target) || downstream(target).some((id) => !isAction(id)))) {
      return notify.warning('Result actions can connect only to action plugins.');
    }
    edges.value = [
      ...edges.value.filter((e) => !(e.src === source && e.port === c.sourceHandle)),
      { src: source, port: c.sourceHandle, dst: target },
    ];
  }
};
const submit = () => {
  if (props.disabled) return;
  if (!name.value.trim() || !entry.value || !nodes.value.length)
    return notify.warning('Give the flow a name, choose an entry node, and add at least one node.');
  let flowWhen = root?.when;
  if (JSON.stringify(when.value) !== initialWhen) {
    const problem = conditionProblem('Start', when.value, facetTypes.value);
    if (problem) {
      selected.value = { kind: 'node', id: 'start' };
      return notify.warning(problem);
    }
    flowWhen = conditionToWhen(when.value, facetTypes.value);
  }
  const out: FlowNode[] = [];
  for (const n of nodes.value) {
    if (n.kind === 'plugin') {
      if (!n.plugin) return notify.warning('Every plugin node needs a plugin.');
      const schema = fields(n.plugin);
      const inputs = buildInputs(n, schema);
      for (const field of schema)
        if (field.required && !(field.name in inputs)) {
          selected.value = { kind: 'node', id: n.uid };
          return notify.warning(`Input "${field.name}" is required.`);
        }
      out.push({
        type: 'plugin',
        id: n.uid,
        plugin: n.plugin,
        config_version: n.configVersion,
        ...(Object.keys(inputs).length ? { inputs } : {}),
      });
    } else if (n.kind === 'condition') {
      const problem = conditionProblem(`node "${n.uid}"`, n.condition, facetTypes.value);
      if (problem) {
        selected.value = { kind: 'node', id: n.uid };
        return notify.warning(problem);
      }
      const when = conditionToWhen(n.condition, facetTypes.value);
      if (!when) {
        selected.value = { kind: 'node', id: n.uid };
        return notify.warning('Add at least one rule to this condition node.');
      }
      out.push({ type: 'condition', id: n.uid, when });
    } else out.push({ type: 'terminal', id: n.uid, outcome: n.outcome });
  }
  emit('submit', {
    name: name.value.trim(),
    definition: {
      entry: entry.value,
      nodes: out,
      edges: edges.value,
      actions: root?.actions,
      when: flowWhen,
      _ui: { nodes: positions.value },
    },
  });
};
</script>
