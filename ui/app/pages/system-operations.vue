<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="system-operations" />
    <template v-if="status">
      <div class="grid gap-6 md:grid-cols-2">
        <section class="flex flex-col gap-4 py-4 md:pr-6" aria-labelledby="export-heading">
          <div>
            <h2 id="export-heading" class="font-semibold text-highlighted">
              Export configuration backup
            </h2>
            <p class="mt-1 text-sm text-toned">
              Download an encrypted backup of server configuration and settings.
            </p>
          </div>
          <UButton class="self-start" icon="i-lucide-download" @click="exportOpen = true"
            >Export configuration backup</UButton
          >
        </section>
        <section
          class="flex flex-col gap-4 border-t border-default py-4 md:border-t-0 md:border-l md:pl-6"
          aria-labelledby="restore-heading"
        >
          <div>
            <h2 id="restore-heading" class="font-semibold text-highlighted">
              Import configuration backup
            </h2>
            <p class="mt-1 text-sm text-toned">
              Preview a configuration backup and choose which settings to restore.
            </p>
          </div>
          <UButton class="self-start" icon="i-lucide-rotate-ccw" @click="restoreOpen = true"
            >Import configuration backup</UButton
          >
        </section>
        <section
          class="flex flex-col gap-4 border-t border-default py-4 md:border-l md:pl-6"
          aria-labelledby="reset-heading"
        >
          <div>
            <h2 id="reset-heading" class="font-semibold text-highlighted">Reset data</h2>
            <p class="mt-1 text-sm text-toned">
              Clear job history, the file index, processing decisions, and selected settings.
            </p>
          </div>
          <UButton
            class="self-start"
            color="error"
            icon="i-lucide-trash-2"
            @click="resetOpen = true"
            >Reset data</UButton
          >
        </section>
      </div>
      <UModal
        :open="resetOpen"
        title="Reset data"
        :dismissible="busy !== 'reset'"
        :ui="{ content: 'w-full sm:max-w-lg' }"
        @update:open="setResetOpen"
      >
        <template #body>
          <div class="space-y-4">
            <UAlert color="warning" icon="i-lucide-triangle-alert"
              ><template #description
                ><ul class="list-disc space-y-1 pl-5">
                  <li>
                    Unchecked categories will be deleted. Job history, the file index, and
                    processing decisions will always be cleared. A configuration backup can't
                    restore the cleared history or index.
                  </li>
                  <li>
                    <strong class="font-semibold">Reset won't delete your media files.</strong>
                  </li>
                  <li>
                    Retained libraries stay suspended until reviewed and resumed on the Libraries
                    page. Clearing processing decisions means existing media can be processed again
                    after resume.
                  </li>
                  <li>Trash stays under its existing retention schedule.</li>
                  <li>Configuration, secrets, and login sessions stay.</li>
                  <li>Busy node work blocks reset.</li>
                </ul></template
              ></UAlert
            >
            <div class="flex flex-wrap items-center justify-between gap-3">
              <h3 class="font-semibold text-highlighted">Categories to keep</h3>
              <UButton
                size="sm"
                color="neutral"
                variant="outline"
                :icon="
                  resetKeep.length === resetItems.length
                    ? 'i-lucide-square'
                    : 'i-lucide-square-check-big'
                "
                :disabled="busy !== null || !resetItems.length"
                :aria-label="`${resetKeep.length === resetItems.length ? 'Unselect' : 'Select'} all reset categories`"
                @click="
                  toggleAll(
                    resetKeep,
                    resetItems.map((item) => item.value),
                  )
                "
              >
                Select
              </UButton>
            </div>
            <UCheckboxGroup v-model="resetKeep" :items="resetItems" class="mw-frame p-3" />
            <p v-if="resetError" class="text-sm text-error">{{ resetError }}</p>
          </div>
        </template>
        <template #footer
          ><div class="flex w-full justify-end gap-2">
            <UButton
              color="error"
              icon="i-lucide-trash-2"
              :loading="busy === 'reset'"
              :disabled="busy !== null"
              @click="resetData"
              >Reset data</UButton
            >
          </div></template
        >
      </UModal>
      <UAlert v-if="message" color="success" icon="i-lucide-circle-check" :description="message" />
      <UModal
        :open="exportOpen"
        title="Export configuration backup"
        description="Choose a password to encrypt the downloaded file."
        :dismissible="busy !== 'export'"
        :ui="{ content: 'w-full sm:max-w-lg' }"
        @update:open="setExportOpen"
      >
        <template #body>
          <form id="backupExportForm" class="space-y-4" @submit.prevent="exportBackup">
            <UAlert
              color="warning"
              icon="i-lucide-triangle-alert"
              title="Not backed up"
              description="jobs, trash, media files."
            />
            <UFormField label="Backup password" required>
              <PasswordInput
                v-model="exportPassword"
                class="w-full"
                autocomplete="new-password"
                required
                :minlength="8"
              />
            </UFormField>
            <UFormField label="Confirm backup password" required>
              <PasswordInput
                v-model="confirmExportPassword"
                class="w-full"
                autocomplete="new-password"
                required
                :minlength="8"
              />
            </UFormField>
            <p v-if="error" class="text-sm text-error">{{ error }}</p>
          </form>
        </template>
        <template #footer>
          <div class="flex w-full justify-end gap-2">
            <UButton
              type="submit"
              form="backupExportForm"
              icon="i-lucide-download"
              :loading="busy === 'export'"
            >
              Download configuration backup
            </UButton>
          </div>
        </template>
      </UModal>

      <UModal
        :open="restoreOpen"
        title="Import configuration backup"
        :dismissible="busy !== 'preview' && busy !== 'restore'"
        :ui="{
          content: 'w-full sm:max-w-2xl',
          body: 'max-h-[70vh] overflow-y-auto p-4 sm:p-6',
        }"
        @update:open="setRestoreOpen"
      >
        <template #body>
          <form
            v-show="stage === 'file'"
            id="backupPreviewForm"
            class="space-y-4"
            @submit.prevent="previewBackup"
          >
            <UFormField label="Configuration backup file" required>
              <input
                :key="fileKey"
                class="block w-full text-sm"
                type="file"
                accept=".mwbackup"
                :disabled="busy !== null"
                required
                @change="setFile"
              />
            </UFormField>
            <UFormField label="Backup password" required>
              <PasswordInput
                v-model="importPassword"
                class="w-full"
                autocomplete="current-password"
                :disabled="busy !== null"
                required
              />
            </UFormField>
          </form>

          <div v-if="stage === 'select' && preview" class="space-y-5">
            <UFormField
              label="Backup password"
              required
              :help="
                !importPassword ? 'Re-enter the password to restore your selections.' : undefined
              "
            >
              <PasswordInput
                v-model="importPassword"
                class="w-full"
                autocomplete="current-password"
                :disabled="busy !== null"
              />
            </UFormField>
            <p
              v-if="
                !preview.flows.length &&
                !preview.libraries.length &&
                !preview.notifications.length &&
                !preview.webhooks.length &&
                !preview.plugins.length &&
                !preview.nodes.length &&
                !preview.config &&
                !preview.settings
              "
              class="text-sm text-toned"
            >
              This configuration backup has no restorable items.
            </p>
            <UAlert
              color="warning"
              icon="i-lucide-triangle-alert"
              title="Not backed up"
              description="jobs, trash, media files."
            />
            <section v-if="preview.flows.length" class="space-y-3">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <h3 class="flex items-center gap-3 font-semibold text-highlighted">
                  <span class="mw-detail-icon"
                    ><UIcon name="i-lucide-git-branch" class="size-4"
                  /></span>
                  Flows
                </h3>
                <UButton
                  size="sm"
                  color="neutral"
                  variant="outline"
                  :icon="
                    flowToggleLabel === 'Unselect' ? 'i-lucide-square' : 'i-lucide-square-check-big'
                  "
                  :disabled="busy !== null || !preview.flows.length"
                  :aria-label="`${flowToggleLabel}${flowToggleLabel === 'Unselect' && requiredFlows.size ? ' optional' : ''} flows`"
                  @click="toggleAll(selectedFlows, preview.flows, [...requiredFlows])"
                >
                  {{ flowToggleLabel }}
                </UButton>
              </div>
              <UCheckboxGroup
                v-model="selectedFlows"
                :items="
                  preview.flows.map((name) => ({
                    label: name,
                    value: name,
                    disabled: requiredFlows.has(name),
                    description: requiredFlows.has(name)
                      ? 'Required by a selected library'
                      : undefined,
                  }))
                "
                class="mw-frame p-3"
              />
            </section>
            <section v-if="preview.libraries.length" class="space-y-3">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <h3 class="flex items-center gap-3 font-semibold text-highlighted">
                  <span class="mw-detail-icon"
                    ><UIcon name="i-lucide-folder" class="size-4"
                  /></span>
                  Libraries
                </h3>
                <UButton
                  size="sm"
                  color="neutral"
                  variant="outline"
                  :icon="
                    selectedLibraries.length === libraryNames.length
                      ? 'i-lucide-square'
                      : 'i-lucide-square-check-big'
                  "
                  :disabled="busy !== null || !libraryNames.length"
                  :aria-label="`${selectedLibraries.length === libraryNames.length ? 'Unselect' : 'Select'} all libraries`"
                  @click="
                    setLibraries(
                      selectedLibraries.length === libraryNames.length ? [] : libraryNames,
                    )
                  "
                >
                  Select
                </UButton>
              </div>
              <div class="mw-frame space-y-3 p-3">
                <UCheckboxGroup
                  v-model="selectedLibraries"
                  :items="
                    preview.libraries.map((item) => ({
                      label: item.name,
                      value: item.name,
                      disabled: Boolean(item.flow_name && !flowNames.includes(item.flow_name)),
                      description: item.flow_name
                        ? flowNames.includes(item.flow_name)
                          ? `Flow: ${item.flow_name}`
                          : `Flow missing from backup: ${item.flow_name}`
                        : undefined,
                    }))
                  "
                  @update:model-value="setLibraries"
                />
                <UFormField
                  v-for="item in preview.libraries.filter((item) =>
                    selectedLibraries.includes(item.name),
                  )"
                  :key="item.name"
                  :label="`Roots for ${item.name}`"
                  description="One path per line"
                >
                  <UTextarea v-model="roots[item.name]" class="w-full" :rows="2" />
                </UFormField>
              </div>
            </section>
            <section v-if="preview.notifications.length" class="space-y-3">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <h3 class="flex items-center gap-3 font-semibold text-highlighted">
                  <span class="mw-detail-icon"><UIcon name="i-lucide-bell" class="size-4" /></span>
                  Notification targets
                </h3>
                <UButton
                  size="sm"
                  color="neutral"
                  variant="outline"
                  :icon="
                    selectedNotifications.length === preview.notifications.length
                      ? 'i-lucide-square'
                      : 'i-lucide-square-check-big'
                  "
                  :disabled="busy !== null || !preview.notifications.length"
                  :aria-label="`${selectedNotifications.length === preview.notifications.length ? 'Unselect' : 'Select'} all notification targets`"
                  @click="toggleAll(selectedNotifications, preview.notifications)"
                >
                  Select
                </UButton>
              </div>
              <UCheckboxGroup
                v-model="selectedNotifications"
                :items="preview.notifications"
                class="mw-frame p-3"
              />
            </section>
            <section v-if="preview.webhooks.length" class="space-y-3">
              <h3 class="flex items-center gap-3 font-semibold text-highlighted">
                <span class="mw-detail-icon"><UIcon name="i-lucide-webhook" class="size-4" /></span>
                Incoming webhooks
              </h3>
              <UCheckboxGroup
                v-model="selectedWebhooks"
                :items="preview.webhooks"
                class="mw-frame p-3"
              />
              <p class="text-xs text-toned">
                Includes receiver API keys and path replacements. Review destinations for this
                server.
              </p>
            </section>
            <section v-if="preview.plugins.length" class="space-y-3">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <h3 class="flex items-center gap-3 font-semibold text-highlighted">
                  <span class="mw-detail-icon"
                    ><UIcon name="i-lucide-puzzle" class="size-4"
                  /></span>
                  Plugins
                </h3>
                <UButton
                  size="sm"
                  color="neutral"
                  variant="outline"
                  :icon="
                    selectedPlugins.length === preview.plugins.length
                      ? 'i-lucide-square'
                      : 'i-lucide-square-check-big'
                  "
                  :disabled="busy !== null || !preview.plugins.length"
                  :aria-label="`${selectedPlugins.length === preview.plugins.length ? 'Unselect' : 'Select'} all plugins`"
                  @click="toggleAll(selectedPlugins, preview.plugins)"
                >
                  Select
                </UButton>
              </div>
              <UCheckboxGroup
                v-model="selectedPlugins"
                :items="preview.plugins"
                class="mw-frame p-3"
              />
            </section>
            <section v-if="preview.nodes.length" class="space-y-3">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <h3 class="flex items-center gap-3 font-semibold text-highlighted">
                  <span class="mw-detail-icon"
                    ><UIcon name="i-lucide-server" class="size-4"
                  /></span>
                  Nodes
                </h3>
                <UButton
                  size="sm"
                  color="neutral"
                  variant="outline"
                  :icon="
                    selectedNodes.length === preview.nodes.length
                      ? 'i-lucide-square'
                      : 'i-lucide-square-check-big'
                  "
                  :disabled="busy !== null || !preview.nodes.length"
                  :aria-label="`${selectedNodes.length === preview.nodes.length ? 'Unselect' : 'Select'} all nodes`"
                  @click="
                    toggleAll(
                      selectedNodes,
                      preview.nodes.map((node) => node.value),
                    )
                  "
                >
                  Select
                </UButton>
              </div>
              <UCheckboxGroup v-model="selectedNodes" :items="preview.nodes" class="mw-frame p-3" />
            </section>
            <section v-if="preview.settings" class="space-y-3">
              <h3 class="flex items-center gap-3 font-semibold text-highlighted">
                <span class="mw-detail-icon"
                  ><UIcon name="i-lucide-sliders-horizontal" class="size-4" /></span
                >Settings
              </h3>
              <div class="mw-frame p-3">
                <UCheckbox v-model="selectedSettings" label="Server settings" />
              </div>
            </section>
            <section v-if="preview.config" class="space-y-3">
              <h3 class="flex items-center gap-3 font-semibold text-highlighted">
                <span class="mw-detail-icon"
                  ><UIcon name="i-lucide-settings" class="size-4"
                /></span>
                Configuration
              </h3>
              <div class="mw-frame p-3">
                <UCheckbox v-model="selectedConfig" label="config.toml (includes secrets)" />
              </div>
            </section>
            <section class="space-y-3">
              <h3 class="flex items-center gap-3 font-semibold text-highlighted">
                <span class="mw-detail-icon"
                  ><UIcon name="i-lucide-list-checks" class="size-4"
                /></span>
                Restore options
              </h3>
              <div class="mw-frame p-3">
                <UCheckbox v-model="replace" label="Replace existing configuration" />
              </div>
            </section>
          </div>
          <p v-if="error" class="mt-4 text-sm text-error">{{ error }}</p>
        </template>
        <template #footer>
          <div class="flex w-full justify-end gap-2">
            <UButton
              v-if="stage === 'file'"
              type="submit"
              form="backupPreviewForm"
              icon="i-lucide-eye"
              color="neutral"
              variant="outline"
              :loading="busy === 'preview'"
              :disabled="busy !== null"
            >
              Preview configuration backup
            </UButton>
            <UButton
              v-else
              icon="i-lucide-rotate-ccw"
              :loading="busy === 'restore'"
              :disabled="!hasSelection || !importPassword || busy !== null"
              @click="restoreBackup"
            >
              Restore selected configuration
            </UButton>
          </div>
        </template>
      </UModal>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue';
