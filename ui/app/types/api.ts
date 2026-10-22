export type FileJobKind = 'healthcheck' | 'transcode';
export type BulkFileAction = FileJobKind | 'skip_cooldown';

export type FileJobAccepted = {
  job_id: number;
  kind: FileJobKind | 'probe';
  status: 'pending' | 'queued';
};

export type BulkFileResult<
  Accepted extends { file_id: number } = FileJobAccepted & { file_id: number },
> = {
  accepted: Accepted[];
  failed: Array<{ file_id: number; status: number; detail: string }>;
};

export type WorkspaceRow = {
  workspace_id: number;
  job_id: number | null;
  file_id: number | null;
  filename: string | null;
  status: string | null;
  node_id: number | null;
  node_name: string | null;
  attempt_number: number | null;
  progress: Record<string, unknown> | null;
  size_bytes: number | null;
  file_count: number | null;
  source_bytes: number | null;
  output_bytes: number | null;
  last_activity_at: string | null;
  eligible: boolean;
  reason: string;
  error: string | null;
};

export type WorkspaceSnapshot = {
  summary: {
    count: number | null;
    active_count: number | null;
    unused_count: number | null;
    unused_size_bytes: number | null;
    protected_count: number | null;
    size_bytes: number | null;
    free_bytes: number | null;
  };
  sampled_at: string;
  error: string | null;
};

export type WorkspaceList = WorkspaceSnapshot & {
  items: WorkspaceDetail[];
  total: number;
};

export type WorkspaceDetail = WorkspaceRow & {
  sampled_at: string;
  files: Array<{ path: string; size_bytes: number; modified_at: string }>;
  attempts: Array<{
    id: number;
    attempt_number: number;
    status: string;
    result: string | null;
    node_name: string | null;
  }>;
};

export type WorkspacePruneResult = {
  items: Array<{
    workspace_id: number;
    outcome: 'removed' | 'skipped' | 'error';
    reason: string | null;
  }>;
  removed: number;
  skipped: number;
  errors: number;
  snapshot: WorkspaceSnapshot;
};

export type JobStatus =
  | 'pending'
  | 'staging'
  | 'hold'
  | 'queued'
  | 'claimed'
  | 'running'
  | 'verifying'
  | 'finalizing'
  | 'succeeded'
  | 'skipped'
  | 'failed'
  | 'cancelled';

export type JobRow = {
  id: number;
  kind: string;
  status: JobStatus;
  priority: number;
  attempts: number;
  error: string | null;
  decision_log: FlowNodeResult[] | null;
  warning?: string | null;
  queue_state?: 'ready' | 'staging' | 'blocked';
  queue_reason?: string | null;
  hold_until?: string | null;
  cooldown_started_at: string | null;
  media_file_id: number | null;
  file: JobFile | null;
  library_id: number | null;
  node_id: number | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  progress?: Record<string, unknown> | null;
  node_name: string | null;
  latest_attempt: JobAttemptRow | null;
};

export type SavingsPoint = { date: string; files: number; saved_bytes: number };
export type AcquisitionPoint = {
  date: string;
  files: number;
  bytes: number;
  current_size_files: number;
};

export type ProcessingKind = 'transcode' | 'remux' | 'unclassified';

export type SavingsBreakdown = {
  series: SavingsPoint[];
  totals: {
    files: number;
    saved_bytes: number;
    source_bytes: number;
    avg_reduction_pct: number;
  };
};

export type SavingsResponse = SavingsBreakdown & {
  days: number;
  by_kind: Record<ProcessingKind, SavingsBreakdown>;
};

export type JobAttemptRow = {
  id: number;
  job_id: number;
  attempt_number: number;
  node_id: number | null;
  node_name: string | null;
  capabilities: Record<string, unknown> | null;
  workload: string | null;
  status: string;
  result: string | null;
  error: string | null;
  source_path: string | null;
  output_path: string | null;
  log_path: string | null;
  argv: string[] | null;
  argv_hash: string | null;
  source_size: number | null;
  output_size: number | null;
  health: Record<string, unknown> | null;
  action_results: ActionResult[] | null;
  claimed_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  input_probe: ProbeData | null;
  output_probe: ProbeData | null;
};

