<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="notifications">
      <template #actions>
        <div class="flex flex-wrap items-center gap-2">
          <UButton
            size="sm"
            color="primary"
            variant="solid"
            icon="i-lucide-plus"
            @click="openEditor()"
            >New target</UButton
          >
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-refresh-cw"
            aria-label="Refresh notification targets"
            :loading="loading"
            @click="loadTargets"
          >
            Refresh
          </UButton>
        </div>
      </template>
    </PageHeader>

    <UAlert
      v-if="lastError"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load notification targets"
      :description="lastError.message"
    />
    <div v-if="loading && !targets.length" class="space-y-3">
      <USkeleton v-for="index in 3" :key="index" class="h-28 rounded-lg" />
    </div>
    <div v-else-if="targets.length" class="mw-card divide-y divide-default" :aria-busy="loading">
      <article
        v-for="target in targets"
        :key="target.name"
        class="grid min-w-0 gap-4 p-4 sm:p-5 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1fr)_auto]"
      >
        <div class="min-w-0">
          <h2 class="wrap-anywhere text-base font-semibold text-highlighted">
            {{ target.name }}
          </h2>
        </div>
        <dl class="grid min-w-0 grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
          <div class="min-w-0 space-y-1">
            <dt class="flex items-center gap-1.5 text-xs text-muted">
              <UIcon name="i-lucide-bell-ring" class="size-3.5 shrink-0" aria-hidden="true" />
              Service
            </dt>
            <dd class="wrap-anywhere">{{ target.url.split('://', 1)[0] }}</dd>
          </div>
          <div class="min-w-0 space-y-1">
            <dt class="flex items-center gap-1.5 text-xs text-muted">
              <UIcon name="i-lucide-clock-3" class="size-3.5 shrink-0" aria-hidden="true" />
              Created
            </dt>
            <dd>
              <UTooltip :text="formatDate(target.created_at, 'detail')">
                <time :datetime="toIsoString(target.created_at)">{{
                  formatRelativeTime(target.created_at, 'en')
                }}</time>
              </UTooltip>
            </dd>
          </div>
          <div class="min-w-0 space-y-1">
            <dt class="flex items-center gap-1.5 text-xs text-muted">
              <UIcon name="i-lucide-history" class="size-3.5 shrink-0" aria-hidden="true" />
              Updated
            </dt>
            <dd>
              <UTooltip :text="formatDate(target.updated_at, 'detail')">
                <time :datetime="toIsoString(target.updated_at)">{{
                  formatRelativeTime(target.updated_at, 'en')
                }}</time>
              </UTooltip>
            </dd>
          </div>
        </dl>
        <div class="flex flex-wrap items-start justify-end gap-1.5">
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-pen-line"
            @click="openEditor(target)"
          >
            Edit
          </UButton>
          <UDropdownMenu :items="targetActions(target)" :modal="false" :content="{ align: 'end' }">
            <UButton
              color="neutral"
              variant="ghost"
              size="sm"
              icon="i-lucide-ellipsis"
              square
              :aria-label="`Actions for ${target.name}`"
              :loading="deleting === target.name || testing === target.name"
              :disabled="deleting !== null || testing !== null"
              :title="testing !== null ? 'Wait for the test notification to finish.' : undefined"
            />
          </UDropdownMenu>
        </div>
      </article>
    </div>
    <UEmpty
      v-else-if="!lastError"
      icon="i-lucide-bell"
      title="No notification targets configured yet."
    />

    <NotificationTargetEditorModal
      v-model:open="editorOpen"
      :target="editingTarget"
      :saving="saving"
      :save-error="saveError"
      @save="saveTarget"
    />
  </main>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import type { DropdownMenuItem } from '@nuxt/ui';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import { useDialog } from '~/composables/useDialog';
import { useNotificationTargets } from '~/composables/useNotificationTargets';
import type { MutationResult, Target } from '~/composables/useNotificationTargets';
import { formatDate, toIsoString } from '~/utils/date';
import { formatRelativeTime } from '~/utils/relativeTime';

