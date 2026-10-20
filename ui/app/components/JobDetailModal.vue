<template>
  <UModal
    v-model:open="open"
    :title="jobTitle"
    :fullscreen="fullscreen"
    :ui="{
      content: fullscreen ? '' : 'w-[calc(100vw-1rem)] max-w-7xl',
      wrapper: 'min-w-0 flex-1',
      close: 'static',
      body: fullscreen ? 'min-h-0 overflow-y-auto' : 'max-h-[80vh] overflow-y-auto',
    }"
  >
    <template #actions>
      <UTooltip :text="fullscreen ? 'Exit fullscreen' : 'Fullscreen'">
        <UButton
          color="neutral"
          variant="ghost"
          size="sm"
          :icon="fullscreen ? 'i-lucide-minimize' : 'i-lucide-maximize'"
          :aria-label="fullscreen ? 'Exit fullscreen' : 'Fullscreen'"
          :aria-pressed="fullscreen"
          @click="fullscreen = !fullscreen"
          >{{ fullscreen ? 'Exit fullscreen' : 'Fullscreen' }}</UButton
        >
      </UTooltip>
    </template>
    <template #body>
      <div v-if="detail" class="space-y-4">
        <UAlert
          v-if="detailError"
          color="error"
          variant="soft"
          icon="i-lucide-circle-alert"
          title="Unable to refresh job"
          :description="detailError"
        />
        <section class="space-y-3">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="flex min-w-0 items-start gap-3">
              <JobProgressRing
                v-if="detail.status === 'hold' || detail.status === 'pending'"
                :status="detail.status"
                :percent="null"
                :cooldown-started-at="detail.cooldown_started_at"
                :cooldown-until="detail.hold_until"
                label="Job wait progress"
                :color="detail.kind === 'healthcheck' ? 'success' : 'primary'"
              />
              <div
                v-else-if="detailActive"
                class="relative size-12 shrink-0"
                role="progressbar"
                aria-label="Job progress"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-valuenow="detailProgress === null ? undefined : Math.round(detailProgress)"
                :aria-valuetext="
                  detailProgress === null
                    ? `${detailProgressLabel} progress`
                    : `${Math.round(detailProgress)}%`
                "
              >
                <svg class="size-full -rotate-90" viewBox="0 0 36 36" aria-hidden="true">
                  <circle
                    cx="18"
                    cy="18"
                    r="15.5"
                    fill="none"
                    stroke="var(--ui-border-accented)"
                    stroke-width="3.5"
                    pathLength="100"
                  />
                  <circle
                    cx="18"
                    cy="18"
                    r="15.5"
                    fill="none"
                    :class="[
                      detail.kind === 'healthcheck' ? 'stroke-success' : 'stroke-primary',
                      detailProgress === null ? 'origin-center stroke-(--ui-text-muted)' : '',
                      detailProgress === null ? 'animate-spin' : '',
                    ]"
                    stroke-width="3.5"
                    stroke-linecap="round"
                    pathLength="100"
                    :stroke-dasharray="detailProgress === null ? '28 72' : `${detailProgress} 100`"
                  />
                </svg>
                <span
                  class="absolute inset-0 flex items-center justify-center text-center text-xs font-semibold text-highlighted tabular-nums"
                >
                  {{ detailProgressLabel }}
                </span>
              </div>
              <span v-else class="mw-detail-icon size-10 shrink-0"
                ><UIcon :name="statusInfo(detail.kind, 'kind').icon" class="size-5"
              /></span>
              <div class="min-w-0">
                <h2 class="truncate text-lg font-semibold text-highlighted">
                  {{ detail.file ? fileName(detail.file.path) : 'File unavailable' }}
                </h2>
                <p v-if="imported" class="text-xs text-toned">Imported job (read-only)</p>
                <UTooltip v-if="detail.file" :text="detail.file.path"
                  ><p class="truncate text-sm text-toned">
                    {{ parentPath(detail.file.path) }}
                  </p></UTooltip
                >
              </div>
            </div>
            <div class="flex flex-wrap items-center justify-end gap-2">
              <UButton
                v-if="!imported && detail.file && onOpenFile"
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-file-video"
                :loading="fileLoading"
                @click="$emit('open-file', detail.file.id)"
                >View file</UButton
              >
              <UButton
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-download"
                :loading="exportLoading"
                @click="exportJob"
              >
                Export Job
              </UButton>
              <UButton
                v-if="!imported"
                color="neutral"
                variant="outline"
                size="sm"
                icon="i-lucide-refresh-cw"
                :loading="detailRefreshing"
                @click="refreshDetail"
              >
                Reload
              </UButton>
              <template v-for="action in jobActions" :key="action.kind">
                <UButton
                  color="neutral"
                  variant="outline"
                  size="sm"
                  :icon="action.icon"
                  :loading="actionLoading === action.kind"
                  :disabled="Boolean(actionLoading) || Boolean(imported)"
                  @click="runJobAction(action.kind)"
                  >{{ action.label }}</UButton
                >
              </template>
            </div>
          </div>
          <div v-if="detail.attempts.length" class="w-full min-w-0">
            <USelectMenu
              v-model="selectedAttemptId"
              :items="attemptOptions"
              value-key="value"
              icon="i-lucide-repeat-2"
              class="w-full min-w-0"
            />
          </div>
          <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard
              label="Status"
              :value="statusInfo(detail.status).label"
              icon="i-lucide-activity"
              :color="detail.error || detail.status === 'failed' ? 'error' : 'success'"
            />
            <StatCard label="Kind" :value="detail.kind" icon="i-lucide-workflow">
              <template #value>
                <span class="flex items-baseline gap-1.5">
                  <bdi>{{ statusInfo(detail.kind, 'kind').label }}</bdi>
                  <UBadge
                    v-if="
                      detail.kind === 'healthcheck' && selectedAttempt?.health?.['healthy'] === true
                    "
                    color="success"
                    variant="soft"
                    size="sm"
                    >Healthy</UBadge
                  >
                </span>
              </template>
            </StatCard>
            <StatCard label="Attempts" :value="detail.attempts.length" icon="i-lucide-repeat-2" />
            <StatCard
              label="Executor"
              :value="
                selectedAttempt?.node_name ??
                selectedAttempt?.node_id ??
                (selectedAttempt && detail.kind !== 'transcode' ? 'Server' : 'Unassigned')
              "
              icon="i-lucide-server"
            />
          </div>
          <p v-if="detailActive && progressStage(detail.progress)" class="text-sm text-toned">
            {{ progressStage(detail.progress) }}
          </p>
          <UAlert
            v-if="localStatsEditing"
            color="warning"
            variant="soft"
            icon="i-lucide-triangle-alert"
            description="Server modify MKV in-place for statistics."
          />
          <dl
            v-if="metadata.length"
            class="grid grid-cols-1 overflow-hidden rounded-md border border-default text-sm [&>div]:border-t [&>div]:border-default [&>div:first-child]:border-t-0 sm:grid-cols-2 sm:[&>div]:border-l sm:[&>div:nth-child(-n+2)]:border-t-0 sm:[&>div:nth-child(2n+1)]:border-l-0 lg:grid-cols-4 lg:[&>div:nth-child(-n+4)]:border-t-0 lg:[&>div:nth-child(2n+1)]:border-l lg:[&>div:nth-child(4n+1)]:border-l-0"
          >
            <div
              v-for="row in metadata"
              :key="row.key"
              class="flex min-w-0 flex-col gap-1 border-default p-3"
            >
              <dt class="flex items-center gap-1 text-xs text-toned">
                <UIcon :name="row.icon" class="size-3.5" />{{ row.label }}
              </dt>
              <dd class="flex flex-wrap items-center gap-1.5" :class="row.color">
                <UTooltip
                  :text="row.tooltip ?? undefined"
                  :disabled="!row.tooltip"
                  :ui="{
                    content: 'h-auto max-h-80 max-w-sm items-start overflow-y-auto',
                    text: 'min-w-0 overflow-visible text-clip whitespace-pre-line wrap-break-word',
                  }"
                >
                  <span
                    class="flex min-w-0 flex-wrap items-center gap-1.5"
                    :tabindex="row.tooltip ? 0 : undefined"
                  >
                    <span v-if="row.after !== null" class="sr-only">Before:</span>
                    <span class="break-all">{{ row.value }}</span>
                    <template v-if="row.after !== null">
                      <span class="sr-only">{{ row.expected ? 'Planned:' : 'After:' }}</span>
                      <UIcon
                        name="i-lucide-arrow-right"
                        class="size-3.5 shrink-0"
                        aria-hidden="true"
                      />
                      <span class="break-all">{{ row.after }}</span>
                    </template>
                  </span>
                </UTooltip>
              </dd>
            </div>
          </dl>
          <UAlert
            v-if="reason"
            color="info"
            variant="soft"
            icon="i-lucide-info"
            :ui="{
              root: 'p-2 gap-2',
              icon: 'size-4',
              description: 'text-xs',
            }"
          >
            <template #description><ReasonDisplay :reason="reason" /></template>
          </UAlert>
        </section>
        <UAlert
          v-if="detail?.warning"
          color="warning"
          variant="soft"
          icon="i-lucide-triangle-alert"
          title="Waiting"
          :description="detail.warning"
        />
        <UAlert
          v-if="detail.error"
          color="error"
          variant="soft"
          icon="i-lucide-circle-alert"
          title="Job error"
          :description="detail.error"
        />
        <UAlert
          v-if="
            selectedAttempt?.error &&
            (selectedAttempt.error !== detail.error ||
              selectedAttempt.id !== detail.attempts.at(-1)?.id)
          "
          color="error"
          variant="soft"
          icon="i-lucide-circle-alert"
          :title="`Attempt ${selectedAttempt.attempt_number} error`"
          :description="selectedAttempt.error"
        />
        <section>
          <dl class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-toned">
            <div
              v-if="detail.status === 'staging' || detail.status === 'queued'"
              class="flex gap-1"
            >
              <dt class="text-toned">Priority</dt>
              <dd>{{ detail.priority }}</dd>
            </div>
            <div class="flex gap-1">
              <dt class="flex items-center gap-1 text-toned">
                <UIcon name="i-lucide-calendar-plus" class="size-3.5" />
                Created
              </dt>
              <dd>
                <UTooltip :text="formatDate(detail.created_at, 'detail')"
                  ><time :datetime="toIsoString(detail.created_at)">{{
                    formatRelativeTime(detail.created_at, 'en')
                  }}</time></UTooltip
                >
              </dd>
            </div>
            <div class="flex gap-1">
              <dt class="flex items-center gap-1 text-toned">
                <UIcon name="i-lucide-play" class="size-3.5" />
                Started
              </dt>
              <dd>
                <UTooltip v-if="detail.started_at" :text="formatDate(detail.started_at, 'detail')"
                  ><time :datetime="toIsoString(detail.started_at)">{{
                    formatRelativeTime(detail.started_at, 'en')
                  }}</time></UTooltip
                >
                <template v-else>Not recorded</template>
              </dd>
            </div>
            <div v-if="estimatedEnd" class="flex gap-1">
              <dt class="flex items-center gap-1 text-toned">
                <UIcon name="i-lucide-timer" class="size-3.5" />
                Estimated end
              </dt>
              <dd>
                <UTooltip :text="formatDate(estimatedEnd, 'detail')"
                  ><time :datetime="toIsoString(estimatedEnd)">{{
                    formatRelativeTime(estimatedEnd, 'en')
                  }}</time></UTooltip
                >
              </dd>
            </div>
            <div v-if="detail.finished_at" class="flex gap-1">
              <dt class="flex items-center gap-1 text-toned">
                <UIcon name="i-lucide-check-circle" class="size-3.5" />
                Finished
              </dt>
              <dd>
                <UTooltip :text="formatDate(detail.finished_at, 'detail')"
                  ><time :datetime="toIsoString(detail.finished_at)">{{
                    formatRelativeTime(detail.finished_at, 'en')
                  }}</time></UTooltip
                >
              </dd>
            </div>
          </dl>
        </section>
        <JobMediaSnapshots :attempt="selectedAttempt" :events="detail.events" />
        <section
          v-if="traceFlow || decisionSteps.length || detail.kind === 'transcode'"
          class="space-y-3"
        >
          <h3
            class="flex items-center justify-between gap-2 text-base font-semibold text-highlighted"
          >
            <span class="flex items-center gap-2">
              <UIcon name="i-lucide-git-branch" class="size-5" />
              Flow decisions
            </span>
            <UButton
              variant="ghost"
              color="neutral"
              size="sm"
              :trailing-icon="
                flowDecisionsVisible ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'
              "
              :aria-label="flowDecisionsVisible ? 'Hide flow decisions' : 'Show flow decisions'"
              :aria-expanded="flowDecisionsVisible"
              @click="flowDecisionsVisible = !flowDecisionsVisible"
            />
          </h3>
          <div v-if="flowDecisionsVisible" class="space-y-3">
            <p v-if="traceFlow && detail.flow_snapshot" class="text-sm text-toned">
              {{ detail.flow_snapshot.name }} · revision
              {{ detail.flow_snapshot.version }}
            </p>
            <DecisionTraceFlow
              v-if="traceFlow"
              :definition="traceFlow"
              :steps="decisionSteps"
              :plugins="plugins"
              movable
              @move="moveTraceNode"
            />
            <p v-if="!traceFlow" class="text-sm text-toned">
              Flow definition was not recorded for this job.
            </p>
            <ol v-if="!traceFlow && decisionSteps.length" class="mw-frame space-y-2 p-3">
              <li
                v-for="(entry, index) in decisionSteps"
                :key="index"
                class="flex flex-wrap items-center gap-2 text-sm"
              >
                <StatusBadge :status="entry.outcome" group="outcome" /><span>{{
                  entry.step ?? entry.node
                }}</span>
                <ReasonDisplay v-if="entry.reason" :reason="entry.reason" class="w-full" />
                <span v-if="entry.error" class="text-xs text-error">{{ entry.error }}</span>
              </li>
            </ol>
          </div>
        </section>
        <section v-if="argvText" class="space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
              <UIcon name="i-lucide-terminal" class="size-5" />
              Command
            </h3>
            <UButton
              v-if="!imported"
              color="neutral"
              variant="outline"
              size="sm"
              icon="i-lucide-copy"
              @click="copyText(argvText)"
              >Copy command</UButton
            >
          </div>
          <pre class="mw-frame overflow-x-auto p-3 font-mono text-xs leading-relaxed text-toned">{{
            argvText
          }}</pre>
        </section>
        <section v-if="selectedAttempt?.health && healthIssues.length" class="space-y-3">
          <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
            <UIcon name="i-lucide-heart-pulse" class="size-5" />
            Health
          </h3>
          <UAlert
            color="warning"
            variant="soft"
            title="Issues found"
            :description="`${healthIssues.length} finding${healthIssues.length === 1 ? '' : 's'}: ${healthIssues.slice(0, 3).join('; ')}`"
          />
        </section>
        <section v-if="actionRows.length" class="space-y-3">
          <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
            <UIcon name="i-lucide-list-checks" class="size-5" />
            Action results
          </h3>
          <ul class="mw-frame space-y-2 p-3 text-sm">
            <li
              v-for="(action, index) in actionRows"
              :key="index"
              class="flex flex-wrap items-center gap-2"
            >
              <StatusBadge :status="action.ok ? 'ok' : 'failed'" group="action" /><span>{{
                action.kind
              }}</span
              ><span v-if="action.phase" class="text-xs text-toned">{{ action.phase }}</span
              ><span v-if="action.error" class="text-error">{{ action.error }}</span
              ><span v-if="action.artifacts.length" class="text-xs text-toned">
                {{ action.artifacts.join(', ') }}
              </span>
            </li>
          </ul>
        </section>
        <section v-if="detail.notification_deliveries.length" class="space-y-3">
          <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
            <UIcon name="i-lucide-bell" class="size-5" />
            Notification deliveries
          </h3>
          <ul class="mw-frame space-y-2 p-3 text-sm">
            <li
              v-for="delivery in detail.notification_deliveries"
              :key="delivery.id"
              class="flex flex-wrap items-center gap-2"
            >
              <StatusBadge :status="delivery.status" group="notification" />
              <span>{{ delivery.target_name }}</span>
              <span class="text-xs text-toned">{{ delivery.attempts }} attempts</span>
              <span v-if="delivery.error" class="text-error">{{ delivery.error }}</span>
            </li>
          </ul>
        </section>
        <section class="space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
              <UIcon name="i-lucide-scroll-text" class="size-5" />
              Attempt activity
            </h3>
            <UButton
              variant="ghost"
              color="neutral"
              size="sm"
              :trailing-icon="activityVisible ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
              :aria-label="activityVisible ? 'Hide attempt activity' : 'Show attempt activity'"
              :aria-expanded="activityVisible"
              @click="activityVisible = !activityVisible"
            />
          </div>
          <div v-if="activityVisible" class="space-y-3">
            <div class="flex flex-wrap items-center gap-2">
              <UInput
                v-model="logFilter"
                size="xs"
                icon="i-lucide-search"
                placeholder="Filter lines"
                aria-label="Filter log lines"
              />
              <UTooltip :text="logWrap ? 'Disable line wrapping' : 'Enable line wrapping'">
                <UButton
                  size="xs"
                  color="neutral"
                  icon="i-lucide-wrap-text"
                  :variant="logWrap ? 'soft' : 'outline'"
                  :aria-pressed="logWrap"
                  @click="logWrap = !logWrap"
                >
                  Wrap lines
                </UButton>
              </UTooltip>
              <UButton
                size="xs"
                color="neutral"
                variant="outline"
                icon="i-lucide-refresh-cw"
                @click="() => loadLog(true)"
                >Refresh log</UButton
              ><UButton
                size="xs"
                color="neutral"
                variant="outline"
                :disabled="!logText"
                icon="i-lucide-copy"
                @click="copyText(logText)"
                >Copy log</UButton
              ><UButton
                v-if="!logComplete && !imported"
                size="xs"
                color="neutral"
                variant="outline"
                icon="i-lucide-history"
                @click="() => loadLog(true)"
                >Load full log</UButton
              ><UButton
                v-if="selectedAttempt?.log_path && !imported"
                color="neutral"
                variant="outline"
                size="xs"
                icon="i-lucide-download"
                :loading="logDownloading"
                @click="downloadLog"
                >Download log</UButton
              >
            </div>
            <div
              ref="activityLog"
              class="mw-frame max-h-112 overflow-y-auto p-2"
              @scroll="updateLogFollowing"
            >
              <p v-if="logText" class="px-3 py-2 text-xs text-toned">
                Showing the latest {{ LOG_WINDOW }} matching log lines. Stored logs and exports
                remain complete.
              </p>
              <p v-if="!activityStages.length" class="px-3 py-6 text-center text-sm text-toned">
                {{ logText ? `No lines match "${logFilter}".` : logMessage }}
              </p>
              <section
                v-for="stage in activityStages"
                :key="stage.name"
                class="border-b border-default/60 p-2 last:border-0"
              >
                <h4 class="flex items-center gap-2 text-sm font-semibold text-highlighted">
                  <UIcon :name="stage.icon" :class="['size-4', stage.tone]" />{{ stage.name }}
                  <span class="text-xs font-normal text-toned"
                    >{{ stage.events.length }} events · {{ stage.lines.length }} lines</span
                  >
                </h4>
                <ol v-if="stage.events.length" class="mt-1 space-y-1">
                  <li
                    v-for="event in stage.events"
                    :key="event.id"
                    class="flex items-start gap-2 text-xs"
                  >
                    <span class="font-medium text-highlighted">{{ humanEvent(event.type) }}</span>
                    <span v-if="eventSummary(event)" class="min-w-0 truncate text-toned">{{
                      eventSummary(event)
                    }}</span>
                    <span v-if="event.attempt_id" class="shrink-0 text-toned"
                      >Attempt {{ attemptNumber(event.attempt_id) }}</span
                    >
                    <UTooltip :text="formatDate(event.ts, 'detail')"
                      ><time
                        class="ml-auto shrink-0 text-toned"
                        :datetime="toIsoString(event.ts)"
                        >{{ formatRelativeTime(event.ts, 'en') }}</time
                      ></UTooltip
                    >
                  </li>
                </ol>
                <pre
                  v-if="stage.lines.length"
                  class="mt-1 overflow-x-auto bg-elevated/20 px-2 py-1 font-mono text-xs leading-relaxed"
                  :class="logWrap ? 'whitespace-pre-wrap wrap-break-word' : 'whitespace-pre'"
                  >{{ stage.lines.join('\n') }}</pre>
              </section>
            </div>
          </div>
        </section>
      </div>
      <UAlert
        v-else-if="detailError"
        color="error"
        variant="soft"
        icon="i-lucide-circle-alert"
        title="Unable to load job"
        :description="detailError"
      />
      <div v-else class="py-8 text-center text-sm text-toned">Loading job details...</div>
    </template>
  </UModal>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useStorage } from '@vueuse/core';
