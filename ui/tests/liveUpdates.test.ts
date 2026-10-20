import { afterEach, beforeAll, describe, expect, mock, test } from 'bun:test';
import * as realUtils from '../app/utils';

const callbacks: Array<(event: ServerEvent) => void> = [];
const reconnects: Array<() => void> = [];
const calls: string[] = [];
let holdRequest = false;
const releases = new Map<() => void, string>();
let responseJob: JobRow | undefined;
let responseJobs: JobRow[] | undefined;
let responseFiles: MediaFileRow[] | undefined;
let failedStatus: string | undefined;
let failedList: 'jobs' | 'files' | undefined;
let responseDetail: Record<string, unknown> | undefined;
let responseBulk:
  | import('../app/composables/useJobs').BulkJobResult
  | import('../app/composables/useJobs').BulkRequestResult
  | undefined;
let responseTotal: number | undefined;
let actionStatus = 200;

mock.module('~/utils', () => ({
  ...realUtils,
  request: (url: string) => {
    calls.push(url);
    if (holdRequest)
      return new Promise<Response>((resolve) => {
        const finish = () => {
          releases.delete(finish);
          resolve(response(url));
        };
        releases.set(finish, url);
      });
    return Promise.resolve(response(url));
  },
  parse_api_error: async (body: { detail?: string } | null) => body?.detail ?? null,
}));
mock.module('~/composables/useApiErrorMessage', () => ({
  useApiErrorMessage: () => ({ messageFor: () => '' }),
}));
mock.module('../app/composables/useEvents', () => ({
  useEvents: () => ({
    on: (callback: (event: ServerEvent) => void) => callbacks.push(callback),
    onReconnect: (callback: () => void) => reconnects.push(callback),
  }),
}));
mock.module('../app/composables/useTranscodeFlow', () => ({
  useTranscodeFlow: () => ({ pick: async () => null }),
}));

type ServerEvent = import('../app/composables/useEvents').ServerEvent;
type JobRow = import('../app/types/api').JobRow;
type MediaFileRow = import('../app/types/api').MediaFileRow;

const job = (id: number, progress: number): JobRow => ({
  id,
  kind: 'transcode',
  status: 'queued',
  priority: 0,
  attempts: 0,
  error: null,
  decision_log: null,
  warning: null,
  hold_until: null,
  media_file_id: id,
  file: null,
  library_id: 1,
  node_id: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  started_at: null,
  finished_at: null,
  progress: { percent: progress },
  node_name: null,
  latest_attempt: null,
});
const file = (id: number, path = `file-${id}.mkv`): MediaFileRow => ({
  id,
  library_id: 1,
  path,
  size: 10,
  container: 'matroska',
  video_codec: 'h264',
  width: 1,
  height: 1,
  duration_s: 1,
  bit_rate: 1,
  medium: null,
  modified_at: '2026-01-01T00:00:00Z',
  cooldown_until: null,
  created_at: '2026-01-01',
  updated_at: '2026-01-01',
  transcode_status: 'pending',
  health_status: 'healthy',
  active_job_kind: null,
  active_job_status: null,
  active_job_id: null,
  active_job_progress: null,
  active_job_updated_at: null,
});
const response = (url: string): Response => {
  if (url === '/api/jobs/bulk' || /^\/api\/jobs\/1(?:\/(cancel|retry|bump))?$/.test(url))
    return Response.json(
      actionStatus >= 200 && actionStatus < 300
        ? (responseBulk ?? (url === '/api/jobs/1' ? { id: 1 } : { id: 1, status: 'pending' }))
        : { detail: 'Action failed.' },
      { status: actionStatus },
    );
  if (url === '/api/jobs/1?events=1') return Response.json(responseDetail);
  if (failedList && url.startsWith(`/api/${failedList}?`))
    return Response.json({ detail: 'The list is unavailable.' }, { status: 503 });
  if (failedStatus && new URL(url, 'http://test').searchParams.get('status') === failedStatus)
    return new Response(null, { status: 500 });
  const isFile = url.startsWith('/api/files');
  const items = isFile
    ? (responseFiles ?? [file(1)])
    : (responseJobs ?? [responseJob ?? job(1, 0)]);
  return new Response(
    JSON.stringify({
      items,
      pagination: { limit: 50, offset: 0, total: responseTotal ?? 1 },
    }),
    {
      status: 200,
    },
  );
};
const emit = (event: ServerEvent) => callbacks.forEach((callback) => callback(event));
const filters = { status: '', limit: 50, offset: 0 };
const fileFilters = { limit: 50, offset: 0 };
const stamps = [
  '2026-01-01T00:00:00Z',
  '2026-01-01T00:00:00.100000Z',
  '2026-01-01T00:00:00.2Z',
  '2026-01-01T00:00:00.300000Z',
];
const running = (attempt: number, percent: number, stamp: string): JobRow => ({
  ...job(1, percent),
  status: 'running',
  attempts: attempt,
  updated_at: stamp,
  latest_attempt: {
    id: attempt,
    attempt_number: attempt,
    status: 'running',
    updated_at: stamp,
  } as JobRow['latest_attempt'],
});
const status = (row: JobRow): ServerEvent => ({
  type: 'job.status',
  payload: {
    job_id: row.id,
    status: row.status,
    priority: row.priority,
    error: row.error,
    warning: row.warning ?? null,
    finished_at: row.finished_at,
    job: row,
  },
});
const progress = (attempt: number, percent: number, stamp: string): ServerEvent => ({
  type: 'job.progress',
  payload: {
    job_id: 1,
    progress: { attempt_id: attempt, percent },
    updated_at: stamp,
  },
});
const releaseAll = () => {
  for (const finish of releases.keys()) finish();
};

beforeAll(() => {
  Bun.plugin({
    name: 'browser-composables',
    setup(build) {
      build.onLoad({ filter: /use(Jobs|Files)\.ts$/ }, async (args) => ({
        contents: (await Bun.file(args.path).text()).replaceAll('import.meta.client', 'true'),
        loader: 'ts',
      }));
    },
  });
});

afterEach(() => {
  releaseAll();
  calls.length = 0;
  holdRequest = false;
  responseJob = undefined;
  responseJobs = undefined;
  responseFiles = undefined;
  failedStatus = undefined;
  failedList = undefined;
  responseDetail = undefined;
  responseBulk = undefined;
  responseTotal = undefined;
  actionStatus = 200;
});