import { useAuth } from '~/composables/useAuth';
import { useBackup } from '~/composables/useBackup';
import { useDialog } from '~/composables/useDialog';
import type { BackupPreview } from '~/types/api';

const { status } = useAuth();
const { confirmDialog, alertDialog } = useDialog();
const resetOpen = ref(false);
const resetKeep = ref<string[]>([]);
const resetError = ref('');
const toast = useToast();
const {
  reset,
  exportBackup: requestExport,
  preview: requestPreview,
  restore: requestRestore,
} = useBackup();
const exportOpen = ref(false);
const restoreOpen = ref(false);
const exportPassword = ref('');
const confirmExportPassword = ref('');
const importPassword = ref('');
const file = ref<File | null>(null);
const fileKey = ref(0);
const stage = ref<'file' | 'select'>('file');
const preview = ref<BackupPreview | null>(null);
const selectedFlows = ref<string[]>([]);
const selectedLibraries = ref<string[]>([]);
const selectedNotifications = ref<string[]>([]);
const selectedWebhooks = ref<string[]>([]);
const selectedPlugins = ref<string[]>([]);
const selectedNodes = ref<string[]>([]);
const selectedConfig = ref(false);
const selectedSettings = ref(false);
const roots = reactive<Record<string, string>>({});
const replace = ref(false);
const busy = ref<'export' | 'preview' | 'restore' | 'reset' | null>(null);
const error = ref('');
const message = ref('');

