import type { FlowDefinition, FlowRow, PluginRow } from '~/types/api';

export type FlowExport = { _type: 'flow'; _version: 1; name: string; definition: FlowDefinition };

export const flowExport = (flow: Pick<FlowRow, 'name' | 'definition'>): FlowExport => ({
  _type: 'flow',
  _version: 1,
  name: flow.name,
  definition: flow.definition,
});

export const hasFlowPasswords = (
  flow: Pick<FlowRow, 'definition'>,
  plugins: readonly PluginRow[],
): boolean =>
  flow.definition.nodes.some((node) => {
    if (node.type !== 'plugin') return false;
    const plugin = plugins.find((plugin) => plugin.manifest.id === node.plugin);
    return (
      plugin?.inputs.some((field) => {
        const value = node.inputs?.[field.name];
        return field.type === 'password' && typeof value === 'string' && value.length > 0;
      }) ?? false
    );
  });

export const encodeFlow = (flow: Pick<FlowRow, 'name' | 'definition'>): string => {
  const bytes = new TextEncoder().encode(JSON.stringify(flowExport(flow)));
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
};

export const decodeFlow = (value: string): unknown => {
  const base64 = value
    .replaceAll('-', '+')
    .replaceAll('_', '/')
    .padEnd(value.length + ((4 - (value.length % 4)) % 4), '=');
  const binary = atob(base64);
  return JSON.parse(
    new TextDecoder().decode(Uint8Array.from(binary, (char) => char.charCodeAt(0))),
  );
};

export const importedFlow = (parsed: unknown): FlowRow | null => {
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  const data = parsed as Record<string, unknown>;
  if (data._type !== 'flow' || data._version !== 1) return null;
  const raw = data.definition;
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const definition = raw as FlowDefinition;
  if (
    typeof definition.entry !== 'string' ||
    !Array.isArray(definition.nodes) ||
    !Array.isArray(definition.edges)
  )
    return null;
  if (
    definition.nodes.some(
      (node) => !node || typeof node !== 'object' || !('id' in node) || !('type' in node),
    )
  )
    return null;
  if (
    definition.edges.some(
      (edge) =>
        !edge ||
        typeof edge !== 'object' ||
        !('src' in edge) ||
        !('dst' in edge) ||
        !('port' in edge),
    )
  )
    return null;
  return {
    id: 0,
    created_at: '',
    name: typeof data.name === 'string' ? data.name : '',
    version: 0,
    definition,
    updated_at: '',
  };
};
