import type { NodeRow } from '../types/api';

const percent = (value: number | null | undefined): string =>
  value == null ? 'N/A' : `${Math.round(value)}%`;

export const nodeUsageSummary = (
  node: Pick<
    NodeRow,
    | 'usage'
    | 'capabilities'
    | 'state'
    | 'active_transcodes'
    | 'max_transcodes'
    | 'active_healthchecks'
    | 'max_healthchecks'
    | 'active_mkv_stats'
    | 'max_mkv_stats'
  >,
): string => {
  const usage = node.usage;
  const gpus = usage?.gpus?.length
    ? usage.gpus
    : (node.capabilities?.gpus ?? []).map((name) => ({ name, percent: null }));
  const metrics = [
    `CPU ${percent(usage?.cpu_percent)}`,
    `RAM ${percent(usage?.memory_percent)}`,
    ...gpus.map((gpu, index) => `GPU ${index + 1} ${percent(gpu.percent)}`),
  ].join(' · ');
  return `T ${node.active_transcodes}/${node.max_transcodes} · H.C ${node.active_healthchecks}/${node.max_healthchecks} · MKV ${node.active_mkv_stats}/${node.max_mkv_stats} · ${node.state !== 'online' && usage ? 'Last known: ' : ''}${metrics}`;
};
