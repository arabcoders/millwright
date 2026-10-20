import type { FlowDefinition, FlowNode, FlowNodeResult } from '~/types/api';

export const tracePort = (node: FlowNode | undefined, step: FlowNodeResult): string | null => {
  if (step.outcome === 'error' && node?.type === 'plugin' && node.plugin === 'action.notify')
    return 'next';
  if (step.outcome === 'deferred' || step.outcome === 'error') return null;
  return node?.type === 'terminal' || step.outcome === 'actions' ? 'next' : step.outcome;
};

export const tracePath = (definition: FlowDefinition, steps: FlowNodeResult[]) => {
  const nodes = new Map(definition.nodes.map((node) => [node.id, node]));
  const taken = new Set<string>();
  const reached = new Set(steps.map((step) => step.node));
  for (const step of steps) {
    const port = tracePort(nodes.get(step.node), step);
    for (const edge of definition.edges) {
      if (edge.src === step.node && edge.port === port) {
        taken.add(`${edge.src}:${edge.port}:${edge.dst}`);
        reached.add(edge.dst);
      }
    }
  }
  return { taken, reached };
};