const resetItems = [
  {
    value: 'settings',
    label: 'Settings',
  },
  {
    value: 'webhooks',
    label: 'Incoming webhooks',
    description: 'Registrations, API keys, and path replacements',
  },
  { value: 'flows', label: 'Flows', description: 'Saved definitions' },
  {
    value: 'libraries',
    label: 'Libraries',
    description: 'Roots and schedules. Removing flows also removes their library assignments.',
  },
  {
    value: 'nodes',
    label: 'Nodes',
    description: 'Registrations and credentials',
  },
  {
    value: 'notifications',
    label: 'Notifications',
    description: 'Notification targets',
  },
  {
    value: 'plugins',
    label: 'Plugins',
    description: 'Uploaded plugins and enabled state',
  },
];

const setResetOpen = (open: boolean): void => {
  if (!open && busy.value !== null) return;
  resetOpen.value = open;
  if (!open) {
    resetKeep.value = [];
    resetError.value = '';
  }
};

const resetData = async (): Promise<void> => {
  const kept =
    resetItems
      .filter((item) => resetKeep.value.includes(item.value))
      .map((item) => item.label)
      .join(', ') || 'none';
  if (
    !(
      await confirmDialog({
        title: 'Confirm reset',
        message: `Keep: ${kept}.\n\nDelete unchecked categories and clear job history, the file index, and processing decisions. A configuration backup can't restore the cleared history or index.\n\nReset won't delete your media files. Retained libraries stay suspended until reviewed and resumed on the Libraries page. Clearing processing decisions means existing media can be processed again after resume.\n\nContinue?`,
        confirmColor: 'error',
      })
    ).status
  )
    return;
  busy.value = 'reset';
  resetError.value = '';
  try {
    const result = await reset(resetKeep.value);
    if (!result.ok) {
      resetError.value =
        result.status === 409
          ? 'Reset could not start because the server is busy or another reset is in progress. Wait and try again.'
          : result.status === 500
            ? 'Reset may be partial, including uploaded plugin cleanup. Review the server before retrying.'
            : result.status === null
              ? 'Reset outcome is unknown. Review the current settings before retrying.'
              : 'Data reset failed.';
      return;
    }
    toast.add({
      title: 'Data reset completed.',
      color: 'success',
      icon: 'i-lucide-circle-check',
    });
    busy.value = null;
    setResetOpen(false);
    window.location.reload();
  } catch {
    resetError.value = 'Reset outcome is unknown. Review the current settings before retrying.';
  } finally {
    busy.value = null;
  }
};