export type JobFile = {
  id: number;
  library_id: number;
  path: string;
  size: number;
  container: string | null;
  video_codec: string | null;
  video_fps?: number | null;
  video_bit_rate?: number | null;
  audio_codec?: string | null;
  audio_channels?: number | null;
  audio_channel_layout?: string | null;
  audio_bit_rate?: number | null;
  width: number | null;
  height: number | null;
  duration_s: number | null;
};

export type JobEventRow = {
  id: number;
  attempt_id?: number | null;
  ts: string;
  type: string;
  payload: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

export type JobAttemptDetail = JobAttemptRow & { log_data: string | null };

export type JobDetail = Omit<JobRow, 'attempts' | 'latest_attempt' | 'node_name'> & {
  attempt_count: number;
  attempts: JobAttemptDetail[];
  events: JobEventRow[];
  notification_deliveries: {
    id: number;
    job_event_id: number;
    target_name: string;
    status: 'pending' | 'sent' | 'failed';
    attempts: number;
    error: string | null;
  }[];
  flow_snapshot: { id: number; name: string; version: number; definition: FlowDefinition } | null;
  file_probe: ProbeData | null;
  plan: Record<string, unknown> | null;
  continuation: Record<string, unknown> | null;
  facets: Record<string, unknown> | null;
  pre_actions: Record<string, unknown>[] | null;
  post_actions: Record<string, unknown>[] | null;
  requires: string[] | null;
  requires_encoders: string[] | null;
  lease_expires_at: string | null;
  stderr_tail: string | null;
};

export type ActionResult = {
  kind: string;
  ok: boolean;
  error?: string | null;
  artifacts?: string[];
};

export type MediaFileRow = {
  id: number;
  library_id: number;
  path: string;
  size: number;
  container: string | null;
  video_codec: string | null;
  width: number | null;
  height: number | null;
  duration_s: number | null;
  bit_rate: number | null;
  medium: string | null;
  modified_at: string;
  cooldown_until: string | null;
  cooldown_started_at: string | null;
  created_at: string;
  updated_at: string;
  transcode_status:
    | 'pending'
    | 'not_needed'
    | 'transcoded'
    | 'failed'
    | 'cancelled'
    | 'rejected'
    | Exclude<JobStatus, 'succeeded' | 'failed' | 'cancelled' | 'skipped'>;
  health_status: string;
  active_job_kind: string | null;
  active_job_status: string | null;
  active_job_id: number | null;
  active_job_progress: Record<string, unknown> | null;
  active_job_updated_at: string | null;
  last_change?: LastFileChange | null;
};

export type FileChangeStats = {
  size: number | null;
  duration_s: number | null;
  bit_rate: number | null;
  video_codec: string | null;
  width: number | null;
  height: number | null;
  audio: number | null;
  subtitle: number | null;
};

export type LastFileChange = {
  job_id: number;
  attempt_number: number;
  before: FileChangeStats;
  after: FileChangeStats;
};

export type ProbeStream = {
  index: number;
  codec: string | null;
  codec_type: string | null;
  language: string | null;
  title: string | null;
  filename?: string | null;
  mimetype?: string | null;
  channels: number | null;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  pix_fmt?: string | null;
  profile?: string | null;
  sample_rate?: number | null;
  channel_layout?: string | null;
  bit_rate?: number | null;
  default?: boolean;
  forced?: boolean;
  disposition_default?: boolean;
  disposition_forced?: boolean;
  disposition_commentary?: boolean;
  disposition_attached_pic?: boolean;
};

export type ProbeSummary = {
  file_path?: string;
  container: string | null;
  duration_s: number | null;
  size_bytes: number | null;
  streams: ProbeStream[];
  raw?: Record<string, unknown> | null;
} | null;
export type ProbeData = ProbeSummary;

export type FileMediaSnapshot = {
  id: string;
  job_id: number;
  attempt_number: number;
  stage_number: number | null;
  kind: 'original' | 'stage' | 'result';
  created_at: string;
  probe: NonNullable<ProbeSummary>;
  result: string | null;
};

export type MediaFileDetail = MediaFileRow & {
  probe: ProbeSummary;
  media_snapshots: FileMediaSnapshot[];
  jobs: Array<{
    id: number;
    kind: string;
    status: string;
    error: string | null;
    created_at: string;
  }>;
  last_transcode?: {
    job_id: number;
    attempt_number: number;
    source_size: number | null;
    output_size: number | null;
    input_probe: ProbeSummary;
    output_probe: ProbeSummary;
  } | null;
};

export type ScheduleSpec = { type: 'grid'; cells: boolean[][] };
export type TrashMode = 'none' | 'transcode' | 'all';

export type LibraryProgressStatus =
  | 'idle'
  | 'queued'
  | 'scanning'
  | 'completed'
  | 'failed'
  | 'paused'
  | 'cancelling'
  | 'cancelled';

export type ScannerState = { paused: boolean; failed: boolean };
export type ProbeState = { failed: boolean };
export type LibraryProgressSnapshot = {
  scanner: ScannerState;
  probing: ProbeState;
  libraries: LibraryProgress[];
};

export type LibraryProgress = {
  library_id: number;
  scanner: { paused: boolean; next_run_at: string | null; discovery_pending: boolean };
  discovery: {
    status: LibraryProgressStatus;
    found: number;
    added: number;
    updated: number;
    error_count: number;
    error: string | null;
    reason: string | null;
    latest_result: LibraryProgressStatus | null;
    finished_at: string | null;
  };
  probing: {
    status: LibraryProgressStatus;
    processed: number;
    remaining: number;
    error_count: number;
    active: {
      job_id: number;
      media_file_id: number;
      path: string;
      tool: 'waiting' | 'ffprobe' | 'mkvpropedit';
      started_at: string;
      percent: number | null;
    }[];
  };
};

export type ProcessingCutoffField = 'created_at' | 'first_seen' | 'mtime';
export type ProcessingCutoffMatch = 'all' | 'any';
export type ProcessingCutoff = {
  at?: string;
  fields?: ProcessingCutoffField[];
  match?: ProcessingCutoffMatch;
};

export type LibraryRow = {
  deleted: boolean;
  delete_error: string | null;
  suspended: boolean;
  id: number;
  created_at: string;
  updated_at: string;
  name: string;
  roots: string[];
  cooldown_s: number;
  schedule: ScheduleSpec | null;
  watch: boolean;
  scan_on_start: boolean;
  scan_interval_s: number | null;
  mkv_stats: boolean;
  healthcheck_enabled: boolean;
  processing_cutoff: ProcessingCutoff;
  trash_mode: TrashMode;
  trash_retention_days: number;
  flow_id: number | null;
};

export type LibraryBody = {
  name: string;
  roots: string[];
  cooldown_s: number;
  schedule: ScheduleSpec | null;
  watch: boolean;
  scan_on_start: boolean;
  scan_interval_s: number | null;
  mkv_stats: boolean;
  healthcheck_enabled: boolean;
  processing_cutoff: ProcessingCutoff;
  trash_mode: TrashMode;
  trash_retention_days: number;
  flow_id: number | null;
};

export type TrashEntry = {
  id: string;
  created_at: string;
  updated_at: string;
  library_id: number;
  current_file_id: number | null;
  original_path: string;
  suffix: string;
  size_bytes: number;
  mtime_ns: number;
  trashed_at: string;
  purge_at: string;
};

export type WebhookType = 'sonarr' | 'radarr' | 'generic';
export type WebhookRegistration = {
  name: string;
  type: WebhookType;
  api_key: string;
  path_replacements: { source: string; destination: string }[];
  created_at: string;
  updated_at: string;
};

export type ExecutionLocation = 'local' | 'node' | 'node_local';

export type OperationalSettings = {
  max_healthchecks: number;
  max_probes: number;
  max_mkv_stats: number;
  healthcheck_hwaccel: boolean;
  healthcheck_location: ExecutionLocation;
  mkv_stats_location: ExecutionLocation;
  min_free_gb: number;
  job_retention_days: number;
  session_ttl_days: number;
  scanner: { containers: string[]; ignore_dirs: string[] };
  playback: {
    segment_duration_s: number;
    max_concurrent_segments: number;
    grant_ttl_s: number;
    max_grants: number;
  };
};

export type BackupPreview = {
  flows: string[];
  libraries: { name: string; flow_name: string | null; roots: string[] }[];
  notifications: string[];
  webhooks: string[];
  plugins: string[];
  nodes: { label: string; value: string }[];
  config: boolean;
  settings: boolean;
};

export type DirectoryEntry = {
  name: string;
  path: string;
};

export type DirectoryListing = {
  path: string;
  items: DirectoryEntry[];
};

export type NodeUsage = {
  cpu_percent: number | null;
  memory_percent: number | null;
  gpus: { name: string; percent: number | null }[];
};

export type NodeCapabilities = {
  hostname?: string;
  max_transcodes?: number;
  max_healthchecks?: number;
  max_mkv_stats?: number;
  mkvpropedit?: boolean;
  ffmpeg_version?: string;
  encoders?: string[];
  video_encoders?: string[];
  audio_encoders?: string[];
  decoders?: string[];
  audio_decoders?: string[];
  hwaccels?: string[];
  worker_tags?: string[];
  cpu_model?: string;
  cpu_cores?: number;
  memory_gb?: number;
  gpus?: string[];
};

export type NodeRow = {
  id: number;
  created_at: string;
  updated_at: string;
  name: string;
  display_name: string;
  state: string;
  last_seen_at: string | null;
  capabilities: NodeCapabilities | null;
  max_transcodes: number;
  max_healthchecks: number;
  max_mkv_stats: number;
  active_jobs: number;
  active_transcodes: number;
  active_healthchecks: number;
  active_mkv_stats: number;
  active_job_ids: number[];
  paused: boolean;
  usage: NodeUsage | null;
  workspace_ready: boolean;
  workspace_free_bytes: number | null;
};

export type NodeChanges = Partial<
  Pick<NodeRow, 'display_name' | 'paused' | 'max_transcodes' | 'max_healthchecks' | 'max_mkv_stats'>
>;

export type StatsResponse = {
  queue_depth: {
    by_status: Record<string, number>;
    by_library: Record<string, number>;
  };
  backlog: {
    remaining_files: number;
    remaining_s: number | null;
    estimated_finish_at: string | null;
    reason: string | null;
  };
  jobs: {
    count: number;
    avg_duration_s: number;
  };
  space_saved_by_library: Record<string, number>;
  codec_distribution: Record<string, number>;
};

export type StatsOverview = {
  days: number;
  library_id: number | null;
  libraries: StatsLibraryRow[];
  inventory: {
    files: number;
    bytes: number;
    video_codecs: Record<string, number>;
    audio_codecs: Record<string, number>;
    subtitle_codecs: Record<string, number>;
    audio_languages: Record<string, number>;
    subtitle_languages: Record<string, number>;
    containers: Record<string, number>;
    resolutions: Record<string, number>;
    health_status: Record<string, number>;
    transcode_status: Record<string, number>;
  };
  jobs: {
    completed: number;
    succeeded: number;
    failed: number;
    cancelled: number;
    skipped: number;
    avg_duration_s: number;
    by_kind: Record<string, Record<string, number>>;
  };
  savings: SavingsResponse;
  acquisition: { series: AcquisitionPoint[] };
};

export type StatsLibraryRow = {
  id: number;
  name: string;
  files: number;
  bytes: number;
  successful_transcodes: number;
  successful_remuxes: number;
  successful_unclassified: number;
  transcode_saved_bytes: number;
  remux_saved_bytes: number;
  unclassified_saved_bytes: number;
  source_bytes: number;
  saved_bytes: number;
  avg_reduction_pct: number;
};

export type SystemInfo = {
  timezone: string;
  version: string;
  commit_sha: string;
  build_date: string;
  branch: string;
  protocol_version: string;
  python: string;
};

export type PluginKind = 'filter' | 'inspector' | 'transcode' | 'action';

export type PluginInputType =
  | 'int'
  | 'float'
  | 'bool'
  | 'str'
  | 'text'
  | 'password'
  | 'enum'
  | 'strings'
  | 'path_mappings';
export type PathMapping = { source: string; destination: string };
export type FlowFact = { key: string; type: string };
export type CodecOptions = {
  videoEncoders: string[];
  audioEncoders: string[];
  audioDecoders: string[];
};

export type PluginInputField = {
  required: boolean;
  nullable: boolean;
  name: string;
  type: PluginInputType;
  default: string | number | boolean | string[] | PathMapping[] | null;
  min: number | null;
  max: number | null;
  values: string[] | null;
  help: string | null;
};

export type PluginManifestInfo = {
  id: string;
  kind: PluginKind;
  name: string;
  description: string;
  author: string;
  url: string;
  help: string | null;
  version: string;
  config_version: number;
  migrations: {
    from_version: number;
    to_version: number;
    changes: (
      | {
          type: 'rename_plugin' | 'rename_action' | 'rename_event' | 'rename_target_prefix';
          from: string;
          to: string;
        }
      | {
          type: 'rename_field';
          scope?: 'inputs' | 'action' | 'event';
          from: string;
          to: string;
        }
    )[];
  }[];
  facets: Record<string, string>;
  requires: string[];
  require_plugins: string[];
};

export type PluginState = 'ready' | 'stopped' | 'disabled';

export type PluginSource = 'bundled' | 'uploaded';

export type PluginRow = {
  manifest: PluginManifestInfo;
  state: PluginState;
  source: PluginSource;
  inputs: PluginInputField[];
};

export type FacetValue = string | number | boolean | string[] | number[];

export type FacetOp = 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'contains' | 'not_contains';

export type FacetFilter = Partial<Record<FacetOp, FacetValue>>;

// Each object requires all field conditions to match. Literals test equality.
// FacetFilter applies comparison operators.
// Any/all groups can contain nested conditions.
export type WhenFacets =
  | Record<string, FacetValue | FacetFilter>
  | { any: WhenFacets[] }
  | { all: WhenFacets[] };

export type FlowNode =
  | {
      type: 'plugin';
      id: string;
      plugin: string;
      config_version?: number;
      inputs?: Record<string, unknown>;
    }
  | { type: 'condition'; id: string; when: WhenFacets }
  | { type: 'terminal'; id: string; outcome: 'done' | 'skip' | 'veto' };
export type FlowEdge = { src: string; port: string; dst: string };

export type FlowUiNodePos = { x: number; y: number };

// The server stores editor positions unchanged. Flow execution does not read them.
export type FlowUiHints = {
  nodes?: Record<string, FlowUiNodePos>;
};

export type FlowDefinition = {
  entry: string;
  nodes: FlowNode[];
  edges: FlowEdge[];
  actions?: unknown[];
  when?: WhenFacets | null;
  _ui?: FlowUiHints | null;
};

export type FlowRow = {
  id: number;
  created_at: string;
  name: string;
  version: number;
  definition: FlowDefinition;
  updated_at: string;
};

export type FlowPreset = {
  id: string;
  name: string;
  description: string;
  route: string;
  concepts: string[];
  definition: FlowDefinition;
};

export type FlowNodeResult = {
  node: string;
  step?: string;
  outcome: string;
  reason?: string;
  error?: string;
  facets?: Record<string, FacetValue>;
};

export type StreamLogEntry = {
  stream_index: number;
  codec: string | null;
  language: string | null;
  title: string | null;
  outcome: string;
  detail: string | null;
};

export type DryRunResult = {
  status: string;
  reason: string;
  steps: FlowNodeResult[];
  facets: Record<string, FacetValue>;
  plan: Record<string, unknown> | null;
  argv: string[] | null;
  stream_log: StreamLogEntry[];
};

export type BatchDryRunScope = { library: LibraryRow } | { files: MediaFileRow[] };

export type BatchDryRunRow = {
  file_id: number;
  root: string;
  file: string;
  status: 'matched' | 'deferred' | 'skipped' | 'vetoed' | 'error' | 'unavailable' | 'not_evaluated';
  reason: string;
  deferred_output: boolean;
  deferred_encoder: boolean;
};

export type BatchDryRunReport = {
  id: string;
  library_id: number | null;
  library_name: string;
  file_ids: number[] | null;
  flow_id: number;
  flow_name: string;
  flow_version: number;
  created_at: string;
  finished_at: string | null;
  expires_at: string | null;
  state: 'preparing' | 'running' | 'cancelling' | 'completed' | 'cancelled' | 'failed';
  error: string;
  processed: number;
  total: number;
  captured: number;
  snapshot_complete: boolean;
  counts: Partial<Record<BatchDryRunRow['status'], number>>;
  items: BatchDryRunRow[];
  pagination: { offset: number; limit: number; total: number };
};

export type FlowDecision = {
  status: string;
  reason: string;
  steps: FlowNodeResult[];
};

export const ACTIVE_JOB_STATUSES: JobStatus[] = [
  'pending',
  'staging',
  'hold',
  'queued',
  'claimed',
  'running',
  'verifying',
  'finalizing',
];