describe('live updates', () => {
  test('retry acknowledges promptly', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJob = { ...job(1, 100), status: 'failed' };
    await jobs.load(filters);
    calls.length = 0;
    failedList = 'jobs';
    actionStatus = 202;
    expect(await jobs.runAction(1, 'retry')).toEqual({
      ok: true,
      accepted: { id: 1, status: 'pending' },
    });
    expect(calls).toEqual(['/api/jobs/1/retry']);
    expect(jobs.lastError.value).toBeNull();
    expect(jobs.isLoading.value).toBe(false);
  });

  test.each(['retry', 'delete'] as const)('bulk %s acknowledges', async (action) => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJobs = [job(1, 0), job(2, 0)];
    responseTotal = 2;
    await jobs.load(filters);
    calls.length = 0;
    failedList = 'jobs';
    actionStatus = 202;
    responseBulk = {
      accepted: [1],
      failed: [{ id: 2, detail: 'Already active.' }],
    };
    expect(await jobs.runBulk(action, [1, 2])).toEqual(responseBulk);
    expect(calls).toEqual(['/api/jobs/bulk']);
    expect(jobs.lastError.value).toBeNull();
    expect(jobs.isLoading.value).toBe(false);
    expect(jobs.items.value.map((row) => row.id)).toEqual([2, 1]);
    if (action === 'delete') {
      emit({ type: 'job.deleted', payload: { job_id: 1 } });
      expect(jobs.items.value.map((row) => row.id)).toEqual([2]);
      expect(jobs.total.value).toBe(1);
      expect(calls).toEqual(['/api/jobs/bulk']);
    }
  });

  test('delete acknowledges promptly', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    await jobs.load(filters);
    calls.length = 0;
    failedList = 'jobs';
    actionStatus = 202;
    expect(await jobs.remove(1)).toEqual({ ok: true });
    expect(calls).toEqual(['/api/jobs/1']);
    expect(jobs.items.value).toEqual([job(1, 0)]);
    expect(jobs.total.value).toBe(1);
    expect(jobs.lastError.value).toBeNull();
    expect(jobs.isLoading.value).toBe(false);
    emit({
      type: 'job.deletion',
      payload: { job_id: 1, outcome: 'failed', reason: 'Workspace cleanup failed.' },
    });
    expect(jobs.items.value).toEqual([job(1, 0)]);
    emit({ type: 'job.deleted', payload: { job_id: 1 } });
    expect(jobs.items.value).toEqual([]);
    expect(jobs.total.value).toBe(0);
    expect(calls).toEqual(['/api/jobs/1']);
  });

  test.each(['single', 'bulk'] as const)('refresh accepts %s', async (mode) => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    await jobs.load(filters);
    holdRequest = true;
    const refresh = jobs.load(filters);
    holdRequest = false;
    calls.length = 0;
    actionStatus = 202;
    if (mode === 'bulk') responseBulk = { accepted: [1], failed: [] };
    try {
      if (mode === 'single') expect(await jobs.remove(1)).toEqual({ ok: true });
      else expect(await jobs.runBulk('delete', [1])).toEqual(responseBulk);
      expect(calls).toEqual([mode === 'single' ? '/api/jobs/1' : '/api/jobs/bulk']);
      expect(jobs.isLoading.value).toBe(true);
      expect(releases.size).toBe(1);
      expect(jobs.items.value).toEqual([job(1, 0)]);
      emit({ type: 'job.deleted', payload: { job_id: 1 } });
      expect(jobs.items.value).toEqual([]);
    } finally {
      releaseAll();
      await refresh;
    }
    expect(jobs.items.value).toEqual([]);
  });

  test('delete retains rejection', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    await jobs.load(filters);
    calls.length = 0;
    actionStatus = 409;
    expect(await jobs.remove(1)).toEqual({
      ok: false,
      error: { status: 409, detail: 'Action failed.' },
    });
    expect(calls).toEqual(['/api/jobs/1']);
    expect(jobs.items.value).toEqual([job(1, 0)]);
  });

  test('pending stays listed', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    const subscriptions = callbacks.length;
    useJobs();
    expect(callbacks).toHaveLength(subscriptions);
    responseJob = {
      ...job(1, 100),
      status: 'failed',
      error: 'Previous failure.',
      started_at: stamps[0]!,
      finished_at: stamps[1]!,
      updated_at: stamps[1]!,
    };
    await jobs.load(filters);
    await jobs.loadProcessing();
    calls.length = 0;
    emit({
      type: 'job.status',
      payload: {
        job_id: 1,
        media_file_id: 1,
        kind: 'transcode',
        status: 'pending',
        updated_at: stamps[2]!,
      },
    });
    expect(jobs.items.value[0]).toMatchObject({
      id: 1,
      status: 'pending',
      progress: null,
      error: null,
      started_at: null,
      finished_at: null,
    });
    expect(jobs.processingJobs.value).toEqual([]);
    emit(status({ ...job(1, 0), status: 'staging', updated_at: stamps[3]! }));
    expect(jobs.items.value[0]?.status).toBe('staging');
    expect(jobs.processingJobs.value).toEqual([]);
    emit({
      type: 'job.requeue',
      payload: { job_id: 1, outcome: 'queued', reason: 'Ready.' },
    });
    expect(calls).toEqual([]);
  });

  test.each([
    ['pending', 'transcode'],
    ['queued', 'healthcheck'],
  ] as const)('file status %s', async (queuedStatus, kind) => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    responseFiles = [
      {
        ...file(1),
        transcode_status: 'failed',
      },
    ];
    await files.load(fileFilters);
    await files.loadRecent();
    calls.length = 0;
    emit({
      type: 'job.status',
      payload: {
        job_id: 1,
        media_file_id: 1,
        kind,
        status: queuedStatus,
        updated_at: stamps[2]!,
      },
    });
    for (const rows of [files.items.value, files.recentItems.value]) {
      expect(rows[0]).toMatchObject({
        active_job_id: 1,
        active_job_status: queuedStatus,
        active_job_progress: null,
        active_job_updated_at: stamps[2]!,
        transcode_status: kind === 'transcode' ? 'pending' : 'failed',
      });
    }
    expect(calls).toEqual([]);
  });

  test('queued status updates', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJob = {
      ...job(1, 0),
      kind: 'healthcheck',
      status: 'pending',
      updated_at: stamps[1]!,
    };
    await jobs.load(filters);
    await jobs.loadProcessing();
    calls.length = 0;
    emit({
      type: 'job.status',
      payload: {
        job_id: 1,
        media_file_id: 1,
        kind: 'healthcheck',
        status: 'queued',
        updated_at: stamps[2]!,
      },
    });
    expect(jobs.items.value[0]).toMatchObject({
      status: 'queued',
      progress: null,
      error: null,
      updated_at: stamps[2]!,
    });
    expect(jobs.processingJobs.value).toEqual([]);
    emit({
      type: 'job.status',
      payload: {
        job_id: 1,
        media_file_id: 1,
        kind: 'healthcheck',
        status: 'pending',
        updated_at: stamps[1]!,
      },
    });
    expect(jobs.items.value[0]?.status).toBe('queued');
    expect(calls).toEqual([]);
  });

  test('pending snapshots normalize', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJob = {
      ...job(1, 100),
      status: 'pending',
      error: 'Previous failure.',
      started_at: stamps[0]!,
      finished_at: stamps[1]!,
      updated_at: stamps[2]!,
    };
    await jobs.load(filters);
    await jobs.loadProcessing();
    expect(jobs.items.value[0]).toMatchObject({
      status: 'pending',
      progress: null,
      error: null,
      started_at: null,
      finished_at: null,
      updated_at: stamps[2]!,
    });
    expect(calls.some((url) => url.includes('status=claimed,running,verifying,finalizing'))).toBe(
      true,
    );
  });

  test('pending file history', async () => {
    const { applyQueuedFileJob } = await import('../app/composables/stateHelpers');
    const detail = {
      ...file(1),
      probe: null,
      jobs: [
        {
          id: 9,
          kind: 'healthcheck',
          status: 'failed',
          error: 'Previous failure.',
          created_at: stamps[0]!,
        },
      ],
    };
    const pending = {
      job_id: 9,
      media_file_id: 1,
      kind: 'healthcheck',
      status: 'pending' as const,
      updated_at: stamps[2]!,
    };
    expect(applyQueuedFileJob(detail, pending)).toBe(true);
    expect(detail.jobs[0]).toMatchObject({ status: 'pending', error: null });
    expect(detail.active_job_id).toBe(9);
    expect(detail.active_job_kind).toBe('healthcheck');
    expect(detail.active_job_status).toBe('pending');
    expect(detail.transcode_status).toBe('pending');
    detail.active_job_status = 'running';
    detail.active_job_updated_at = stamps[3]!;
    expect(applyQueuedFileJob(detail, pending)).toBe(false);
    expect(detail.active_job_status).toBe('running');
  });

  test.each([
    ['pending', 901],
    ['queued', 902],
  ] as const)('terminal rejects %s', async (queuedStatus, id) => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    const catalogue = {
      ...file(id),
      updated_at: stamps[0]!,
      transcode_status: 'failed' as const,
    };
    responseFiles = [catalogue];
    await files.load(fileFilters);
    await files.loadRecent();
    const update = {
      job_id: id,
      media_file_id: id,
      kind: 'transcode',
      status: queuedStatus,
      updated_at: stamps[1]!,
    };
    emit({ type: 'job.status', payload: update });
    emit(
      status({
        ...job(id, 0),
        status: 'cancelled',
        updated_at: stamps[3]!,
        finished_at: stamps[3]!,
      }),
    );
    emit({
      type: 'file.status',
      payload: { action: 'updated', library_id: 1, file: { ...catalogue } },
    });
    await files.load(fileFilters);
    await files.loadRecent();
    calls.length = 0;
    emit({ type: 'job.status', payload: update });
    for (const rows of [files.items.value, files.recentItems.value]) {
      expect(rows[0]).toMatchObject({
        active_job_id: null,
        active_job_status: null,
        active_job_updated_at: null,
        transcode_status: 'failed',
        updated_at: stamps[0]!,
      });
    }
    expect(calls).toEqual([]);
  });

  test('rejects stale queue', async () => {
    const { applyQueuedFileJob, retainFileJobStatus } =
      await import('../app/composables/stateHelpers');
    const detail = { ...file(903), probe: null, jobs: [] };
    retainFileJobStatus(detail, {
      ...job(903, 0),
      status: 'cancelled',
      updated_at: stamps[3]!,
    });
    const replacement = { ...file(903), probe: null, jobs: [] };
    retainFileJobStatus(replacement, detail);
    expect(
      applyQueuedFileJob(replacement, {
        job_id: 903,
        media_file_id: 903,
        kind: 'healthcheck',
        status: 'queued',
        updated_at: stamps[1]!,
      }),
    ).toBe(false);
    expect(replacement.active_job_id).toBeNull();
    expect(
      applyQueuedFileJob(replacement, {
        job_id: 904,
        media_file_id: 903,
        kind: 'healthcheck',
        status: 'queued',
        updated_at: '2026-01-01T00:00:01Z',
      }),
    ).toBe(true);
    expect(replacement.active_job_id).toBe(904);
  });

  test.each(['cancel', 'bump'] as const)('bulk refreshes %s', async (action) => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    const selectedFilters = {
      ...filters,
      status: 'queued',
      query: 'movie',
      kind: 'transcode',
      limit: 999,
      offset: 999,
      sort_by: 'priority' as const,
      sort_dir: 'asc' as const,
    };
    await jobs.load(selectedFilters);
    const listUrl = calls.at(-1)!;
    calls.length = 0;
    responseJobs = [];
    responseTotal = 0;
    responseBulk = {
      succeeded: [1],
      failed: [],
    };
    expect(await jobs.runBulk(action, [1])).toEqual(responseBulk);
    expect(calls).toEqual(['/api/jobs/bulk', listUrl]);
    expect(jobs.items.value).toEqual([]);
    expect(jobs.total.value).toBe(0);
    expect(jobs.isLoading.value).toBe(false);
  });

  test('bulk partial refresh', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJobs = [job(1, 0), job(2, 0)];
    responseTotal = 2;
    await jobs.load(filters);
    const listUrl = calls.at(-1)!;
    calls.length = 0;
    responseJobs = [job(2, 0)];
    responseTotal = 1;
    responseBulk = {
      succeeded: [1],
      failed: [{ id: 2, detail: 'Job already finished.' }],
    };
    expect(await jobs.runBulk('cancel', [1, 2])).toEqual(responseBulk);
    expect(calls).toEqual(['/api/jobs/bulk', listUrl]);
    expect(jobs.items.value.map((row) => row.id)).toEqual([2]);
    expect(jobs.total.value).toBe(1);
  });

  test.each(['cancel', 'bump'] as const)('action refreshes %s', async (action) => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    await jobs.load(filters);
    const listUrl = calls.at(-1)!;
    calls.length = 0;
    responseJobs = [{ ...job(1, 0), status: 'cancelled' }];
    const result = await jobs.runAction(1, action);
    expect(result.ok).toBe(true);
    expect(calls).toEqual([`/api/jobs/1/${action}`, listUrl]);
    expect(jobs.items.value).toEqual(responseJobs);
  });

  test.each(['cancel', 'delete'] as const)('bulk %s rejection', async (action) => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    await jobs.load(filters);
    calls.length = 0;
    actionStatus = 409;
    expect(await jobs.runBulk(action, [1])).toEqual({
      error: { status: 409, detail: 'Action failed.' },
    });
    expect(calls).toEqual(['/api/jobs/bulk']);
    expect(jobs.items.value).toEqual([job(1, 0)]);
  });

  test('bulk refresh failure', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    await jobs.load(filters);
    failedList = 'jobs';
    responseBulk = { succeeded: [1], failed: [] };
    expect(await jobs.runBulk('cancel', [1])).toEqual(responseBulk);
    expect(jobs.lastError.value).toEqual({
      status: 503,
      detail: 'The list is unavailable.',
    });
    expect(jobs.isLoading.value).toBe(false);
  });

  test('files retain full page', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    responseFiles = Array.from({ length: 999 }, (_, index) => file(index + 1));
    responseTotal = 1000;
    await files.load({ limit: 999, offset: 0, sort_by: 'id', sort_dir: 'asc' });
    expect(new URL(calls.at(-1)!, 'http://test').searchParams.get('limit')).toBe('999');
    expect(files.items.value.map((file) => file.id)).toEqual(
      Array.from({ length: 999 }, (_, index) => index + 1),
    );
    expect(files.total.value).toBe(1000);
  });

  test('recent isolates filters', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    const retained = { ...file(9, 'retained.mkv'), library_id: 2 };
    responseFiles = [retained];
    await files.load({ ...fileFilters, library_id: '2', query: 'retained' });
    calls.length = 0;
    responseFiles = [
      { ...file(1), created_at: '2026-01-01T00:00:00Z' },
      { ...file(2), created_at: '2026-01-02T00:00:00Z' },
    ];
    await files.loadRecent();
    expect(calls).toEqual(['/api/files?limit=6&offset=0&sort_by=created_at&sort_dir=desc']);
    expect(files.recentItems.value.map((row) => row.id)).toEqual([2, 1]);
    expect(files.items.value).toEqual([retained]);
    expect(files.total.value).toBe(1);
    emit({
      type: 'file.status',
      payload: {
        action: 'added',
        library_id: 1,
        file: { ...file(3), created_at: '2026-01-03T00:00:00Z' },
      },
    });
    expect(files.recentItems.value.map((row) => row.id)).toEqual([3, 2, 1]);
    expect(files.items.value).toEqual([retained]);
    expect(files.total.value).toBe(1);
    responseFiles = [];
    await files.load({ ...fileFilters, library_id: '3' });
    expect(files.recentItems.value.map((row) => row.id)).toEqual([3, 2, 1]);
  });

  test('recent stays bounded', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    responseFiles = Array.from({ length: 6 }, (_, index) => ({
      ...file(index + 1),
      created_at: `2026-01-0${index + 1}T00:00:00Z`,
    }));
    await files.loadRecent();
    calls.length = 0;
    for (const id of [7, 7, 8]) {
      emit({
        type: 'file.status',
        payload: {
          action: 'added',
          library_id: 1,
          file: { ...file(id), created_at: `2026-01-0${id}T00:00:00Z` },
        },
      });
    }
    expect(files.recentItems.value.map((row) => row.id)).toEqual([8, 7, 6, 5, 4, 3]);
    expect(calls).toEqual([]);
  });

  test('recent deletion merge', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    responseFiles = Array.from({ length: 6 }, (_, index) => file(index + 1));
    for (const overlap of [false, true]) {
      holdRequest = false;
      await files.loadRecent();
      holdRequest = overlap;
      calls.length = 0;
      const pending = overlap ? files.loadRecent() : Promise.resolve();
      try {
        emit({
          type: 'file.status',
          payload: { action: 'deleted', library_id: 1, media_file_id: 6 },
        });
        expect(files.recentItems.value.map((row) => row.id)).toEqual([5, 4, 3, 2, 1]);
        releaseAll();
        await pending;
        expect(files.recentItems.value.map((row) => row.id)).toEqual([5, 4, 3, 2, 1]);
        expect(calls).toHaveLength(overlap ? 1 : 0);
      } finally {
        releaseAll();
        await pending;
      }
    }
  });

  test.each(['deleted', 'pruned'] as const)('removed file lists %s', async (action) => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    responseFiles = [file(1), file(2)];
    responseTotal = 2;
    await files.load(fileFilters);
    await files.loadRecent();
    calls.length = 0;
    emit({ type: 'file.status', payload: { action, library_id: 1, media_file_id: 1 } });
    expect(files.items.value.map((row) => row.id)).toEqual([2]);
    expect(files.recentItems.value.map((row) => row.id)).toEqual([2]);
    expect(files.total.value).toBe(1);
    expect(calls).toEqual([]);
  });

  test('file deletion merge', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    responseFiles = [file(1), file(2)];
    responseTotal = 2;
    await files.load(fileFilters);
    await files.loadRecent();
    holdRequest = true;
    calls.length = 0;
    const pending = Promise.all([files.load(fileFilters), files.loadRecent()]);
    try {
      emit({
        type: 'file.status',
        payload: { action: 'deleted', library_id: 1, media_file_id: 1 },
      });
      releaseAll();
      await pending;
      expect(files.items.value.map((row) => row.id)).toEqual([2]);
      expect(files.recentItems.value.map((row) => row.id)).toEqual([2]);
      expect(files.total.value).toBe(1);
      expect(calls).toHaveLength(2);
    } finally {
      releaseAll();
      await pending;
    }
  });

  test('recent failure retention', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    responseFiles = [file(2)];
    await files.loadRecent();
    emit({ type: 'events.resync', payload: { reason: 'overflow' } });
    failedList = 'files';
    await expect(files.loadRecent()).rejects.toThrow();
    expect(files.recentItems.value.map((row) => row.id)).toEqual([2]);
    expect(files.recentError.value).toBeInstanceOf(Error);
    expect(files.recentLoading.value).toBe(false);
    failedList = undefined;
    responseFiles = [file(3)];
    await files.loadRecent();
    expect(files.recentItems.value.map((row) => row.id)).toEqual([3]);
    expect(files.recentError.value).toBeNull();
  });

  test('recent progress overlaps', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    const active = {
      ...file(1),
      active_job_id: 1,
      active_job_kind: 'healthcheck',
      active_job_status: 'running',
      active_job_progress: { percent: 20 },
      active_job_updated_at: stamps[0]!,
    };
    responseFiles = [active];
    await files.loadRecent();
    holdRequest = true;
    const pending = files.loadRecent();
    try {
      emit(progress(1, 60, stamps[2]!));
      emit(progress(1, 40, stamps[1]!));
      expect(files.recentItems.value[0]?.active_job_progress?.percent).toBe(60);
      releaseAll();
      await pending;
      emit({
        type: 'file.status',
        payload: { action: 'updated', library_id: 1, file: { ...active } },
      });
      expect(files.recentItems.value[0]?.active_job_progress?.percent).toBe(60);
      emit({
        type: 'file.status',
        payload: { action: 'updated', library_id: 1, file: file(1) },
      });
      emit(progress(1, 99, stamps[3]!));
      expect(files.recentItems.value[0]?.active_job_id).toBeNull();
      expect(files.recentItems.value[0]?.active_job_progress).toBeNull();
    } finally {
      releaseAll();
      await pending;
    }
  });

  test('separates file states', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    await files.load({ ...fileFilters, transcode_status: 'pending' });
    calls.length = 0;
    emit({
      type: 'file.status',
      payload: {
        action: 'updated',
        library_id: 1,
        file: {
          ...file(1),
          transcode_status: 'queued',
          active_job_kind: 'transcode',
          active_job_status: 'queued',
          active_job_id: 1,
        },
      },
    });
    expect(files.items.value).toEqual([]);
    expect(files.total.value).toBe(0);
    expect(calls).toEqual([]);

    responseFiles = [{ ...file(1), transcode_status: 'failed' }];
    await files.load({ ...fileFilters, transcode_status: 'failed' });
    calls.length = 0;
    emit({
      type: 'file.status',
      payload: {
        action: 'updated',
        library_id: 1,
        file: { ...file(1), transcode_status: 'not_needed' },
      },
    });
    expect(files.items.value).toEqual([]);
    expect(files.total.value).toBe(0);
    expect(calls).toEqual([]);
  });

  test('file progress updates', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    const active = {
      ...file(1),
      active_job_id: 1,
      active_job_kind: 'transcode',
      active_job_status: 'running',
      active_job_progress: { percent: 20, attempt_id: 1 },
      active_job_updated_at: stamps[0]!,
    };
    responseFiles = [active];
    await files.load(fileFilters);
    calls.length = 0;
    emit(progress(1, 40, stamps[2]!));
    emit(progress(1, 30, stamps[1]!));
    emit({
      type: 'job.progress',
      payload: {
        job_id: 999,
        progress: { percent: 99 },
        updated_at: stamps[3]!,
      },
    });
    expect(files.items.value[0]?.active_job_progress?.percent).toBe(40);
    expect(files.items.value[0]?.updated_at).toBe(active.updated_at);
    emit({
      type: 'file.status',
      payload: {
        action: 'updated',
        library_id: 1,
        file: { ...active, active_job_updated_at: stamps[1]! },
      },
    });
    expect(files.items.value[0]?.active_job_progress?.percent).toBe(40);
    emit({
      type: 'file.status',
      payload: { action: 'updated', library_id: 1, file: file(1) },
    });
    emit(progress(1, 99, stamps[3]!));
    expect(files.items.value[0]?.active_job_progress).toBeNull();
    expect(files.items.value[0]?.active_job_id).toBeNull();
    expect(calls).toEqual([]);
  });

  test('file progress overlaps', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    const active = {
      ...file(1),
      active_job_id: 1,
      active_job_kind: 'healthcheck',
      active_job_status: 'running',
      active_job_progress: { percent: 20 },
      active_job_updated_at: stamps[0]!,
    };
    for (const firstLoad of [true, false]) {
      responseFiles = firstLoad ? [] : [active];
      await files.load(fileFilters);
      responseFiles = [active];
      holdRequest = true;
      const pending = files.load(fileFilters);
      try {
        emit(progress(1, 60, stamps[2]!));
        emit(progress(1, 40, stamps[1]!));
        releaseAll();
        await pending;
        expect(files.items.value[0]?.active_job_progress?.percent).toBe(60);
        expect(files.items.value[0]?.active_job_updated_at).toBe(stamps[2]!);
      } finally {
        releaseAll();
        await pending;
        holdRequest = false;
      }
    }
  });

  test('file terminal overlaps', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    responseFiles = [
      {
        ...file(1),
        active_job_id: 1,
        active_job_kind: 'transcode',
        active_job_status: 'running',
        active_job_progress: { percent: 20 },
        active_job_updated_at: stamps[0]!,
      },
    ];
    await files.load(fileFilters);
    holdRequest = true;
    const pending = files.load(fileFilters);
    try {
      emit(progress(1, 60, stamps[1]!));
      emit({
        type: 'file.status',
        payload: { action: 'updated', library_id: 1, file: file(1) },
      });
      emit(progress(1, 99, stamps[3]!));
      releaseAll();
      await pending;
      expect(files.items.value[0]?.active_job_id).toBeNull();
      expect(files.items.value[0]?.active_job_progress).toBeNull();
    } finally {
      releaseAll();
      await pending;
    }
  });

  test('file job replacement', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    responseFiles = [
      {
        ...file(1),
        active_job_id: 1,
        active_job_kind: 'transcode',
        active_job_status: 'running',
        active_job_progress: { percent: 90 },
        active_job_updated_at: stamps[0]!,
      },
    ];
    await files.load(fileFilters);
    files.upsert({
      ...file(1),
      active_job_id: 2,
      active_job_kind: 'transcode',
      active_job_status: 'claimed',
      active_job_progress: null,
      active_job_updated_at: stamps[1]!,
    });
    emit(progress(1, 99, stamps[3]!));
    expect(files.items.value[0]?.active_job_progress).toBeNull();
    emit({
      type: 'job.progress',
      payload: { job_id: 2, progress: { percent: 10 }, updated_at: stamps[2]! },
    });
    expect(files.items.value[0]?.active_job_progress?.percent).toBe(10);
  });

  test('jobs isolate filters', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    const view = { ...filters, query: 'failed-jobs' };
    failedList = 'jobs';
    expect(await jobs.load(view)).toBe(false);
    expect(jobs.lastError.value).toMatchObject({
      status: 503,
      detail: 'The list is unavailable.',
    });
    expect(jobs.items.value).toEqual([]);
    expect(jobs.total.value).toBe(0);
    failedList = undefined;
    await jobs.load(view);
    failedList = 'jobs';
    expect(await jobs.load(view)).toBe(false);
    expect(jobs.items.value.map((row) => row.id)).toEqual([1]);
    expect(jobs.total.value).toBe(1);
    holdRequest = true;
    const pending = jobs.load({ ...view, kind: 'healthcheck' });
    try {
      expect(jobs.isLoading.value).toBe(true);
      expect(jobs.items.value).toEqual([]);
      expect(jobs.total.value).toBe(0);
      releaseAll();
      expect(await pending).toBe(false);
      expect(jobs.lastError.value?.status).toBe(503);
      expect(jobs.items.value).toEqual([]);
      holdRequest = false;
      failedList = undefined;
      await jobs.load(view);
      expect(jobs.lastError.value).toBeNull();
      expect(jobs.items.value.map((row) => row.id)).toEqual([1]);
    } finally {
      releaseAll();
      await pending;
    }
  });

  test('files isolate filters', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    const view = { ...fileFilters, query: 'failed-files' };
    failedList = 'files';
    await expect(files.load(view)).rejects.toThrow();
    expect(files.lastError.value).toBeInstanceOf(Error);
    expect(files.items.value).toEqual([]);
    expect(files.total.value).toBe(0);
    failedList = undefined;
    await files.load(view);
    failedList = 'files';
    await expect(files.load(view)).rejects.toThrow();
    expect(files.items.value.map((row) => row.id)).toEqual([1]);
    expect(files.total.value).toBe(1);
    holdRequest = true;
    const pending = files.load({ ...view, library_id: '2' }).catch((error) => error);
    try {
      expect(files.isLoading.value).toBe(true);
      expect(files.items.value).toEqual([]);
      expect(files.total.value).toBe(0);
      releaseAll();
      expect(await pending).toBeInstanceOf(Error);
      expect(files.lastError.value).toBeInstanceOf(Error);
      expect(files.items.value).toEqual([]);
      holdRequest = false;
      failedList = undefined;
      await files.load(view);
      expect(files.lastError.value).toBeNull();
      expect(files.items.value.map((row) => row.id)).toEqual([1]);
    } finally {
      releaseAll();
      await pending;
    }
  });

  test('late failures obsolete', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const { useFiles } = await import('../app/composables/useFiles');
    const jobs = useJobs();
    const files = useFiles();
    holdRequest = true;
    const oldJobs = jobs.load({ ...filters, query: 'old' });
    const oldFiles = files.load({ ...fileFilters, query: 'old' }).catch((error) => error);
    try {
      holdRequest = false;
      await jobs.load({ ...filters, query: 'current' });
      await files.load({ ...fileFilters, query: 'current' });
      failedList = 'jobs';
      const jobRelease = [...releases].find(([, url]) => url.startsWith('/api/jobs?'));
      expect(jobRelease).toBeDefined();
      jobRelease![0]();
      expect(await oldJobs).toBe(false);
      failedList = 'files';
      releaseAll();
      expect(await oldFiles).toBeInstanceOf(Error);
      expect(jobs.lastError.value).toBeNull();
      expect(files.lastError.value).toBeNull();
      expect(jobs.items.value.map((row) => row.id)).toEqual([1]);
      expect(files.items.value.map((row) => row.id)).toEqual([1]);
    } finally {
      releaseAll();
      await Promise.all([oldJobs, oldFiles]);
    }
  });

  test('initial overlaps show rows', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJob = running(1, 0, stamps[0]!);
    holdRequest = true;
    const pending = Promise.all([jobs.load(filters), jobs.loadProcessing()]);
    try {
      emit(progress(1, 99, stamps[2]!));
      releaseAll();
      const result = await pending;
      expect(result[0]).toBe(true);
      expect(jobs.items.value.map((row) => row.id)).toEqual([1]);
      expect(jobs.processingJobs.value.map((row) => row.id)).toEqual([1]);
    } finally {
      releaseAll();
      await pending;
    }
  });

  test('events do not reload loaded lists', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const { useFiles } = await import('../app/composables/useFiles');
    const jobs = useJobs();
    const files = useFiles();
    await jobs.load(filters);
    await files.load(fileFilters);
    await jobs.loadProcessing();
    calls.length = 0;
    for (let id = 2; id < 1002; id++) {
      emit({
        type: 'job.status',
        payload: {
          job_id: id,
          status: 'queued',
          priority: 0,
          error: null,
          warning: null,
          finished_at: null,
          job: job(id, 0),
        },
      });
      emit({
        type: 'file.status',
        payload: { action: 'probed', library_id: 1, file: file(id) },
      });
    }
    emit({
      type: 'job.progress',
      payload: { job_id: 1, progress: { percent: 42 }, updated_at: 'now' },
    });
    emit({
      type: 'job.status',
      payload: {
        job_id: 2,
        status: 'queued',
        priority: 0,
        error: null,
        warning: null,
        finished_at: null,
        job: job(2, 0),
      },
    });
    emit({ type: 'job.deleted', payload: { job_id: 1 } });
    emit({
      type: 'file.status',
      payload: {
        action: 'updated',
        library_id: 1,
        file: file(1001, 'changed.mkv'),
      },
    });
    emit({ type: 'events.batch', payload: { events: [] } });
    emit({
      type: 'library.scan',
      payload: {
        status: 'succeeded',
        reason: 'manual',
        library_id: 1,
        library_name: 'x',
        found: 1,
        added: 0,
        updated: 1,
        unchanged: 0,
        pruned: 0,
        error_count: 0,
        error_samples: [],
      },
    });
    emit({ type: 'events.resync', payload: { reason: 'overflow' } });
    expect(calls).toEqual([]);
    expect(new Set(jobs.items.value.map((row) => row.id)).size).toBe(jobs.items.value.length);
    expect(jobs.items.value.length).toBeLessThanOrEqual(50);
    expect(new Set(files.items.value.map((row) => row.id)).size).toBe(files.items.value.length);
    expect(files.items.value.length).toBeLessThanOrEqual(50);
    expect(files.items.value.find((row) => row.id === 1001)?.path).toBe('changed.mkv');
  });

  test('manual loads request once', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const { useFiles } = await import('../app/composables/useFiles');
    await useJobs().load(filters);
    await useFiles().load(fileFilters);
    await useJobs().loadProcessing();
    expect(
      calls.filter((url) => url.startsWith('/api/jobs?') && url.includes('limit=50')),
    ).toHaveLength(1);
    expect(
      calls.filter((url) => url.startsWith('/api/files?') && url.includes('limit=50')),
    ).toHaveLength(1);
  });

  test('refresh removes inactive', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    for (const reconnect of [false, true]) {
      for (const missed of ['deleted', 'terminal']) {
        holdRequest = false;
        responseJobs = [running(1, 0, stamps[0]!)];
        await jobs.loadProcessing();
        if (reconnect) reconnects.forEach((callback) => callback());
        else emit({ type: 'events.resync', payload: { reason: 'overflow' } });
        responseJobs =
          missed === 'deleted'
            ? []
            : [
                {
                  ...running(1, 0, stamps[2]!),
                  status: 'succeeded',
                  progress: null,
                },
              ];
        holdRequest = true;
        calls.length = 0;
        const pending = jobs.loadProcessing();
        try {
          expect(jobs.processingLoading.value).toBe(true);
          await jobs.loadProcessing();
          expect(calls).toHaveLength(1);
          releaseAll();
          await pending;
          expect(jobs.processingLoading.value).toBe(false);
          expect(jobs.processingJobs.value).toEqual([]);
        } finally {
          releaseAll();
          await pending;
        }
      }
    }
  });

  test('processing failure visible', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJob = running(1, 0, stamps[0]!);
    await jobs.loadProcessing();
    emit({ type: 'events.resync', payload: { reason: 'overflow' } });
    failedStatus = 'claimed,running,verifying,finalizing';
    await jobs.loadProcessing();
    expect(jobs.processingJobs.value[0]?.id).toBe(1);
    expect(jobs.processingLoading.value).toBe(false);
    expect(jobs.processingError.value?.status).toBe(500);
    failedStatus = undefined;
    await jobs.loadProcessing();
    expect(jobs.processingError.value).toBeNull();
  });

  test('processing order stays', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJobs = [3, 1, 2].map((id) => ({ ...job(id, 0), status: 'running' }));
    responseTotal = 3;
    await jobs.loadProcessing();
    const query = new URL(calls.at(-1)!, 'http://test').searchParams;
    expect(query.get('sort_by')).toBe('id');
    expect(query.get('sort_dir')).toBe('asc');
    expect(jobs.processingJobs.value.map((row) => row.id)).toEqual([1, 2, 3]);
    await jobs.load({ ...filters, sort_by: 'updated_at', sort_dir: 'desc' });
    calls.length = 0;

    emit({
      type: 'job.progress',
      payload: { job_id: 1, progress: { percent: 25 }, updated_at: stamps[1]! },
    });
    expect(jobs.processingJobs.value.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(jobs.items.value[0]?.id).toBe(1);
    emit({
      type: 'job.progress',
      payload: { job_id: 3, progress: { percent: 50 }, updated_at: stamps[2]! },
    });
    expect(jobs.processingJobs.value.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(jobs.items.value[0]?.id).toBe(3);
    emit(status({ ...job(2, 75), status: 'running', updated_at: stamps[3]! }));
    expect(jobs.processingJobs.value.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(jobs.items.value[0]?.id).toBe(2);
    expect(jobs.processingJobs.value.map((row) => row.progress?.percent)).toEqual([25, 75, 50]);

    emit(status({ ...job(4, 0), status: 'running', updated_at: stamps[3]! }));
    expect(jobs.processingJobs.value.map((row) => row.id)).toEqual([1, 2, 3, 4]);
    emit(status({ ...job(2, 100), status: 'succeeded', updated_at: '2026-01-01T00:00:00.4Z' }));
    expect(jobs.processingJobs.value.map((row) => row.id)).toEqual([1, 3, 4]);
    expect(jobs.processingTotal.value).toBe(3);
    expect(calls).toEqual([]);
  });

  test('cancellation retains jobs', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    for (const terminal of ['cancelled', 'succeeded'] as const) {
      holdRequest = false;
      responseJob = running(1, 0, stamps[0]!);
      await jobs.load(filters);
      await jobs.loadProcessing();
      holdRequest = true;
      const pending = Promise.all([jobs.load(filters), jobs.loadProcessing()]);
      try {
        emit({ type: 'job.cancel_requested', payload: { job_id: 1 } });
        expect(jobs.processingJobs.value[0]?.status).toBe('running');
        emit(progress(1, 75, stamps[1]!));
        expect(jobs.processingJobs.value[0]?.progress?.percent).toBe(75);
        emit(
          status({
            ...running(1, 0, stamps[2]!),
            status: terminal,
            progress: null,
          }),
        );
        expect(jobs.processingJobs.value).toEqual([]);
        emit({ type: 'job.cancel_requested', payload: { job_id: 1 } });
        emit(progress(1, 99, stamps[3]!));
        releaseAll();
        await pending;
        expect(jobs.items.value[0]?.status).toBe(terminal);
        expect(jobs.processingJobs.value).toEqual([]);
        expect(jobs.items.value[0]?.progress).toBeNull();
      } finally {
        releaseAll();
        await pending;
      }
    }
  });

  test('dashboard splits jobs', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const { dashboardJobStatusOptions, processingJobStatuses } =
      await import('../app/utils/jobList');
    const jobs = useJobs();
    const statuses: JobRow['status'][] = [
      'pending',
      'staging',
      'hold',
      'queued',
      'succeeded',
      'skipped',
      'failed',
      'cancelled',
      ...processingJobStatuses,
    ];
    responseJobs = statuses.map((value, index) => ({
      ...job(index + 1, 0),
      status: value,
    }));
    responseTotal = dashboardJobStatusOptions.length;
    const view = {
      ...filters,
      status: dashboardJobStatusOptions.map((option) => option.value).join(','),
      sort_by: 'id' as const,
      sort_dir: 'asc' as const,
    };
    await jobs.load(view);
    responseTotal = processingJobStatuses.length;
    await jobs.loadProcessing();
    expect(jobs.items.value.map((row) => row.status)).toEqual(
      dashboardJobStatusOptions.map((option) => option.value),
    );
    expect(jobs.processingJobs.value.map((row) => row.status)).toEqual(processingJobStatuses);
    expect(jobs.total.value).toBe(dashboardJobStatusOptions.length);
    expect(jobs.processingTotal.value).toBe(processingJobStatuses.length);
    calls.length = 0;
    const moved = {
      ...job(1, 0),
      status: 'running' as const,
      updated_at: stamps[1]!,
    };
    emit(status(moved));
    expect(jobs.items.value.some((row) => row.id === moved.id)).toBe(false);
    expect(jobs.processingJobs.value.find((row) => row.id === moved.id)?.status).toBe('running');
    emit(status({ ...moved, status: 'succeeded', updated_at: stamps[2]! }));
    expect(jobs.processingJobs.value.some((row) => row.id === moved.id)).toBe(false);
    expect(jobs.items.value.find((row) => row.id === moved.id)?.status).toBe('succeeded');
    expect(calls).toEqual([]);
  });

  test('split preserves attempts', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const { dashboardJobStatusOptions } = await import('../app/utils/jobList');
    const jobs = useJobs();
    responseJob = running(2, 0, stamps[1]!);
    responseTotal = 0;
    await jobs.load({
      ...filters,
      status: dashboardJobStatusOptions.map((option) => option.value).join(','),
    });
    responseTotal = 1;
    await jobs.loadProcessing();
    emit(status({ ...running(1, 100, stamps[3]!), status: 'failed' }));
    expect(jobs.items.value).toEqual([]);
    expect(jobs.processingJobs.value[0]?.attempts).toBe(2);
    emit(
      status({
        ...running(2, 100, stamps[2]!),
        status: 'succeeded',
        progress: null,
      }),
    );
    emit(status(running(1, 99, stamps[3]!)));
    expect(jobs.items.value[0]?.status).toBe('succeeded');
    expect(jobs.items.value[0]?.attempts).toBe(2);
    expect(jobs.processingJobs.value).toEqual([]);
  });

  test('fetches job details', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    responseDetail = {
      id: 1,
      attempts: [
        {
          id: 4,
          log_data: 'full log',
          input_probe: { raw: { retained: true } },
        },
      ],
      events: [{ id: 8, payload: { retained: true } }],
      extra: { retained: ['value'] },
    };
    expect(await useJobs().get(1, true)).toEqual(responseDetail);
    expect(calls).toEqual(['/api/jobs/1?events=1']);
  });

  test('list totals update', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const { useFiles } = await import('../app/composables/useFiles');
    const jobs = useJobs();
    const files = useFiles();
    await jobs.load(filters);
    await files.load(fileFilters);
    jobs.upsert(job(2, 0));
    files.upsert(file(2));
    expect(jobs.total.value).toBe(2);
    expect(files.total.value).toBe(2);
    jobs.drop(1);
    files.drop(1);
    expect(jobs.total.value).toBe(1);
    expect(files.total.value).toBe(1);
    jobs.drop(999);
    files.drop(999);
    expect(jobs.total.value).toBe(1);
    expect(files.total.value).toBe(1);
  });

  test('search changes become snapshots', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const { useFiles } = await import('../app/composables/useFiles');
    const jobs = useJobs();
    const files = useFiles();
    await jobs.load({ ...filters, query: 'file' });
    await files.load({ ...fileFilters, query: 'file' });
    calls.length = 0;
    jobs.upsert({ ...job(1, 0), file: { ...file(1), path: 'other.mkv' } });
    files.upsert(file(1, 'other.mkv'));
    jobs.upsert(job(2, 0));
    files.upsert(file(2));
    expect(jobs.items.value).toEqual([]);
    expect(files.items.value).toEqual([]);
    expect(jobs.total.value).toBe(1);
    expect(files.total.value).toBe(1);
    expect(calls).toEqual([]);
    await jobs.load(filters);
    await files.load(fileFilters);
    calls.length = 0;
    reconnects.forEach((callback) => callback());
    expect(calls).toEqual([]);
  });

  test('partial pages keep snapshots', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const { useFiles } = await import('../app/composables/useFiles');
    const jobs = useJobs();
    const files = useFiles();
    await jobs.load({ ...filters, limit: 1 });
    await files.load({ ...fileFilters, limit: 1 });
    jobs.upsert(job(2, 0));
    files.upsert(file(2, 'a.mkv'));
    expect(jobs.total.value).toBe(2);
    expect(files.total.value).toBe(2);
    expect(jobs.items.value.map((row) => row.id)).toEqual([2]);
    expect(files.items.value.map((row) => row.id)).toEqual([2]);
    jobs.upsert(job(3, 0));
    files.upsert(file(3, 'b.mkv'));
    expect(jobs.total.value).toBe(2);
    expect(files.total.value).toBe(2);
    jobs.drop(3);
    files.drop(3);
    expect(jobs.items.value).toEqual([]);
    expect(files.items.value).toEqual([]);
    expect(jobs.total.value).toBe(1);
    expect(files.total.value).toBe(1);
  });

  test('file statuses combine', async () => {
    const { useFiles } = await import('../app/composables/useFiles');
    const files = useFiles();
    responseFiles = [
      file(1),
      { ...file(2), transcode_status: 'failed', health_status: 'corrupt' },
      { ...file(3), transcode_status: 'cancelled', health_status: 'pending' },
      { ...file(4), transcode_status: 'transcoded', health_status: 'corrupt' },
      { ...file(5), transcode_status: 'failed', health_status: 'pending' },
      { ...file(6), library_id: 2, transcode_status: 'failed' },
      { ...file(7), transcode_status: 'failed' },
    ];
    const view = {
      ...fileFilters,
      library_id: '1',
      transcode_status: 'pending,failed',
      health_status: 'healthy,corrupt',
      sort_dir: 'asc' as const,
    };
    await files.load(view);
    const params = new URL(calls.at(-1)!, 'http://test').searchParams;
    expect(params.get('transcode_status')).toBe('pending,failed');
    expect(params.get('health_status')).toBe('healthy,corrupt');
    expect(params.get('library_id')).toBe('1');
    expect(files.items.value.map((row) => row.id)).toEqual([1, 2, 7]);
    for (const overlap of [false, true]) {
      await files.load(view);
      holdRequest = overlap;
      const pending = overlap ? files.load(view) : Promise.resolve();
      try {
        files.upsert({ ...file(1), transcode_status: 'cancelled' });
        files.upsert({
          ...file(3),
          transcode_status: 'failed',
          health_status: 'corrupt',
        });
        files.upsert({
          ...file(7),
          transcode_status: 'failed',
          health_status: 'pending',
        });
        files.upsert({
          ...file(2),
          library_id: 2,
          transcode_status: 'failed',
          health_status: 'corrupt',
        });
        expect(files.items.value.map((row) => row.id)).toEqual([3]);
        releaseAll();
        await pending;
        expect(files.items.value.map((row) => row.id)).toEqual([3]);
      } finally {
        releaseAll();
        await pending;
        holdRequest = false;
      }
    }

    await files.load({ ...view, transcode_status: undefined });
    expect(files.items.value.map((row) => row.id)).toEqual([1, 2, 4, 7]);
    expect(new URL(calls.at(-1)!, 'http://test').searchParams.has('transcode_status')).toBe(false);
    await files.load({ ...view, health_status: undefined });
    expect(files.items.value.map((row) => row.id)).toEqual([1, 2, 5, 7]);
    expect(new URL(calls.at(-1)!, 'http://test').searchParams.has('health_status')).toBe(false);
    await files.load({
      ...view,
      transcode_status: undefined,
      health_status: undefined,
    });
    expect(files.items.value.map((row) => row.id)).toEqual([1, 2, 3, 4, 5, 7]);
  });

  test('multiple statuses filter', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJobs = [
      job(1, 0),
      { ...job(2, 0), status: 'failed' },
      { ...job(3, 0), status: 'running' },
      { ...job(4, 0), status: 'cancelled' },
    ];
    const view = {
      ...filters,
      status: 'queued,failed',
      sort_by: 'id' as const,
      sort_dir: 'asc' as const,
    };
    await jobs.load(view);
    expect(new URL(calls.at(-1)!, 'http://test').searchParams.get('status')).toBe('queued,failed');
    expect(jobs.items.value.map((row) => row.id)).toEqual([1, 2]);
    jobs.upsert({ ...job(1, 0), status: 'running', updated_at: stamps[1]! });
    jobs.upsert({ ...job(3, 0), status: 'failed', updated_at: stamps[1]! });
    expect(jobs.items.value.map((row) => row.id)).toEqual([2, 3]);
    await jobs.load({ ...view, status: '' });
    expect(new URL(calls.at(-1)!, 'http://test').searchParams.get('status')).toBe('');
    expect(jobs.items.value.map((row) => row.id)).toEqual([1, 2, 3, 4]);
  });

  test('updates preserve view choices', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    const choices = {
      ...filters,
      status: 'queued',
      kind: 'transcode',
      sort_by: 'priority' as const,
      sort_dir: 'asc' as const,
    };
    await jobs.load(choices);
    jobs.upsert({ ...job(2, 0), priority: 2 });
    jobs.upsert({ ...job(1, 0), priority: 3 });
    expect(jobs.items.value.map((row) => row.id)).toEqual([2, 1]);
    jobs.upsert({ ...job(2, 0), priority: 2, kind: 'healthcheck' });
    expect(jobs.items.value.map((row) => row.id)).toEqual([1]);
    await jobs.load({ ...choices, offset: 50 });
    jobs.upsert(job(2, 0));
    expect(jobs.items.value.map((row) => row.id)).toEqual([1]);
    expect(new URL(calls.at(-1)!, 'http://test').searchParams.get('offset')).toBe('50');
    expect(calls).toHaveLength(2);
  });

  test('overlaps preserve live views', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJob = running(1, 0, stamps[0]!);
    const view = { ...filters, status: 'running' };
    await jobs.load(view);
    await jobs.loadProcessing();
    holdRequest = true;
    const pending = Promise.all([jobs.load(view), jobs.loadProcessing()]);
    try {
      emit(status({ ...running(1, 0, stamps[1]!), status: 'queued' }));
      expect(jobs.items.value).toEqual([]);
      emit(status(running(1, 0, stamps[1]!)));
      emit(progress(1, 99, stamps[2]!));
      releaseAll();
      await pending;
      expect(jobs.items.value[0]?.progress?.percent).toBe(99);
      expect(jobs.processingJobs.value[0]?.progress?.percent).toBe(99);
    } finally {
      releaseAll();
      await pending;
    }
  });

  test('changed filters accept snapshots', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJob = running(1, 0, stamps[0]!);
    await jobs.load(filters);
    holdRequest = true;
    const pending = jobs.load({ ...filters, status: 'running' });
    try {
      emit(progress(1, 99, stamps[2]!));
      releaseAll();
      expect(await pending).toBe(true);
      expect(jobs.items.value[0]?.progress?.percent).toBe(0);
    } finally {
      releaseAll();
      await pending;
    }
  });

  test('missed updates invalidate loads', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    for (const reconnect of [false, true]) {
      holdRequest = false;
      responseJob = running(1, 0, stamps[0]!);
      await jobs.load(filters);
      await jobs.loadProcessing();
      responseJob = running(1, 50, stamps[2]!);
      holdRequest = true;
      const pending = Promise.all([jobs.load(filters), jobs.loadProcessing()]);
      try {
        if (reconnect) reconnects.forEach((callback) => callback());
        else emit({ type: 'events.resync', payload: { reason: 'overflow' } });
        releaseAll();
        const result = await pending;
        expect(result[0]).toBe(false);
        expect(jobs.items.value[0]?.progress?.percent).toBe(0);
        expect(jobs.processingJobs.value[0]?.progress?.percent).toBe(0);
      } finally {
        releaseAll();
        await pending;
      }
    }
  });

  test('updates respect attempt boundaries', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJob = running(1, 0, stamps[0]!);
    await jobs.load(filters);
    await jobs.loadProcessing();
    calls.length = 0;
    emit(progress(1, 99, stamps[2]!));
    emit(status(running(1, 10, stamps[1]!)));
    expect(jobs.items.value[0]?.progress?.percent).toBe(99);
    expect(jobs.processingJobs.value[0]?.progress?.percent).toBe(99);
    emit(status(running(2, 0, stamps[1]!)));
    emit(progress(1, 99, stamps[3]!));
    for (const row of [jobs.items.value[0], jobs.processingJobs.value[0]]) {
      expect(row?.attempts).toBe(2);
      expect(row?.progress?.percent).toBe(0);
    }
    emit(progress(2, 5, stamps[3]!));
    emit(progress(2, 99, stamps[2]!));
    expect(jobs.items.value[0]?.progress?.percent).toBe(5);
    emit(
      status({
        ...running(2, 0, stamps[2]!),
        status: 'succeeded',
        progress: null,
      }),
    );
    emit(progress(2, 99, stamps[3]!));
    emit(status(running(1, 99, stamps[3]!)));
    expect(jobs.items.value[0]?.status).toBe('succeeded');
    expect(jobs.items.value[0]?.progress).toBeNull();
    expect(jobs.processingJobs.value).toEqual([]);
    const queued = {
      ...running(2, 0, stamps[3]!),
      status: 'queued' as const,
      progress: null,
    };
    queued.latest_attempt = { ...queued.latest_attempt!, status: 'succeeded' };
    emit(status(queued));
    emit(progress(2, 99, '2026-01-01T00:00:00.4Z'));
    expect(jobs.items.value[0]?.progress).toBeNull();
    expect(jobs.processingJobs.value).toEqual([]);
    expect(calls).toEqual([]);
  });

  test('late responses exclude deletions', async () => {
    const { useJobs } = await import('../app/composables/useJobs');
    const jobs = useJobs();
    responseJob = running(1, 0, stamps[0]!);
    await jobs.load(filters);
    await jobs.loadProcessing();
    holdRequest = true;
    calls.length = 0;
    const pending = Promise.all([jobs.load(filters), jobs.loadProcessing()]);
    try {
      emit({ type: 'job.deleted', payload: { job_id: 1 } });
      releaseAll();
      await pending;
      expect(jobs.items.value).toEqual([]);
      expect(jobs.processingJobs.value).toEqual([]);
      expect(calls).toHaveLength(2);
      holdRequest = false;
      await jobs.load(filters);
      await jobs.loadProcessing();
      expect(jobs.items.value[0]?.id).toBe(1);
      expect(jobs.processingJobs.value[0]?.id).toBe(1);
    } finally {
      releaseAll();
      await pending;
    }
  });
});
