export const healthSamples = (health: Record<string, unknown> | null | undefined): string[] => {
  if (!health || health['healthy'] === true) return [];
  const samples = health['samples'];
  return Array.isArray(samples)
    ? samples.filter((sample): sample is string => typeof sample === 'string')
    : ['Health check reported issues.'];
};