import StatusBadge from '~/components/StatusBadge.vue';
import StatCard from '~/components/StatCard.vue';
import DecisionTraceFlow from '~/components/DecisionTraceFlow.vue';
import ReasonDisplay from '~/components/ReasonDisplay.vue';
import JobMediaSnapshots from '~/components/JobMediaSnapshots.vue';
import { copyText, jobNumber } from '~/utils';
import { jobMetadata } from '~/utils/mediaMetadata';
import { jobReason } from '~/utils/jobList';
import { formatDate, toIsoString } from '~/utils/date';
import { healthSamples } from '~/utils/health';
import { jobLogLines, LOG_WINDOW } from '~/utils/logs';
import { formatRelativeTime } from '~/utils/relativeTime';
import { estimatedJobCompletion, progressPercent, progressStage } from '~/utils/progress';
import { statusInfo } from '~/utils/status';
import { useEvents, type ServerEvent } from '~/composables/useEvents';
import { useJobs } from '~/composables/useJobs';
import { useJobRequeue } from '~/composables/useJobRequeue';
import { usePlugins } from '~/composables/usePlugins';
import { applyJobDetail, applyJobProgress, applyQueuedJob } from '~/composables/stateHelpers';
import type {
  ActionResult,
  FlowDefinition,
  FlowNodeResult,
  FlowUiNodePos,
  JobDetail,
  JobAttemptDetail,
} from '~/types/api';
import { ACTIVE_JOB_STATUSES } from '~/types/api';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
const props = defineProps<{
  jobId: number | null;
  imported?: JobDetail | null;
  fileLoading?: boolean;
  onOpenFile?: (id: number) => void;
}>();
defineEmits<{ 'open-file': [id: number] }>();
const open = defineModel<boolean>('open', { default: false });
const fullscreen = useStorage('job_details_fullscreen', false);
const detail = ref<JobDetail | null>(null);
const reason = computed(() => (detail.value ? jobReason(detail.value) : null));
const jobTitle = computed(() => {
  const id = props.imported?.id ?? props.jobId;
  return id === null ? 'Job details' : `Job ${jobNumber(id)}`;
});
const detailJobId = computed(() => props.jobId);
const selectedAttemptId = ref<number | undefined>(undefined);
const logText = ref('');
const logComplete = ref(false);
const logMessage = ref('No log recorded.');
const logRequest = ref(0);
const logFilter = ref('');
const logWrap = useStorage('job_log_wrap', true);
const activityLog = ref<HTMLDivElement | null>(null);
const logFollowing = ref(true);
const flowDecisionsVisible = useStorage('job_flow_decisions_visible', true);
const activityVisible = useStorage('job_activity_visible', true);
const detailRefreshing = ref(false);
const detailError = ref('');
const exportLoading = ref(false);
const logDownloading = ref(false);
let exportRequest = 0;
const actionLoading = ref<'bump' | 'cancel' | 'retry' | null>(null);
const notify = useNotification();
const { messageFor } = useApiErrorMessage();
const { runAction, get, attemptLog, downloadLog: fetchLogDownload } = useJobs();
const { requeue } = useJobRequeue();
const { items: plugins, ensure: ensurePlugins } = usePlugins();
const stageDefinitions = [
  ['Queue', ['enqueued', 'staged', 'queued', 'retried', 'bumped'], 'i-lucide-list-ordered'],
  ['Assign', ['claimed', 'offered', 'offer_failed'], 'i-lucide-send'],
  ['Prepare', ['preparation', 'staging'], 'i-lucide-box'],
  ['Run', ['started', 'progress'], 'i-lucide-loader-circle'],
  ['Verify', ['verifying'], 'i-lucide-file-check'],
  [
    'Finalize',
    [
      'finalizing',
      'post_actions',
      'action_results',
      'finalization_recovered',
      'finalization_rolled_back',
    ],
    'i-lucide-list-checks',
  ],
  [
    'Result',
    ['succeeded', 'failed', 'cancelled', 'skipped', 'stall_cancelled'],
    'i-lucide-check-circle',
  ],
  ['Other', [], 'i-lucide-activity'],
] as const;
const eventStage = (type: string): string => {
  const normalized = type.toLowerCase();
  const found = stageDefinitions.find(([, _types]) =>
    _types.some(
      (name) =>
        normalized === name || normalized.endsWith(`.${name}`) || normalized.includes(`${name}_`),
    ),
  );
  if (found) return found[0];
  if (normalized.includes('offer') || normalized.includes('assignment')) return 'Assign';
  return 'Other';
};
const attemptNumber = (id: number): number | string =>
  detail.value?.attempts.find((attempt) => attempt.id === id)?.attempt_number ?? 'Unknown';
