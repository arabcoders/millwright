<template>
  <form id="libraryForm" autocomplete="off" class="space-y-5" @submit.prevent="submitForm">
    <section class="space-y-3" aria-labelledby="library-details-heading">
      <h3
        id="library-details-heading"
        class="flex items-center gap-2 text-base font-semibold text-highlighted"
      >
        <UIcon name="i-lucide-folder-tree" class="size-5" />
        Library
      </h3>
      <div class="grid gap-4 lg:grid-cols-2">
        <UFormField label="Name" description="Used in job lists." required>
          <UInput v-model="name" placeholder="movies-hevc" class="w-full" />
        </UFormField>

        <UFormField label="Flow" description="Processes files found in this library.">
          <USelectMenu v-model="flowId" :items="flowItems" value-key="value" class="w-full" />
        </UFormField>
      </div>

      <UFormField
        label="Root folders"
        description="Folders the scanner checks and watches for new files."
        required
      >
        <div class="space-y-2">
          <div v-for="(_, index) in roots" :key="index" class="flex gap-2">
            <UInput
              v-model="roots[index]"
              placeholder="/media/movies"
              class="w-full"
              :aria-label="`Folder ${index + 1}`"
            />

            <UButton
              type="button"
              color="neutral"
              variant="outline"
              icon="i-lucide-folder-open"
              aria-label="Browse folders"
              @click="openPicker(index)"
            />

            <UButton
              type="button"
              color="neutral"
              variant="outline"
              icon="i-lucide-trash"
              aria-label="Remove folder"
              :disabled="roots.length === 1"
              @click="roots.splice(index, 1)"
            />
          </div>

          <UButton
            type="button"
            color="neutral"
            variant="outline"
            icon="i-lucide-plus"
            aria-label="Add folder"
            @click="roots.push('')"
          >
            Add folder
          </UButton>
        </div>
      </UFormField>
    </section>

    <section class="space-y-3" aria-labelledby="library-scanning-heading">
      <h3
        id="library-scanning-heading"
        class="flex items-center gap-2 text-base font-semibold text-highlighted"
      >
        <UIcon name="i-lucide-radar" class="size-5" />
        Scanning
      </h3>
      <div class="grid gap-4 sm:grid-cols-2">
        <UFormField
          label="Watch"
          description="Detect new files through filesystem notifications (inotify on Linux)."
        >
          <USwitch v-model="watchEnabled" />
        </UFormField>

        <UFormField label="Scan on start" description="Scan this library when the server boots.">
          <USwitch v-model="scanOnStart" />
        </UFormField>
      </div>

      <UFormField
        label="Recurring scans"
        description="Scan all folders at this interval, independently of Watch, startup, and Scan now. Catches network changes missed by filesystem notifications."
      >
        <div class="flex flex-wrap gap-2">
          <USelectMenu
            v-model="scanMode"
            :items="scanModeItems"
            value-key="value"
            aria-label="Recurring scans"
            class="w-36"
          />
          <template v-if="scanMode === 'interval'">
            <UInput
              v-model="scanCount"
              type="number"
              min="0"
              step="any"
              required
              aria-label="Scan interval"
              class="w-24"
            />
            <USelectMenu
              v-model="scanUnit"
              :items="scanUnitItems"
              value-key="value"
              aria-label="Scan interval unit"
              class="w-32"
            />
          </template>
        </div>
      </UFormField>

      <div class="grid gap-4 sm:grid-cols-2">
        <UFormField label="MKV statistics" description="Write missing MKV stream statistics tags.">
          <template #label>
            <span class="inline-flex items-center gap-1.5">
              MKV statistics
              <UTooltip text="Can take a long time on large files or slow storage.">
                <span
                  tabindex="0"
                  role="img"
                  aria-label="About MKV statistics"
                  class="inline-flex shrink-0 cursor-help text-dimmed"
                >
                  <UIcon name="i-lucide-circle-help" class="size-3.5" aria-hidden="true" />
                </span>
              </UTooltip>
            </span>
          </template>
          <USwitch v-model="mkvStats" />
        </UFormField>

        <UFormField label="Health check" description="Decode media to detect errors.">
          <template #label>
            <span class="inline-flex items-center gap-1.5">
              Health check
              <UTooltip text="Uses CPU and storage I/O and can slow library processing.">
                <span
                  tabindex="0"
                  role="img"
                  aria-label="About health checks"
                  class="inline-flex shrink-0 cursor-help text-dimmed"
                >
                  <UIcon name="i-lucide-circle-help" class="size-3.5" aria-hidden="true" />
                </span>
              </UTooltip>
            </span>
          </template>
          <USwitch v-model="healthcheckEnabled" />
        </UFormField>
      </div>
    </section>

    <section class="space-y-3" aria-labelledby="library-cutoff-heading">
      <h3
        id="library-cutoff-heading"
        class="flex items-center gap-2 text-base font-semibold text-highlighted"
      >
        <UIcon name="i-lucide-calendar-range" class="size-5" />
        Automatic processing
      </h3>
      <UFormField
        :label="`Cutoff (${timezone})`"
        description="Automatically health check or transcode only files whose selected dates are on or after this cutoff. Leave empty for no date limit."
      >
        <UInput
          v-model="cutoffInput"
          type="datetime-local"
          step="any"
          class="w-full"
          :aria-label="`Automatic processing cutoff in ${timezone}`"
        />
      </UFormField>
      <template v-if="cutoffInput">
        <UFormField label="Compare dates" description="Select at least one date." required>
          <UCheckboxGroup
            v-model="cutoffFields"
            :items="cutoffFieldItems"
            aria-label="Cutoff comparison dates"
          />
        </UFormField>
        <UFormField v-if="cutoffFields.length > 1" label="Matching rule">
          <URadioGroup
            v-model="cutoffMatch"
            :items="cutoffMatchItems"
            aria-label="Cutoff matching rule"
          />
        </UFormField>
      </template>
      <p class="text-sm text-toned">
        Manual actions and existing jobs are unaffected. Scanning, probing, and output verification
        continue.
      </p>
    </section>

    <section class="space-y-3" aria-labelledby="library-timing-heading">
      <h3
        id="library-timing-heading"
        class="flex items-center gap-2 text-base font-semibold text-highlighted"
      >
        <UIcon name="i-lucide-clock" class="size-5" />
        Job timing
      </h3>
      <UFormField
        label="Cooldown"
        description="Seconds a file must stay unchanged before jobs start. 0 disables."
      >
        <UInput
          v-model="cooldownS"
          type="number"
          min="0"
          class="w-full"
          aria-label="Cooldown seconds"
        />
      </UFormField>
      <UFormField
        :label="`Schedule (${timezone})`"
        description="Choose when jobs can run for this library."
      >
        <ScheduleEditor v-model="schedule" :error="error" />
      </UFormField>
    </section>

    <section class="space-y-3" aria-labelledby="library-retention-heading">
      <h3
        id="library-retention-heading"
        class="flex items-center gap-2 text-base font-semibold text-highlighted"
      >
        <UIcon name="i-lucide-trash-2" class="size-5" />
        File retention
      </h3>
      <div class="grid gap-4 sm:grid-cols-2">
        <UFormField
          label="Trash mode"
          description="Choose whether to retain originals after verified replacement."
        >
          <USelectMenu
            v-model="trashMode"
            :items="trashModeItems"
            value-key="value"
            class="w-full"
            aria-label="Trash mode"
          />
        </UFormField>

        <UFormField
          label="Trash retention days"
          description="Days to keep replaced files in trash."
          :required="trashMode !== 'none'"
        >
          <UInput
            v-model="trashRetentionDays"
            type="number"
            min="1"
            required
            :disabled="trashMode === 'none'"
            class="w-full"
            aria-label="Trash retention days"
          />
        </UFormField>
      </div>

      <p class="text-sm text-toned">{{ trashModeDescription }}</p>
    </section>

    <DirectoryPicker
      v-model:open="pickerOpen"
      :initial-path="pickerInitialPath"
      @select="selectRoot"
    />
  </form>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import DirectoryPicker from '~/components/DirectoryPicker.vue';
