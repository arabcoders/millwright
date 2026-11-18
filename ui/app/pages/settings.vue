<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="settings">
      <template #actions>
        <div class="flex flex-wrap items-center gap-2">
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-refresh-cw"
            :loading="loading"
            :disabled="saving"
            @click="reload"
            >Refresh</UButton
          >
          <UButton
            v-if="draft"
            type="submit"
            form="settingsForm"
            size="sm"
            icon="i-lucide-save"
            :loading="saving"
            :disabled="!isDirty || loading || saving"
            :title="!isDirty ? 'No unsaved changes.' : undefined"
            >Save changes</UButton
          >
        </div>
      </template>
    </PageHeader>
    <UAlert
      v-if="error"
      class="mx-auto w-full max-w-4xl"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Settings request failed"
      :description="error"
    />
    <USkeleton v-if="loading && !draft" class="mx-auto h-80 w-full max-w-4xl rounded-lg" />
    <form
      v-if="draft"
      id="settingsForm"
      class="mx-auto w-full max-w-4xl"
      @submit.prevent="saveSettings"
    >
      <fieldset :disabled="loading || saving" class="min-w-0 space-y-4">
        <section class="mw-card-padded-lg space-y-4">
          <h2 class="flex items-center gap-2 font-semibold text-highlighted">
            <UIcon name="i-lucide-workflow" class="size-5 text-toned" aria-hidden="true" />
            Processing
          </h2>
          <div class="grid gap-4 md:grid-cols-2">
            <UFormField label="Health checks"
              ><USelect v-model="draft.healthcheck_location" :items="locations" class="w-full"
            /></UFormField>
            <UFormField
              label="MKV statistics"
              :help="
                draft.mkv_stats_location !== 'node'
                  ? 'Server modify MKV in-place for statistics.'
                  : undefined
              "
              :ui="{ help: 'text-warning' }"
              ><USelect v-model="draft.mkv_stats_location" :items="locations" class="w-full"
            /></UFormField>
          </div>
          <UCheckbox
            v-model="draft.healthcheck_hwaccel"
            label="Use Hardware acceleration when available"
          />
        </section>
        <section class="mw-card-padded-lg space-y-4">
          <h2 class="flex items-center gap-2 font-semibold text-highlighted">
            <UIcon name="i-lucide-gauge" class="size-5 text-toned" aria-hidden="true" />
            Server limits
          </h2>
          <div class="grid gap-4 sm:grid-cols-2">
            <UFormField
              v-for="field in capacityFields"
              :key="field.key"
              :label="field.label"
              required
            >
              <UInput
                v-model.number="draft[field.key]"
                type="number"
                :min="field.min"
                :step="field.step"
                required
                class="w-full"
              />
            </UFormField>
          </div>
        </section>
        <section class="mw-card-padded-lg space-y-4">
          <h2 class="flex items-center gap-2 font-semibold text-highlighted">
            <UIcon name="i-lucide-monitor-play" class="size-5 text-toned" aria-hidden="true" />
            Playback
          </h2>
          <div class="grid gap-4 sm:grid-cols-2">
            <UFormField
              v-for="field in playbackFields"
              :key="field.key"
              :label="field.label"
              required
            >
              <UInput
                v-model.number="draft.playback[field.key]"
                type="number"
                :min="field.min"
                :step="field.step"
                required
                class="w-full"
              />
            </UFormField>
          </div>
        </section>
        <section class="mw-card-padded-lg space-y-4">
          <h2 class="flex items-center gap-2 font-semibold text-highlighted">
            <UIcon name="i-lucide-list-filter" class="size-5 text-toned" aria-hidden="true" />
            Scanner filters
          </h2>
          <div class="space-y-4">
            <UFormField label="Extensions"
              ><UInput v-model="containers" placeholder="mkv, mp4, avi" class="w-full"
            /></UFormField>
            <UFormField label="Ignored directories" help="One directory name per line."
              ><UTextarea v-model="ignoreDirs" :rows="3" class="w-full"
            /></UFormField>
          </div>
        </section>
        <section class="mw-card-padded-lg space-y-4">
          <h2 class="flex items-center gap-2 font-semibold text-highlighted">
            <UIcon name="i-lucide-history" class="size-5 text-toned" aria-hidden="true" />
            Retention and login sessions
          </h2>
          <div class="grid gap-4 md:grid-cols-2">
            <UFormField
              label="Keep job history (days)"
              help="Zero keeps history indefinitely."
              required
              ><UInput
                v-model.number="draft.job_retention_days"
                type="number"
                :min="0"
                :step="1"
                required
                class="w-full"
            /></UFormField>
            <UFormField label="Login session lifetime (days)" required
              ><UInput
                v-model.number="draft.session_ttl_days"
                type="number"
                :min="1"
                :step="1"
                required
                class="w-full"
            /></UFormField>
          </div>
        </section>
      </fieldset>
    </form>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useSettings } from '~/composables/useSettings';

const { draft, loading, saving, error, isDirty, load, save } = useSettings();
const { confirmDialog } = useDialog();
const notify = useNotification();
useDirtyCloseGuard(ref(true), isDirty, () => {});

const locations = [
  { label: 'Server', value: 'local' },
  { label: 'Nodes', value: 'node' },
  { label: 'Server and nodes', value: 'node_local' },
];
const capacityFields = [
  { key: 'max_healthchecks', label: 'Max health checks', min: 1, step: 1 },
  { key: 'max_probes', label: 'Max metadata probes', min: 1, step: 1 },
  { key: 'max_mkv_stats', label: 'Max MKV statistics', min: 1, step: 1 },
  { key: 'min_free_gb', label: 'Minimum free space (GB)', min: 0, step: 'any' },
] as const;
const playbackFields = [
  { key: 'segment_duration_s', label: 'Segment duration (seconds)', min: 0, step: 'any' },
  { key: 'max_concurrent_segments', label: 'Max concurrent segments', min: 1, step: 1 },
  { key: 'grant_ttl_s', label: 'Session lifetime (seconds)', min: 60, step: 1 },
  { key: 'max_grants', label: 'Session limit', min: 1, step: 1 },
] as const;
const containers = computed({
  get: () => draft.value?.scanner.containers.join(',') ?? '',
  set: (value: string) => {
    if (draft.value) draft.value.scanner.containers = value.split(',');
  },
});
const ignoreDirs = computed({
  get: () => draft.value?.scanner.ignore_dirs.join('\n') ?? '',
  set: (value: string) => {
    if (draft.value) draft.value.scanner.ignore_dirs = value.split('\n');
  },
});
const reload = async (): Promise<void> => {
  if (
    isDirty.value &&
    !(
      await confirmDialog({
        title: 'Discard changes?',
        message: 'Reload settings and discard your unsaved changes?',
        confirmText: 'Discard changes',
        confirmColor: 'warning',
      })
    ).status
  )
    return;
  await load();
};
const saveSettings = async (): Promise<void> => {
  if (!draft.value) return;
  draft.value.scanner.containers = draft.value.scanner.containers
    .map((line) => line.trim())
    .filter(Boolean);
  draft.value.scanner.ignore_dirs = draft.value.scanner.ignore_dirs
    .map((line) => line.trim())
    .filter(Boolean);
  if (await save()) notify.success('Settings saved.');
};
onMounted(() => void load());
</script>