type ActivityStage = {
  name: string;
  icon: string;
  tone: string;
  events: NonNullable<JobDetail['events']>;
  lines: string[];
};
const activityStages = computed(() => {
  const job = detail.value;
  const attempt = selectedAttempt.value;
  if (!job) return [];
  if (!attempt) {
    return [
      {
        name: 'Other',
        icon: 'i-lucide-activity',
        tone: 'text-toned',
        events: job.events,
        lines: [],
      },
    ];
  }
  const previous = job.attempts.find((item) => item.attempt_number === attempt.attempt_number - 1);
  const start = previous?.finished_at ?? job.created_at;
  const end = job.attempts.find(
    (item) => item.attempt_number === attempt.attempt_number + 1,
  )?.claimed_at;
  const events = job.events.filter((event) => {
    if (event.attempt_id === attempt.id) return true;
    if (event.attempt_id !== null && event.attempt_id !== undefined) return false;
    return event.ts >= start && (!end || event.ts < end);
  });
  const filteredLines = jobLogLines(logText.value, logFilter.value);
  const stages: ActivityStage[] = stageDefinitions.map(([name, _types, icon]) => {
    const stageName = name;
    return {
      name: stageName,
      icon,
      tone: name === 'Result' ? 'text-success' : 'text-primary',
      events: events.filter((event) => eventStage(event.type) === name),
      lines: filteredLines.filter(({ stage }) => stage === stageName).map(({ line }) => line),
    };
  });
  stages.push(
    ...(['Recent output', 'Health check'].map((name) => ({
      name,
      icon: 'i-lucide-scroll-text',
      tone: 'text-toned',
      events: [],
      lines: filteredLines.filter(({ stage }) => stage === name).map(({ line }) => line),
    })) satisfies ActivityStage[]),
  );
  return stages.filter((stage) => stage.events.length || stage.lines.length);
});

