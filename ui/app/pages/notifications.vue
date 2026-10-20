<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="notifications">
      <template #actions>
        <div class="flex flex-wrap items-center gap-2">
          <UButton
            size="sm"
            color="neutral"
            variant="outline"
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

    <div v-if="loading && !targets.length" class="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      <USkeleton v-for="index in 3" :key="index" class="h-52 rounded-lg" />
    </div>
    <div v-else-if="targets.length" class="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      <article
        v-for="target in targets"
        :key="target.name"
        class="mw-card flex h-full flex-col gap-3 p-4"
      >
        <div class="min-w-0">
          <h2 class="truncate font-semibold text-highlighted">
            {{ target.name }}
          </h2>
        </div>
        <dl class="grid grid-cols-2 gap-x-3 gap-y-2 text-xs xl:grid-cols-3">
          <div>
            <dt class="text-dimmed">Service</dt>
            <dd class="mt-0.5 text-toned">{{ target.url.split('://', 1)[0] }}</dd>
          </div>
          <div>
            <dt class="text-dimmed">Created</dt>
            <dd class="mt-0.5 text-toned">
              <UTooltip :text="formatDate(target.created_at, 'detail')">
                <time :datetime="toIsoString(target.created_at)">{{
                  formatRelativeTime(target.created_at, 'en')
                }}</time>
              </UTooltip>
            </dd>
          </div>
          <div>
            <dt class="text-dimmed">Updated</dt>
            <dd class="mt-0.5 text-toned">
              <UTooltip :text="formatDate(target.updated_at, 'detail')">
                <time :datetime="toIsoString(target.updated_at)">{{
                  formatRelativeTime(target.updated_at, 'en')
                }}</time>
              </UTooltip>
            </dd>
          </div>
        </dl>
        <div class="mt-auto flex items-center justify-end gap-2">
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-pen-line"
            @click="openEditor(target)"
          >
            Edit
          </UButton>
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-send"
            :loading="testing === target.name"
            :disabled="testing !== null || deleting !== null"
            @click="testTarget(target)"
          >
            Test
          </UButton>
          <UButton
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-trash-2"
            :loading="deleting === target.name"
            :disabled="deleting !== null || testing !== null"
            :title="testing !== null ? 'Wait for the test notification to finish.' : undefined"
            @click="deleteTarget(target)"
          >
            Delete
          </UButton>
        </div>
      </article>
    </div>
    <div v-else class="mw-card flex flex-col items-center justify-center gap-2 py-10 text-center">
      <UIcon name="i-lucide-bell" class="size-7 text-toned" />
      <p class="text-sm text-toned">No notification targets configured yet.</p>
    </div>

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