const hasSelection = computed(
  () =>
    selectedFlows.value.length > 0 ||
    selectedLibraries.value.length > 0 ||
    selectedNotifications.value.length > 0 ||
    selectedWebhooks.value.length > 0 ||
    selectedPlugins.value.length > 0 ||
    selectedNodes.value.length > 0 ||
    selectedConfig.value ||
    selectedSettings.value,
);
const flowNames = computed(() => preview.value?.flows ?? []);
const libraryNames = computed(
  () =>
    preview.value?.libraries
      .filter((item) => !item.flow_name || flowNames.value.includes(item.flow_name))
      .map((item) => item.name) ?? [],
);
const requiredFlows = computed(
  () =>
    new Set(
      preview.value?.libraries
        .filter((item) => selectedLibraries.value.includes(item.name))
        .map((item) => item.flow_name)
        .filter((name): name is string => Boolean(name)) ?? [],
    ),
);
const flowToggleLabel = computed(() =>
  selectedFlows.value.length !== flowNames.value.length ? 'Select' : 'Unselect',
);

const toggleAll = (selected: string[], names: string[], required: string[] = []): void => {
  selected.splice(0, selected.length, ...(selected.length === names.length ? required : names));
};

const setLibraries = (names: string[]): void => {
  selectedLibraries.value = names;
  for (const name of requiredFlows.value)
    if (!selectedFlows.value.includes(name)) selectedFlows.value.push(name);
};

