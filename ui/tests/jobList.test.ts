import { describe, expect, test } from 'bun:test';
import type { BulkJobAction } from '../app/composables/useJobs';
import type { JobRow, JobStatus } from '../app/types/api';
import { statusOptions } from '../app/utils/status';
import {
  JOB_FILTER_ALL,
  canJobAction,
  jobActionIds,
  jobReason,
  formatReason,
  jobRouteFilters,
  jobStatusOptions,
  dashboardJobStatusOptions,
  remainingJobSelection,
  updateJobRoute,
} from '../app/utils/jobList';

describe('Job filters', () => {
  test('defaults to all', () => {
    expect(jobRouteFilters({})).toEqual({});
    expect(jobRouteFilters({ status: '' })).toEqual({});
    expect(updateJobRoute({ status: 'queued', offset: '50' }, { status: '' })).toEqual({});
  });

  test('keeps exact statuses', () => {
    for (const { value } of statusOptions('job').slice(1)) {
      expect(jobRouteFilters({ status: value })).toEqual({ status: value });
    }
  });

  test('dashboard filters statuses', () => {
    const status = 'queued,running,failed,pending,queued';
    expect(jobRouteFilters({ status }, dashboardJobStatusOptions)).toEqual({
      status: 'queued,failed,pending',
    });
    expect(
      updateJobRoute({ offset: '50', job: '42' }, { status }, dashboardJobStatusOptions),
    ).toEqual({ status: 'queued,failed,pending', job: '42' });
    expect(
      updateJobRoute({ status: 'queued', offset: '50' }, { status: '' }, dashboardJobStatusOptions),
    ).toEqual({});
  });

  test('keeps multiple statuses', () => {
    for (const status of [
      'staging,queued',
      'claimed,running,verifying,finalizing',
      'queued,running',
      'failed,cancelled',
    ]) {
      expect(jobRouteFilters({ status, kind: 'transcode' })).toEqual({ status, kind: 'transcode' });
      expect(updateJobRoute({ status: 'hold', offset: '100', job: '42' }, { status })).toEqual({
        status,
        job: '42',
      });
    }
  });

  test('normalizes status selections', () => {
    expect(jobRouteFilters({ status: ' failed,queued,failed,unknown,, cancelled ' })).toEqual({
      status: 'failed,queued,cancelled',
    });
    const status = jobStatusOptions.map((option) => option.value).join(',');
    expect(jobRouteFilters({ status })).toEqual({ status });
    expect(updateJobRoute({ status }, { status: '' })).toEqual({});
  });

  test('normalizes saved filters', () => {
    expect(
      jobRouteFilters({
        status: 'hold',
        query: '  movies/title.mkv  ',
        kind: 'healthcheck',
        offset: '000100',
        limit: '100',
        sort_by: 'priority',
        sort_dir: 'asc',
        job: '42',
      }),
    ).toEqual({
      status: 'hold',
      query: 'movies/title.mkv',
      kind: 'healthcheck',
      offset: '100',
      limit: '100',
      sort_by: 'priority',
      sort_dir: 'asc',
    });
  });

  test('validates page sizes', () => {
    for (const limit of ['50', '100', '200', '300', '500', '750', '999']) {
      expect(jobRouteFilters({ limit })).toEqual(limit === '50' ? {} : { limit });
    }
    for (const limit of ['0', '-50', '25', '400', '1000', '100.0', '0100', 'invalid', ['100']]) {
      expect(jobRouteFilters({ limit })).toEqual({});
    }
  });

  test('removes invalid filters', () => {
    for (const status of ['queue', 'processing', 'unknown', ', ,', JOB_FILTER_ALL, ['queued']]) {
      expect(jobRouteFilters({ status })).toEqual({});
    }
    expect(
      jobRouteFilters({
        query: ' ',
        kind: 'invalid',
        offset: '-1',
        sort_by: 'invalid',
        sort_dir: 'up',
      }),
    ).toEqual({});
    expect(jobRouteFilters({ offset: '000' })).toEqual({});
  });

  test('resets changed pagination', () => {
    const changes = [
      { status: 'running' },
      { status: 'failed,cancelled' },
      { query: 'movie' },
      { kind: 'healthcheck' },
      { sort_by: 'path' },
      { sort_dir: 'asc' },
      { limit: '200' },
      { limit: '999' },
    ];
    for (const change of changes) {
      expect(updateJobRoute({ offset: '100', job: '42' }, change)).toEqual({
        job: '42',
        ...change,
      });
    }
    expect(
      updateJobRoute({ status: 'queued', job: '42', limit: '999' }, { offset: '999' }),
    ).toEqual({
      status: 'queued',
      job: '42',
      limit: '999',
      offset: '999',
    });
  });

  test('normalization keeps detail', () => {
    expect(
      updateJobRoute({ status: 'queued', offset: '00050', limit: '50', job: '42' }, {}),
    ).toEqual({
      status: 'queued',
      offset: '50',
      job: '42',
    });
  });
});

