import type { JobAttemptRow, JobEventRow, ProbeData } from '~/types/api';
import { validProbeSnapshot } from '~/utils/jobExport';

type SnapshotAttempt = Pick<
  JobAttemptRow,
  'id' | 'input_probe' | 'output_probe' | 'source_size' | 'output_size' | 'result'
>;

export type JobMediaSnapshot = {
  value: string;
  label: string;
  input: ProbeData;
  output: ProbeData;
  sourceSize: number | null;
  outputSize: number | null;
  result: string | null;
};

export const jobSnapshots = (
  attempt: SnapshotAttempt | null,
  events: JobEventRow[],
): JobMediaSnapshot[] => {
  if (!attempt) return [];
  const stages = events
    .filter((event) => event.type === 'stage' && event.attempt_id === attempt.id)
    .sort((a, b) => a.id - b.id)
    .map((event): JobMediaSnapshot => {
      const data = event.payload ?? {};
      return {
        value: `stage-${event.id}`,
        label: typeof data.number === 'number' ? `Stage ${data.number}` : 'Stage',
        input: validProbeSnapshot(data.probe) ? data.probe : null,
        output: validProbeSnapshot(data.output_probe) ? data.output_probe : null,
        sourceSize: typeof data.source_size === 'number' ? data.source_size : null,
        outputSize: typeof data.output_size === 'number' ? data.output_size : null,
        result: typeof data.result === 'string' ? data.result : null,
      };
    });
  return [
    {
      value: 'attempt',
      label: 'Whole attempt',
      input: attempt.input_probe ?? null,
      output: attempt.output_probe ?? null,
      sourceSize: attempt.source_size ?? null,
      outputSize: attempt.output_size ?? null,
      result: attempt.result ?? null,
    },
    ...stages,
  ];
};