import ScheduleEditor from '~/components/ScheduleEditor.vue';
import { dateTimezone as timezone, formatDateTimeInput, parseDateTimeInput } from '~/utils/date';
import type {
  LibraryBody,
  LibraryRow,
  ProcessingCutoffField,
  ProcessingCutoffMatch,
  ScheduleSpec,
  TrashMode,
} from '~/types/api';

const props = defineProps<{
  library: LibraryRow | null;
  flowItems: Array<{ label: string; value: number }>;
  error?: string | null;
}>();

const emit = defineEmits<{
  submit: [payload: LibraryBody];
}>();

const notify = useNotification();

const name = ref(props.library?.name ?? '');
const roots = ref<string[]>(props.library?.roots.length ? [...props.library.roots] : ['']);
const cooldownS = ref(String(props.library?.cooldown_s ?? 0));
const watchEnabled = ref(props.library?.watch ?? false);
const scanOnStart = ref(props.library?.scan_on_start ?? false);
const initialInterval = props.library ? props.library.scan_interval_s : 3600;
const scanMode = ref(initialInterval === null ? 'off' : 'interval');
const scanUnit = ref(initialInterval !== null && initialInterval % 86400 === 0 ? 86400 : 3600);
const scanCount = ref(String(initialInterval === null ? 1 : initialInterval / scanUnit.value));
const scanModeItems = [
  { label: 'Off', value: 'off' },
  { label: 'Every', value: 'interval' },
];
const scanUnitItems = [
  { label: 'Hours', value: 3600 },
  { label: 'Days', value: 86400 },
];
const mkvStats = ref(props.library?.mkv_stats ?? false);
const healthcheckEnabled = ref(props.library?.healthcheck_enabled ?? false);
const cutoff = props.library?.processing_cutoff;
const initialCutoffInput = formatDateTimeInput(cutoff?.at);
const cutoffInput = ref(initialCutoffInput);
const cutoffFields = ref<ProcessingCutoffField[]>([...(cutoff?.fields ?? ['created_at'])]);
const cutoffMatch = ref<ProcessingCutoffMatch>(cutoff?.match ?? 'all');
const cutoffFieldItems = [
  {
    label: 'Added to Millwright',
    value: 'created_at',
    description: 'When this catalog entry was created. Unchanged by later file edits.',
  },
  {
    label: 'Source first seen',
    value: 'first_seen',
    description: 'Resets when discovery detects a file change.',
  },
  {
    label: 'File modified',
    value: 'mtime',
    description: 'Filesystem modification time. Copying tools can preserve older dates.',
  },
];
const cutoffMatchItems = [
  { label: 'All selected dates must meet the cutoff', value: 'all' },
  { label: 'Any selected date can meet the cutoff', value: 'any' },
];
const trashMode = ref<TrashMode>(props.library?.trash_mode ?? 'none');
const trashRetentionDays = ref(String(props.library?.trash_retention_days ?? 30));
const schedule = ref<ScheduleSpec | null>(props.library?.schedule ?? null);
const flowId = ref(props.library?.flow_id ?? 0);
const flowItems = computed(() => props.flowItems);
const trashModeItems = [
  { label: 'None', value: 'none' },
  { label: 'Transcode', value: 'transcode' },
  { label: 'Transcode + Remux', value: 'all' },
];
const trashModeDescription = computed(() => {
  if (trashMode.value === 'transcode')
    return 'Retains re-encoded originals, but not container or stream-copy remuxes.';
  if (trashMode.value === 'all')
    return 'Retains re-encoded originals and container or stream-copy remuxes.';
  return 'Discards originals after a verified replacement.';
});