const fileName = (path: string): string => path.split(/[\\/]/).at(-1) || path;
const parentPath = (path: string): string =>
  path.slice(0, Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))) || 'Root';
const humanEvent = (type: string): string =>
  type
    .split(/[._-]/)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
type DetailLoad = {
  id: number;
  updates: Array<Extract<ServerEvent, { type: 'job.status' | 'job.progress' }>>;
};
let detailLoad: DetailLoad | null = null;
let detailDeleted = false;
const loadDetail = async (id: number): Promise<JobDetail | null> => {
  const pending: DetailLoad = { id, updates: [] };
  detailLoad = pending;
  detailError.value = '';
  try {
    const job = await get(id, true);
    if (
      detailLoad === pending &&
      open.value &&
      detailJobId.value === id &&
      !props.imported &&
      !detailDeleted
    ) {
      const wasActive = ACTIVE_JOB_STATUSES.includes(job.status);
      for (const update of pending.updates) {
        if (update.type === 'job.status') {
          if ('job' in update.payload) applyJobDetail(job, update.payload.job);
          else applyQueuedJob(job, update.payload);
        } else applyJobProgress(job, update.payload);
      }
      detail.value = job;
      detailLoad = null;
      traceFlow.value = validFlowDefinition(job.flow_snapshot?.definition)
        ? job.flow_snapshot.definition
        : null;
      if (!job.attempts.some((attempt) => attempt.id === selectedAttemptId.value))
        selectedAttemptId.value = job.attempts.at(-1)?.id;
      if (wasActive && !ACTIVE_JOB_STATUSES.includes(job.status)) return await loadDetail(id);
      await loadLog();
      return job;
    }
  } catch (error) {
    if (detailLoad === pending && open.value && detailJobId.value === id && !props.imported) {
      const failure = error as { status?: number; detail?: string };
      detailError.value =
        failure.status === 404
          ? 'Job not found.'
          : messageFor(failure.detail, 'Unable to load job details.');
    }
  } finally {
    if (detailLoad === pending) detailLoad = null;
  }
  return null;
};