onBeforeUnmount(() => clearSecrets());

const clearSecrets = (): void => {
  exportPassword.value = '';
  confirmExportPassword.value = '';
  importPassword.value = '';
};

const clearPreview = (): void => {
  preview.value = null;
  selectedFlows.value = [];
  selectedLibraries.value = [];
  selectedNotifications.value = [];
  selectedWebhooks.value = [];
  selectedPlugins.value = [];
  selectedNodes.value = [];
  selectedConfig.value = false;
  selectedSettings.value = false;
  replace.value = false;
  Object.keys(roots).forEach((name) => {
    roots[name] = '';
  });
};

const setExportOpen = (open: boolean): void => {
  if (!open && busy.value !== null) return;
  exportOpen.value = open;
  if (!open) {
    exportPassword.value = '';
    confirmExportPassword.value = '';
    error.value = '';
  }
};

const setRestoreOpen = (open: boolean): void => {
  if (!open && busy.value !== null) return;
  restoreOpen.value = open;
  if (!open) {
    importPassword.value = '';
    file.value = null;
    fileKey.value += 1;
    stage.value = 'file';
    clearPreview();
    error.value = '';
  }
};

const setFile = (event: Event): void => {
  if (busy.value !== null) return;
  file.value = (event.target as HTMLInputElement).files?.[0] ?? null;
  importPassword.value = '';
  stage.value = 'file';
  clearPreview();
  error.value = '';
  message.value = '';
};