const pickerOpen = ref(false);
const pickerIndex = ref<number | null>(null);
const pickerInitialPath = ref('/');
const dirty = useDirtyState(
  computed(() => ({
    name: name.value,
    roots: roots.value,
    cooldownS: cooldownS.value,
    watchEnabled: watchEnabled.value,
    scanOnStart: scanOnStart.value,
    scanMode: scanMode.value,
    scanCount: scanCount.value,
    scanUnit: scanUnit.value,
    mkvStats: mkvStats.value,
    healthcheckEnabled: healthcheckEnabled.value,
    cutoffInput: cutoffInput.value,
    cutoffFields: cutoffFields.value,
    cutoffMatch: cutoffMatch.value,
    trashMode: trashMode.value,
    trashRetentionDays: trashRetentionDays.value,
    schedule: schedule.value,
    flowId: flowId.value,
  })),
);
dirty.markClean();
defineExpose({ isDirty: dirty.isDirty });

const openPicker = (index: number): void => {
  pickerIndex.value = index;
  pickerInitialPath.value =
    roots.value[index]?.trim() ||
    [...roots.value]
      .reverse()
      .find((root) => root.trim())
      ?.trim() ||
    '/';
  pickerOpen.value = true;
};

const selectRoot = (path: string): void => {
  if (pickerIndex.value !== null && roots.value[pickerIndex.value] !== undefined) {
    roots.value[pickerIndex.value] = path;
  }
};