const notify = useNotification();
const { messageFor } = useApiErrorMessage();
const { confirmDialog } = useDialog();
const {
  items: targets,
  isLoading: loading,
  lastError,
  ensure,
  reload,
  save,
  test,
  remove,
} = useNotificationTargets();

const notifyMutationError = (result: MutationResult): void => {
  notify.error(messageFor(result.detail));
};
const editingTarget = ref<Target | null>(null);
const editorOpen = ref(false);
const saving = ref(false);
const deleting = ref<string | null>(null);
const testing = ref<string | null>(null);
const saveError = ref<string | null>(null);

const loadTargets = async (): Promise<void> => {
  try {
    await reload();
  } catch (error) {
    notify.error(
      messageFor(
        error instanceof Error ? error.message : undefined,
        'Could not load notification targets.',
      ),
    );
  }
};

const openEditor = (target?: Target): void => {
  if (saving.value) return;
  editingTarget.value = target ? { ...target } : null;
  saveError.value = null;
  editorOpen.value = true;
};

const saveTarget = async ({ name, url }: { name: string; url: string }): Promise<void> => {
  if (saving.value) return;
  saving.value = true;
  saveError.value = null;
  try {
    const saved = await save(name, url, editingTarget.value);
    if (!saved.ok) {
      if (saved.status !== 422) notifyMutationError(saved);
      saveError.value =
        saved.status === 422
          ? 'Invalid notification name or Apprise URL. Check both fields and try again.'
          : (saved.detail ?? 'Could not save the notification target.');
      return;
    }
    notify.success('Notification target saved.');
    editorOpen.value = false;
    if (saved.refreshError) notify.warning(saved.refreshError);
  } catch {
    saveError.value = 'Could not save the notification target.';
  } finally {
    saving.value = false;
  }
};

const testTarget = async (target: Target): Promise<void> => {
  if (testing.value !== null || deleting.value !== null) return;
  testing.value = target.name;
  try {
    const result = await test(target.name);
    if (result.ok) notify.success('Test notification sent.');
    else notifyMutationError(result);
  } catch (error) {
    notify.error(
      messageFor(
        error instanceof Error ? error.message : undefined,
        'Could not send the test notification.',
      ),
    );
  } finally {
    testing.value = null;
  }
};

const deleteTarget = async (target: Target): Promise<void> => {
  if (testing.value !== null || deleting.value !== null) return;
  deleting.value = target.name;
  try {
    const { status } = await confirmDialog({
      title: 'Delete notification target',
      message: `Delete "${target.name}"?`,
      confirmText: 'Delete',
      confirmColor: 'error',
      confirmIcon: 'i-lucide-trash-2',
    });
    if (!status) return;
    const removed = await remove(target.name);
    if (!removed.ok) {
      notifyMutationError(removed);
      return;
    }
    notify.success('Notification target deleted.');
    if (removed.refreshError) notify.warning(removed.refreshError);
    if (editingTarget.value?.name === target.name) editorOpen.value = false;
  } catch (error) {
    notify.error(
      messageFor(
        error instanceof Error ? error.message : undefined,
        'Could not delete the notification target.',
      ),
    );
  } finally {
    deleting.value = null;
  }
};

const targetActions = (target: Target): DropdownMenuItem[][] => [
  [
    {
      label: 'Test',
      icon: 'i-lucide-send',
      disabled: testing.value !== null || deleting.value !== null,
      onSelect: () => testTarget(target),
    },
  ],
  [
    {
      label: 'Delete',
      icon: 'i-lucide-trash-2',
      color: 'error',
      disabled: testing.value !== null || deleting.value !== null,
      onSelect: () => deleteTarget(target),
    },
  ],
];

onMounted(
  () =>
    void ensure().catch((error) =>
      notify.error(
        messageFor(
          error instanceof Error ? error.message : undefined,
          'Could not load notification targets.',
        ),
      ),
    ),
);
</script>