const traceFlow = ref<FlowDefinition | null>(null);
const moveTraceNode = (id: string, position: FlowUiNodePos): void => {
  const flow = traceFlow.value;
  if (!flow) return;
  traceFlow.value = {
    ...flow,
    _ui: { ...flow._ui, nodes: { ...flow._ui?.nodes, [id]: position } },
  };
};
const validFlowDefinition = (value: unknown): value is FlowDefinition => {
  if (!value || typeof value !== 'object') return false;
  const definition = value as Partial<FlowDefinition>;
  return (
    typeof definition.entry === 'string' &&
    Array.isArray(definition.nodes) &&
    definition.nodes.every((node) => {
      if (!node || typeof node.id !== 'string') return false;
      if (node.type === 'plugin') return typeof node.plugin === 'string';
      if (node.type === 'condition') return node.when !== null && typeof node.when === 'object';
      return node.type === 'terminal' && typeof node.outcome === 'string';
    }) &&
    Array.isArray(definition.edges) &&
    definition.edges.every(
      (edge) =>
        !!edge &&
        typeof edge.src === 'string' &&
        typeof edge.dst === 'string' &&
        typeof edge.port === 'string',
    )
  );
};

type JobAction = 'bump' | 'cancel' | 'retry';
const jobActions = computed<Array<{ kind: JobAction; label: string; icon: string }>>(() => {
  if (props.imported) return [];
  const status = detail.value?.status;
  if (status === 'staging' || status === 'queued') {
    return [
      { kind: 'bump', label: 'Move to top', icon: 'i-lucide-arrow-up' },
      { kind: 'cancel', label: 'Cancel', icon: 'i-lucide-x' },
    ];
  }
  if (status && ACTIVE_JOB_STATUSES.includes(status))
    return [{ kind: 'cancel', label: 'Cancel', icon: 'i-lucide-x' }];
  if (status === 'succeeded' || status === 'failed' || status === 'cancelled') {
    return [{ kind: 'retry', label: 'Requeue', icon: 'i-lucide-rotate-ccw' }];
  }
  return [];
});
const runJobAction = async (action: JobAction): Promise<void> => {
  if (props.imported) return;
  const id = detail.value?.id;
  if (id === undefined || actionLoading.value) return;
  actionLoading.value = action;
  try {
    if (action === 'retry') {
      await requeue(id);
      return;
    }
    const result = await runAction(id, action);
    if (!result.ok) notify.error(messageFor(result.error.detail, 'Request failed.'));
    else {
      notify.success(
        action === 'bump'
          ? `Job #${id} moved to the top of the queue.`
          : `Cancellation requested for job #${id}.`,
      );
      if (open.value && detailJobId.value === id && !props.imported) await loadDetail(id);
    }
  } finally {
    actionLoading.value = null;
  }
};
const refreshDetail = async (): Promise<void> => {
  if (props.imported) return;
  const id = detail.value?.id;
  if (id === undefined || detailRefreshing.value) return;
  detailRefreshing.value = true;
  try {
    await loadDetail(id);
  } finally {
    if (detailJobId.value === id && !props.imported) detailRefreshing.value = false;
  }
};