const submitForm = (): void => {
  if (!name.value.trim()) {
    notify.warning('Give the library a name.');
    return;
  }

  const cleanRoots = roots.value.map((root) => root.trim()).filter(Boolean);
  if (!cleanRoots.length) {
    notify.warning('Give the library at least one root directory.');
    return;
  }

  const interval =
    scanMode.value === 'off' ? null : Math.round(Number(scanCount.value) * scanUnit.value);
  if (interval !== null && (!Number.isSafeInteger(interval) || interval < 1)) {
    notify.warning('Enter a positive scan interval in hours or days.');
    return;
  }

  const retentionDays = Number(trashRetentionDays.value);
  if (trashMode.value !== 'none' && (!Number.isInteger(retentionDays) || retentionDays < 1)) {
    notify.warning('Enter a whole number of trash retention days, at least 1.');
    return;
  }

  let cutoffAt: string | null = null;
  if (cutoffInput.value) {
    const date = parseDateTimeInput(cutoffInput.value);
    if (!date) {
      notify.warning(
        `Enter a valid cutoff date and time in ${timezone.value}. Times skipped by a daylight-saving change are not valid.`,
      );
      return;
    }
    if (!cutoffFields.value.length) {
      notify.warning('Select at least one cutoff comparison date.');
      return;
    }
    // Keep the saved precision when the date input has not been edited.
    cutoffAt =
      cutoffInput.value === initialCutoffInput && cutoff?.at ? cutoff.at : date.toISOString();
  }

  emit('submit', {
    name: name.value.trim(),
    roots: cleanRoots,
    cooldown_s: Math.max(0, Number(cooldownS.value) || 0),
    schedule: schedule.value,
    watch: watchEnabled.value,
    scan_on_start: scanOnStart.value,
    scan_interval_s: interval,
    mkv_stats: mkvStats.value,
    healthcheck_enabled: healthcheckEnabled.value,
    processing_cutoff: cutoffAt
      ? { at: cutoffAt, fields: [...cutoffFields.value], match: cutoffMatch.value }
      : {},
    trash_mode: trashMode.value,
    trash_retention_days: retentionDays >= 1 ? Math.floor(retentionDays) : 30,
    flow_id: flowId.value === 0 ? null : flowId.value,
  });
};
</script>
