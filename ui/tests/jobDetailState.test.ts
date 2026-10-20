import { describe, expect, test } from 'bun:test';
import {
  applyJobDetail,
  applyJobProgress,
  applyQueuedJob,
} from '../app/composables/stateHelpers';
import type { JobAttemptRow, JobDetail, JobRow } from '../app/types/api';

const stamp = (fraction: number) => `2026-10-06T12:00:00.${fraction}Z`;
const attempt = (changes: Partial<JobAttemptRow> = {}): JobAttemptRow => ({
  id: 1,
  job_id: 1,
  attempt_number: 1,
  node_id: 1,
  node_name: 'node',
  capabilities: null,
  workload: 'transcode',
  status: 'running',
  result: null,
  error: null,
  source_path: null,
  output_path: null,
  log_path: null,
  argv: null,
  argv_hash: null,
  source_size: null,
  output_size: null,
  health: null,
  action_results: null,
  claimed_at: stamp(0),
  started_at: stamp(0),
  finished_at: null,
  created_at: stamp(0),
  updated_at: stamp(0),
  input_probe: null,
  output_probe: null,
  ...changes,
});
const row = (changes: Partial<JobRow> = {}): JobRow => ({
  id: 1,
  kind: 'transcode',
  status: 'running',
  priority: 0,
  attempts: 1,
  error: null,
  decision_log: null,
  media_file_id: 1,
  file: null,
  library_id: 1,
  node_id: 1,
  created_at: stamp(0),
  updated_at: stamp(0),
  started_at: stamp(0),
  finished_at: null,
  progress: { percent: 10, attempt_id: 1 },
  node_name: 'node',
  latest_attempt: attempt(),
  ...changes,
});
const detail = (changes: Partial<JobDetail> = {}): JobDetail => {
  const { attempts, latest_attempt, ...fields } = row();
  return {
    ...fields,
    attempt_count: attempts,
    attempts: latest_attempt ? [{ ...latest_attempt, log_data: 'saved log' }] : [],
    events: [],
    notification_deliveries: [],
    decision_log: null,
    flow_snapshot: null,
    file_probe: null,
    plan: null,
    continuation: null,
    facets: null,
    pre_actions: null,
    post_actions: null,
    requires: null,
    requires_encoders: null,
    lease_expires_at: null,
    stderr_tail: null,
    ...changes,
  };
};

describe('Job detail updates', () => {
  test('queue resets state', () => {
    const snapshot = detail({
      status: 'pending',
      error: 'Previous failure.',
      finished_at: stamp(0),
    });
    applyQueuedJob(snapshot, { status: 'queued', updated_at: stamp(1) });
    expect(snapshot).toMatchObject({
      status: 'queued',
      error: null,
      progress: null,
      started_at: null,
      finished_at: null,
      updated_at: stamp(1),
    });
    applyQueuedJob(snapshot, { status: 'pending', updated_at: stamp(0) });
    expect(snapshot.status).toBe('queued');
  });
  test('pending preserves history', () => {
    const snapshot = detail({
      status: 'failed',
      error: 'Previous failure.',
      finished_at: stamp(0),
      attempts: [{ ...attempt({ status: 'failed' }), log_data: 'saved log' }],
    });
    applyQueuedJob(snapshot, { status: 'pending', updated_at: stamp(1) });
    expect(snapshot).toMatchObject({
      status: 'pending',
      progress: null,
      started_at: null,
      finished_at: null,
      error: null,
    });
    expect(snapshot.attempt_count).toBe(1);
    expect(snapshot.attempts[0]?.log_data).toBe('saved log');
    expect(applyJobDetail(snapshot, row({ status: 'queued', updated_at: stamp(2) }))).toBe(true);
    expect(snapshot.status).toBe('queued');
    applyQueuedJob(snapshot, { status: 'pending', updated_at: stamp(1) });
    expect(snapshot.status).toBe('queued');
  });
  test('retains overlapping updates', () => {
    const snapshot = detail({ status: 'claimed' });
    expect(applyJobDetail(snapshot, row({ updated_at: stamp(1) }))).toBe(true);
    expect(
      applyJobProgress(snapshot, {
        progress: { percent: 60, attempt_id: 1 },
        updated_at: stamp(3),
      }),
    ).toBe(true);
    expect(applyJobDetail(snapshot, row({ updated_at: stamp(2) }))).toBe(false);
    expect(snapshot.status).toBe('running');
    expect(snapshot.progress?.percent).toBe(60);
    expect(snapshot.updated_at).toBe(stamp(3));
  });

  test('preserves detailed snapshots', () => {
    const snapshot = detail();
    const saved = snapshot.attempts[0]!;
    saved.input_probe = {
      container: 'mkv',
      duration_s: 60,
      size_bytes: 100,
      streams: [],
      raw: { retained: true },
    };
    snapshot.events.push({
      id: 1,
      attempt_id: 1,
      ts: stamp(0),
      type: 'started',
      payload: {},
      created_at: stamp(0),
      updated_at: stamp(0),
    });
    expect(applyJobDetail(snapshot, row({ updated_at: stamp(1) }))).toBe(true);
    expect(snapshot.attempts[0]?.input_probe).toEqual(saved.input_probe);
    expect(snapshot.attempts[0]?.log_data).toBe('saved log');
    expect(snapshot.events.map((event) => event.id)).toEqual([1]);
  });

  test('retains new attempts', () => {
    const snapshot = detail();
    const next = attempt({ id: 2, attempt_number: 2, status: 'claimed' });
    expect(
      applyJobDetail(snapshot, row({ status: 'claimed', attempts: 2, latest_attempt: next })),
    ).toBe(true);
    expect(snapshot.attempt_count).toBe(2);
    expect(snapshot.attempts.map((attempt) => attempt.id)).toEqual([1, 2]);
    expect(snapshot.attempts[0]?.log_data).toBe('saved log');
    expect(
      applyJobProgress(snapshot, {
        progress: { percent: 90, attempt_id: 1 },
        updated_at: stamp(2),
      }),
    ).toBe(false);
    expect(
      applyJobProgress(snapshot, {
        progress: { percent: 5, attempt_id: 2 },
        updated_at: stamp(2),
      }),
    ).toBe(true);
    expect(applyJobDetail(snapshot, row({ updated_at: stamp(3) }))).toBe(false);
    expect(snapshot.attempt_count).toBe(2);
    expect(snapshot.progress?.percent).toBe(5);
  });

  test('keeps terminal precedence', () => {
    const snapshot = detail({ updated_at: stamp(3) });
    expect(
      applyJobDetail(
        snapshot,
        row({
          status: 'succeeded',
          updated_at: stamp(2),
          finished_at: stamp(2),
          latest_attempt: attempt({ status: 'succeeded', result: 'succeeded' }),
        }),
      ),
    ).toBe(true);
    expect(
      applyJobProgress(snapshot, {
        progress: { percent: 99, attempt_id: 1 },
        updated_at: stamp(4),
      }),
    ).toBe(false);
    expect(applyJobDetail(snapshot, row({ updated_at: stamp(1) }))).toBe(false);
    expect(snapshot.status).toBe('succeeded');
    expect(snapshot.attempts[0]?.result).toBe('succeeded');
  });
});
