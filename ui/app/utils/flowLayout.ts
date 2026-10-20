import type { FlowEdge, FlowUiNodePos } from '~/types/api';

type FlowLayoutGraph = {
  entry: string;
  nodes: { id: string }[];
  edges: Pick<FlowEdge, 'src' | 'dst'>[];
};

export type FlowOrientation = 'vertical' | 'horizontal';

export const arrangeFlow = (
  definition: FlowLayoutGraph,
  orientation: FlowOrientation = 'vertical',
): Record<string, FlowUiNodePos> => {
  const horizontalGap = 320;
  const verticalGap = 180;
  const order = new Map(definition.nodes.map((node, index) => [node.id, index]));
  const depth = new Map<string, number>([['start', 0]]);
  const links = [
    { src: 'start', dst: definition.entry, order: -1 },
    ...definition.edges.map((edge, index) => ({ ...edge, order: index })),
  ];
  const incoming = new Map<string, number>();
  for (const link of links) incoming.set(link.dst, (incoming.get(link.dst) ?? 0) + 1);
  const queue = ['start'];
  while (queue.length) {
    const source = queue.shift()!;
    for (const link of links.filter((item) => item.src === source)) {
      depth.set(link.dst, Math.max(depth.get(link.dst) ?? 0, depth.get(source)! + 1));
      const remaining = incoming.get(link.dst)! - 1;
      incoming.set(link.dst, remaining);
      if (remaining === 0) queue.push(link.dst);
    }
  }
  const predecessors = new Map<string, number>();
  for (const link of links) predecessors.set(link.dst, predecessors.get(link.dst) ?? link.order);
  const grouped = new Map<number, string[]>();
  for (const node of definition.nodes) {
    const level = depth.get(node.id) ?? 0;
    grouped.set(level, [...(grouped.get(level) ?? []), node.id]);
  }
  for (const ids of grouped.values())
    ids.sort(
      (a, b) =>
        (predecessors.get(a) ?? Infinity) - (predecessors.get(b) ?? Infinity) ||
        order.get(a)! - order.get(b)!,
    );
  const widestLevel = Math.max(...[...grouped.values()].map((ids) => ids.length), 1);
  const crossGap = orientation === 'vertical' ? horizontalGap : verticalGap;
  const depthGap = orientation === 'vertical' ? verticalGap : horizontalGap;
  const centerOffset = (count: number) => ((widestLevel - count) * crossGap) / 2;
  const positions: Record<string, FlowUiNodePos> = {
    start:
      orientation === 'vertical'
        ? { x: 40 + centerOffset(1), y: 40 }
        : { x: 40, y: 40 + centerOffset(1) },
  };
  for (const [level, ids] of grouped)
    ids.forEach((id, index) => {
      positions[id] = {
        x:
          orientation === 'vertical'
            ? 40 + centerOffset(ids.length) + index * crossGap
            : 40 + level * depthGap,
        y:
          orientation === 'vertical'
            ? 40 + level * depthGap
            : 40 + centerOffset(ids.length) + index * crossGap,
      };
    });
  return positions;
};