const exportJob = async (): Promise<void> => {
  const snapshot = detail.value;
  if (!snapshot || exportLoading.value) return;
  const token = ++exportRequest;
  const imported = props.imported;
  exportLoading.value = true;
  try {
    const job = imported ? snapshot : await get(snapshot.id, true);
    if (
      !job ||
      token !== exportRequest ||
      !open.value ||
      props.imported !== imported ||
      detailJobId.value !== snapshot.id
    )
      return;
    const result = {
      blob: new Blob([JSON.stringify(job, null, 2)], {
        type: 'application/json',
      }),
      filename: `job-${job.id}.json`,
    };
    const url = URL.createObjectURL(result.blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = result.filename;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    if (token === exportRequest)
      notify.error(messageFor(error instanceof Error ? error.message : undefined));
  } finally {
    if (token === exportRequest) exportLoading.value = false;
  }
};

const { on } = useEvents();
const stopEvents = on((event) => {
  if (
    !props.imported &&
    event.type === 'job.deleted' &&
    detailJobId.value === event.payload.job_id
  ) {
    detailDeleted = true;
    detail.value = null;
    detailError.value = 'Job not found.';
    logRequest.value++;
    return;
  }
  if (detailDeleted) return;
  if (
    !props.imported &&
    detailLoad &&
    (event.type === 'job.status' || event.type === 'job.progress') &&
    detailLoad.id === event.payload.job_id
  ) {
    detailLoad.updates.push(event);
  }
  if (!props.imported && event.type === 'job.status' && detail.value?.id === event.payload.job_id) {
    if (!('job' in event.payload)) {
      applyQueuedJob(detail.value, event.payload);
      return;
    }
    const row = event.payload.job;
    const current = detail.value;
    const previousStatus = current.status;
    if (!applyJobDetail(current, row)) return;
    if (row.latest_attempt) {
      if (selectedAttemptId.value === undefined) selectedAttemptId.value = row.latest_attempt.id;
    }
    const terminal = ['succeeded', 'failed', 'cancelled', 'skipped'].includes(event.payload.status);
    if (terminal && previousStatus !== row.status) void loadDetail(row.id);
  } else if (
    !props.imported &&
    event.type === 'job.progress' &&
    detail.value?.id === event.payload.job_id
  ) {
    applyJobProgress(detail.value, event.payload);
  }
});
onBeforeUnmount(() => {
  detailLoad = null;
  exportRequest++;
  logRequest.value++;
  stopEvents();
});

const loadForJob = async (id: number | null): Promise<void> => {
  detailDeleted = false;
  exportRequest++;
  exportLoading.value = false;
  detailRefreshing.value = false;
  detailError.value = '';
  logRequest.value++;
  detailLoad = null;
  detail.value = null;
  traceFlow.value = null;
  selectedAttemptId.value = undefined;
  logText.value = '';
  logComplete.value = false;
  logMessage.value = 'Loading log...';
  if (props.imported) {
    detail.value = props.imported;
    if (validFlowDefinition(detail.value.flow_snapshot?.definition))
      traceFlow.value = detail.value.flow_snapshot.definition;
    selectedAttemptId.value = detail.value.attempts.at(-1)?.id;
    await loadLog();
  } else if (id !== null) {
    void ensurePlugins();
    await loadDetail(id);
  }
};

const selectedAttempt = computed<JobAttemptDetail | null>(() => {
  const id = selectedAttemptId.value;
  return detail.value?.attempts.find((attempt) => attempt.id === id) ?? null;
});
const localStatsEditing = computed(() => {
  const job = detail.value;
  const attempt = selectedAttempt.value;
  return (
    !props.imported &&
    job?.kind === 'mkv_stats' &&
    job.status === 'running' &&
    attempt?.status === 'running' &&
    attempt.id === job.attempts.at(-1)?.id &&
    attempt.capabilities === null
  );
});
const attemptOptions = computed(() =>
  (detail.value?.attempts ?? []).map((attempt) => ({
    label: `#${attempt.attempt_number} · ${statusInfo(attempt.status).label}${attempt.result ? ` · ${statusInfo(attempt.result).label}` : ''} · ${attempt.node_name ?? attempt.node_id ?? (detail.value?.kind === 'transcode' ? 'Unassigned' : 'Server')}`,
    value: attempt.id,
  })),
);
const healthIssues = computed(() => healthSamples(selectedAttempt.value?.health));

const metadata = computed(() =>
  jobMetadata(
    detail.value?.file ?? null,
    selectedAttempt.value,
    selectedAttempt.value?.id === detail.value?.attempts.at(-1)?.id ? detail.value : null,
  ),
);

const detailActive = computed(() => {
  const status = detail.value?.status;
  return status ? ACTIVE_JOB_STATUSES.includes(status) : false;
});

const updateLogFollowing = (): void => {
  const element = activityLog.value;
  if (element)
    logFollowing.value = element.scrollHeight - element.scrollTop - element.clientHeight <= 24;
};

const scrollLogBottom = async (): Promise<void> => {
  if (!detailActive.value || !logFollowing.value) return;
  await nextTick();
  const element = activityLog.value;
  if (element) element.scrollTop = element.scrollHeight;
};

watch(activityVisible, (visible) => {
  if (!visible) return;
  logFollowing.value = true;
  void scrollLogBottom();
});

const downloadLog = async (): Promise<void> => {
  const jobId = detailJobId.value;
  const attemptId = selectedAttemptId.value;
  if (jobId === null || attemptId === undefined || logDownloading.value) return;
  logDownloading.value = true;
  let url: string | null = null;
  try {
    const blob = await fetchLogDownload(jobId, attemptId);
    url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `job-${jobId}-attempt-${attemptId}.log`;
    link.click();
  } catch (error) {
    notify.error(messageFor((error as { detail?: string }).detail, 'Unable to download log.'));
  } finally {
    if (url) URL.revokeObjectURL(url);
    logDownloading.value = false;
  }
};

const loadLog = async (complete = false): Promise<void> => {
  const token = ++logRequest.value;
  const jobId = detailJobId.value;
  const attemptId = selectedAttemptId.value;
  if (jobId === null || attemptId === undefined) return;
  if (props.imported || (!complete && selectedAttempt.value?.log_data != null)) {
    const savedLog = selectedAttempt.value?.log_data ?? null;
    logText.value = savedLog ?? '';
    logComplete.value = true;
    logMessage.value =
      savedLog === null ? 'No log recorded for this attempt.' : savedLog ? '' : 'The log is empty.';
    return;
  }
  logComplete.value = false;
  logMessage.value = 'Loading log...';
  try {
    const result = await attemptLog(jobId, attemptId, complete);
    if (
      token !== logRequest.value ||
      !open.value ||
      props.imported ||
      jobId !== detailJobId.value ||
      attemptId !== selectedAttemptId.value
    )
      return;
    if (result.missing) {
      logText.value = '';
      logMessage.value = 'No log recorded for this attempt.';
      return;
    }
    logText.value = result.text;
    logComplete.value = result.complete;
    if (result.complete && selectedAttempt.value) selectedAttempt.value.log_data = result.text;
    logMessage.value = logText.value ? '' : 'The log is empty.';
    await scrollLogBottom();
  } catch {
    if (token === logRequest.value && !props.imported) {
      logText.value = '';
      logMessage.value = "Couldn't load this attempt's log. Choose Refresh log to try again.";
    }
  }
};

watch(selectedAttemptId, () => {
  logRequest.value++;
  logText.value = '';
  logComplete.value = false;
  logFollowing.value = true;
  if (open.value) void loadLog();
});

const detailProgress = computed<number | null>(() => {
  const job = detail.value;
  if (!job || job.status === 'pending') return null;
  return progressPercent(job.progress);
});

const estimatedEnd = computed(() => (detail.value ? estimatedJobCompletion(detail.value) : null));

const detailProgressLabel = computed(() => {
  if (detailProgress.value !== null) return `${Math.round(detailProgress.value)}%`;
  if (detail.value?.progress?.['tool'] === 'ffprobe') return 'Probe';
  if (detail.value?.progress?.['tool'] === 'mkvpropedit') return 'Stats';
  const stage = detail.value?.progress?.['stage'];
  if (stage === 'probe') return 'Probe';
  if (stage === 'decode') return 'Decode';
  const status = detail.value?.status;
  if (status === 'claimed') return 'Claim';
  if (status === 'running') return 'Run';
  if (status === 'verifying') return 'Verify';
  if (status === 'finalizing') return 'Finish';
  return '';
});

const eventSummary = (event: { payload: Record<string, unknown> | null }): string => {
  const payload = event.payload;
  if (!payload) return '';
  if (typeof payload['error'] === 'string' && payload['error']) return payload['error'];
  if (typeof payload['reason'] === 'string' && payload['reason']) return payload['reason'];
  if (typeof payload['node'] === 'string' && payload['node']) return payload['node'];
  return '';
};

const decisionSteps = computed<FlowNodeResult[]>(() => detail.value?.decision_log ?? []);

const argvText = computed(() => selectedAttempt.value?.argv?.join(' ') ?? '');

type ActionRow = {
  kind: string;
  ok: boolean;
  phase: string;
  error: string | null;
  artifacts: string[];
};

const actionRowsFrom = (raw: unknown, phase: string): ActionRow[] => {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item): ActionRow[] => {
    if (!item || typeof item !== 'object' || typeof (item as ActionResult).kind !== 'string') {
      return [];
    }
    const entry = item as ActionResult;
    return [
      {
        kind: entry.kind,
        ok: entry.ok === true,
        phase,
        error: entry.error ?? null,
        artifacts: entry.artifacts ?? [],
      },
    ];
  });
};

const actionRows = computed<ActionRow[]>(() => {
  return actionRowsFrom(selectedAttempt.value?.action_results, '');
});

watch(
  [open, detailJobId, () => props.imported],
  ([isOpen, id]) => {
    if (isOpen) void loadForJob(id);
    else {
      exportRequest++;
      exportLoading.value = false;
      logRequest.value++;
      detailLoad = null;
      detail.value = null;
      selectedAttemptId.value = undefined;
      logText.value = '';
      logComplete.value = false;
      logMessage.value = 'No log recorded.';
    }
  },
  { immediate: true },
);
</script>
