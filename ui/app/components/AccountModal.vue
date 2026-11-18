<template>
  <UModal
    :open="open"
    title="Account"
    :dismissible="!busy"
    :close="{ disabled: busy }"
    :ui="{ content: 'w-full sm:max-w-md', body: 'max-h-[80vh] overflow-y-auto p-4 sm:p-5' }"
    @update:open="handleOpenChange"
    @after:leave="clearForm"
  >
    <template #body>
      <div class="space-y-4">
        <section class="mw-card-padded-lg space-y-4">
          <div class="flex items-center gap-2">
            <UIcon name="i-lucide-user-round" class="size-5 text-toned" aria-hidden="true" />
            <p class="text-base font-semibold text-highlighted">Account details</p>
          </div>
          <dl>
            <div class="min-w-0 space-y-1">
              <dt class="text-xs font-medium text-muted">Username</dt>
              <dd class="wrap-anywhere text-sm font-medium text-highlighted">
                <bdi>{{ username }}</bdi>
              </dd>
            </div>
          </dl>
          <UBadge v-if="apiKey" color="neutral" variant="soft" size="sm"> API key access </UBadge>
        </section>
        <form
          v-if="!apiKey"
          id="changePasswordForm"
          class="mw-card-padded-lg space-y-4"
          @submit.prevent="changePassword"
        >
          <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
            <UIcon name="i-lucide-key-round" class="size-5 text-toned" aria-hidden="true" />
            Change password
          </h3>
          <UFormField label="Current password" required>
            <PasswordInput
              v-model="currentPassword"
              autocomplete="current-password"
              :maxlength="1024"
              :disabled="busy"
              required
              class="w-full"
            />
          </UFormField>
          <UFormField label="New password" required>
            <PasswordInput
              v-model="password"
              autocomplete="new-password"
              :maxlength="1024"
              :disabled="busy"
              required
              class="w-full"
            />
          </UFormField>
          <UFormField label="Confirm new password" required>
            <PasswordInput
              v-model="confirmation"
              autocomplete="new-password"
              :maxlength="1024"
              :disabled="busy"
              required
              class="w-full"
            />
          </UFormField>
          <UAlert
            v-if="saveError"
            color="error"
            variant="soft"
            icon="i-lucide-circle-alert"
            :description="saveError"
            :ui="{ description: 'wrap-anywhere' }"
          />
        </form>
      </div>
    </template>

    <template #footer>
      <div class="flex w-full flex-wrap items-center justify-between gap-2">
        <UButton
          color="error"
          variant="outline"
          size="sm"
          icon="i-lucide-log-out"
          :loading="loggingOut"
          :disabled="busy"
          v-if="!apiKey"
          @click="logout"
        >
          Log out
        </UButton>
        <UButton
          v-if="!apiKey"
          type="submit"
          form="changePasswordForm"
          size="sm"
          icon="i-lucide-save"
          :loading="saving"
          :disabled="busy"
        >
          Change password
        </UButton>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { useAuth } from '~/composables/useAuth';
import { to_api_error } from '~/utils';

const open = defineModel<boolean>('open', { default: false });
const auth = useAuth();
const notify = useNotification();
const loggingOut = ref(false);
const saving = ref(false);
const busy = computed(() => saving.value || loggingOut.value);
const currentPassword = ref('');
const password = ref('');
const confirmation = ref('');
const saveError = ref('');
const dirty = useDirtyState(
  computed(() => ({
    currentPassword: currentPassword.value,
    password: password.value,
    confirmation: confirmation.value,
  })),
);
dirty.markClean();
const { handleOpenChange } = useDirtyCloseGuard(open, dirty.isDirty, () => {
  if (!busy.value) open.value = false;
});

const me = computed(() => auth.status.value);
const apiKey = computed(() => me.value?.auth_method === 'api_key');
const username = computed(() => me.value?.username || 'Unknown');

const clearForm = (): void => {
  currentPassword.value = '';
  password.value = '';
  confirmation.value = '';
  saveError.value = '';
  dirty.markClean();
};

const changePassword = async (): Promise<void> => {
  if (busy.value) return;
  saveError.value = '';
  if (password.value !== confirmation.value) {
    saveError.value = 'Passwords do not match.';
    return;
  }
  saving.value = true;
  try {
    await auth.changePassword(currentPassword.value, password.value);
    clearForm();
    notify.success('Password changed.');
  } catch (error) {
    saveError.value = to_api_error(error).message;
  } finally {
    saving.value = false;
  }
};

const logout = async (): Promise<void> => {
  if (busy.value) return;
  loggingOut.value = true;
  try {
    await auth.logout();
    open.value = false;
    await navigateTo('/login');
  } catch (error) {
    notify.error(`Could not confirm logout. ${to_api_error(error).message}`);
  } finally {
    loggingOut.value = false;
  }
};
</script>