const fail = (status: number | null, uncertain = false, reason?: string, detail?: string): void => {
  if (detail) {
    error.value =
      status === 409 && detail.includes('already exists')
        ? `${detail} Select Replace existing configuration or deselect it.`
        : status !== null && status >= 500
          ? `${detail} Review the current settings before retrying.`
          : detail;
    return;
  }
  error.value =
    status === null
      ? uncertain
        ? 'Restore outcome is unknown. Review the current settings before retrying.'
        : reason === 'decode'
          ? 'Invalid configuration backup response. Try again or check the server logs.'
          : 'Configuration backup request failed. Check your connection and try again.'
      : status === 409
        ? 'Configuration backup request conflicts with the current server state. Review the current settings before retrying.'
        : status === 500
          ? 'Restore may be partial. Review the current settings before retrying.'
          : 'Configuration backup request failed. Check the file, password, paths, and selections.';
};

const exportBackup = async (): Promise<void> => {
  if (exportPassword.value !== confirmExportPassword.value) {
    error.value = 'Backup passwords do not match.';
    return;
  }
  busy.value = 'export';
  error.value = '';
  message.value = '';
  try {
    const result = await requestExport(exportPassword.value);
    if (!result.ok) {
      fail(result.status, false, result.reason, result.detail);
      return;
    }
    const url = URL.createObjectURL(result.data.blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'millwright.mwbackup';
    link.click();
    URL.revokeObjectURL(url);
    toast.add({
      title: 'Configuration backup downloaded.',
      color: 'success',
      icon: 'i-lucide-circle-check',
    });
    exportOpen.value = false;
  } catch {
    error.value = 'Configuration backup export failed.';
  } finally {
    busy.value = null;
    clearSecrets();
  }
};

const previewBackup = async (): Promise<void> => {
  if (busy.value !== null || !file.value || !importPassword.value) return;
  busy.value = 'preview';
  error.value = '';
  message.value = '';
  clearPreview();
  try {
    const result = await requestPreview(file.value, importPassword.value);
    if (!result.ok) {
      fail(result.status, false, result.reason, result.detail);
      return;
    }
    preview.value = result.data;
    selectedFlows.value = [];
    selectedLibraries.value = [];
    selectedNotifications.value = [];
    selectedWebhooks.value = [];
    selectedPlugins.value = [];
    selectedNodes.value = [];
    preview.value.libraries.forEach((item) => {
      roots[item.name] = item.roots.join('\n');
    });
    stage.value = 'select';
  } catch {
    error.value = 'Configuration backup preview failed.';
  } finally {
    busy.value = null;
  }
};

const restoreBackup = async (): Promise<void> => {
  if (stage.value !== 'select' || !file.value || !preview.value || !importPassword.value) return;
  if (
    replace.value &&
    !(
      await confirmDialog({
        title: 'Replace existing configuration?',
        message: 'This will replace selected existing configuration with settings from the backup.',
        confirmColor: 'error',
      })
    ).status
  )
    return;

  busy.value = 'restore';
  error.value = '';
  message.value = '';
  try {
    const rootOverrides: Record<string, string[]> = {};
    selectedLibraries.value.forEach((name) => {
      rootOverrides[name] = (roots[name] ?? '')
        .split('\n')
        .map((root) => root.trim())
        .filter(Boolean);
    });
    const result = await requestRestore(file.value, importPassword.value, {
      flows: selectedFlows.value,
      libraries: selectedLibraries.value,
      notifications: selectedNotifications.value,
      webhooks: selectedWebhooks.value,
      plugins: selectedPlugins.value,
      nodes: selectedNodes.value,
      config: selectedConfig.value,
      settings: selectedSettings.value,
      replace: replace.value,
      roots: rootOverrides,
    });
    if (!result.ok) {
      fail(result.status, true, result.reason, result.detail);
      return;
    }
    message.value = result.data.restartRequired
      ? 'Configuration backup restored. Restart the server to apply configuration and uploaded plugin changes.'
      : 'Selected configuration restored.';
    restoreOpen.value = false;
    stage.value = 'file';
    clearPreview();
    file.value = null;
    fileKey.value += 1;
    clearSecrets();
    if (result.data.restartRequired)
      await alertDialog({
        title: 'Configuration backup restored',
        message: message.value,
        confirmText: 'Reload page',
        confirmIcon: 'i-lucide-refresh-cw',
      });
    window.location.reload();
  } catch {
    error.value = 'Restore outcome is unknown. Review the current settings before retrying.';
  } finally {
    busy.value = null;
    clearSecrets();
  }
};
</script>