describe('Job reason', () => {
  test('groups recorded removals', () => {
    const reason = [
      'Change container from MP4 to MKV.',
      'Drop audio stream 4 (spa, aac): language not in keep list.',
      'Drop audio stream 5 (eng, aac): descriptive track.',
      'Drop subtitle stream 7 (fre, dvd_subtitle): language not in keep list.',
      'Drop audio stream 6 (ger, ac3): language not in keep list.',
      'Drop subtitle stream 8 (ger, dvd_subtitle): language not in keep list.',
      'Drop audio stream 9 (fre, aac): language not in keep list.',
    ].join(' ');
    const job = {
      kind: 'transcode' as const,
      status: 'running' as const,
      decision_log: [{ node: 'cleanup', outcome: 'plan', reason }],
    };
    expect(jobReason(job)).toBe(reason);
    expect(formatReason(reason)).toBe(
      [
        'Change container from MP4 to MKV.',
        'Dropping audio streams (4:spa, 9:fre) [aac]: language not in keep list.',
        'Dropping audio stream (5, eng, aac): descriptive track.',
        'Dropping subtitle streams (7:fre, 8:ger) [dvd_subtitle]: language not in keep list.',
        'Dropping audio stream (6, ger, ac3): language not in keep list.',
      ].join('\n'),
    );
    expect(job.decision_log[0]?.reason).toBe(reason);
  });

  test('separates stream changes', () => {
    const reason = [
      'Transcode audio stream 1 (eng, dts): encode aac 192k.',
      'Transcode audio stream 2 (fre, ac3): encode aac 384k.',
      'Transcode subtitle stream 3 (eng, subrip): encode mov_text.',
      'Retag audio stream 4 (und, aac): language=eng, title=2.0.',
      'Transcode video stream 0 (und, h264): encode hevc_nvenc, crf=23, preset=slow.',
      'Transcode audio stream 5 (ita, dts): encode aac 192k.',
      'Encode video to HEVC.',
    ].join(' ');
    expect(formatReason(reason)).toBe(
      [
        'Transcoding audio streams (1:eng, 5:ita) [dts]: encode aac 192k.',
        'Transcoding audio stream (2, fre, ac3): encode aac 384k.',
        'Transcoding subtitle stream (3, eng, subrip): encode mov_text.',
        'Retagging audio stream (4, und, aac): language=eng, title=2.0.',
        'Transcoding video stream (0, und, h264): encode hevc_nvenc, crf=23, preset=slow.',
        'Encode video to HEVC.',
      ].join('\n'),
    );
  });

  test('retains every stream', () => {
    const streams = Array.from({ length: 60 }, (_, index) => ({
      index: String(index + 6),
      language: ['bul', 'eng', 'fre'][index % 3]!,
      codec: index % 2 ? 'subrip' : 'dvd_subtitle',
    }));
    const reason = streams
      .map(
        (stream) =>
          `Drop subtitle stream ${stream.index} (${stream.language}, ${stream.codec}): language not in keep list.`,
      )
      .join(' ');
    expect(formatReason(reason)).toBe(
      ['dvd_subtitle', 'subrip']
        .map(
          (codec) =>
            `Dropping subtitle streams (${streams
              .filter((stream) => stream.codec === codec)
              .map((stream) => `${stream.index}:${stream.language}`)
              .join(', ')}) [${codec}]: language not in keep list.`,
        )
        .join('\n'),
    );
  });

  test('keeps recorded text', () => {
    const reason = '  Keep metadata.\nContainer already matches.  ';
    expect(formatReason(reason)).toBe(reason);
    expect(
      formatReason(
        'Keep metadata. Drop audio stream 1 (eng, aac): duplicate track. Keep chapters. Drop audio stream 2 (eng, aac): duplicate track. Change container from MP4 to MKV.',
      ),
    ).toBe(
      [
        'Keep metadata.',
        'Dropping audio streams (1:eng, 2:eng) [aac]: duplicate track.',
        'Keep chapters.',
        'Change container from MP4 to MKV.',
      ].join('\n'),
    );
  });

  test('guards job kind', () => {
    const reason = 'Drop subtitle stream 6 (bul, subrip): language not in keep list.';
    const decision_log = [{ node: 'cleanup', outcome: 'plan', reason }];
    const recorded = structuredClone(decision_log);
    for (const kind of ['healthcheck', 'probe', 'mkv_stats'] satisfies JobRow['kind'][]) {
      expect(jobReason({ kind, status: 'running', decision_log })).toBeNull();
    }
    expect(jobReason({ kind: 'transcode', status: 'running', decision_log })).toBe(reason);
    expect(decision_log).toEqual(recorded);
  });

  test('retains planned stages', () => {
    expect(
      jobReason({
        kind: 'transcode',
        status: 'running',
        decision_log: [
          { node: 'cleanup', outcome: 'plan', reason: 'Remove subtitle stream 3 (ger).' },
          { node: 'encode', outcome: 'plan', reason: 'Encode video to HEVC.' },
          { node: 'cleanup-again', outcome: 'skip', reason: 'Streams already match.' },
          { node: 'done', outcome: 'done' },
        ],
      }),
    ).toBe('Remove subtitle stream 3 (ger).\nEncode video to HEVC.');
  });

  test('ignores absent reasons', () => {
    expect(
      jobReason({
        kind: 'transcode',
        status: 'running',
        decision_log: [
          { node: 'cleanup', outcome: 'skip', reason: 'Streams already match.' },
          { node: 'encode', outcome: 'plan' },
        ],
      }),
    ).toBeNull();
    expect(jobReason({ kind: 'transcode', status: 'queued', decision_log: null })).toBeNull();
  });

  test('pending hides previous', () => {
    expect(
      jobReason({
        kind: 'transcode',
        status: 'pending',
        decision_log: [
          { node: 'cleanup', outcome: 'plan', reason: 'Remove subtitle stream 3 (ger).' },
        ],
      }),
    ).toBeNull();
  });
});

describe('Job actions', () => {
  const statuses: JobStatus[] = [
    'staging',
    'hold',
    'queued',
    'claimed',
    'running',
    'verifying',
    'finalizing',
    'succeeded',
    'skipped',
    'failed',
    'cancelled',
    'pending',
  ];
  const jobs = statuses.map((status, index) => ({ id: index + 1, status }));
  const selected = new Set(jobs.map((job) => job.id));
  const eligible: Record<BulkJobAction, JobStatus[]> = {
    bump: ['staging', 'queued'],
    cancel: [
      'staging',
      'hold',
      'queued',
      'claimed',
      'running',
      'verifying',
      'finalizing',
      'pending',
    ],
    retry: ['succeeded', 'failed', 'cancelled'],
    delete: ['staging', 'hold', 'queued', 'succeeded', 'skipped', 'failed', 'cancelled'],
  };

  test('matches card eligibility', () => {
    for (const action of ['bump', 'cancel', 'retry', 'delete'] as const) {
      expect(statuses.filter((status) => canJobAction(status, action))).toEqual(eligible[action]);
    }
  });

  test('selects eligible IDs', () => {
    for (const action of ['bump', 'cancel', 'retry', 'delete'] as const) {
      expect(jobActionIds(jobs, selected, action)).toEqual(
        jobs.filter((job) => eligible[action].includes(job.status)).map((job) => job.id),
      );
    }
    expect(jobActionIds(jobs, new Set([1, 2, 8]), 'bump')).toEqual([1]);
    expect(jobActionIds(jobs, new Set([2, 8]), 'cancel')).toEqual([2]);
    expect(jobActionIds(jobs, new Set([1, 2]), 'retry')).toEqual([]);
    expect(jobActionIds(jobs, new Set(), 'delete')).toEqual([]);
  });

  test('retains failed exclusions', () => {
    const mixed = new Set([1, 2, 8]);
    const attempted = jobActionIds(jobs, mixed, 'bump');
    expect(attempted).toEqual([1]);
    expect(remainingJobSelection(mixed, attempted)).toEqual(new Set([2, 8]));
    expect(remainingJobSelection(new Set([1, 3, 8]), [1])).toEqual(new Set([3, 8]));
    expect(remainingJobSelection(mixed, [])).toEqual(mixed);
    expect(mixed).toEqual(new Set([1, 2, 8]));
  });
});
