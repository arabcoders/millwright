<template>
  <UModal
    v-model:open="open"
    title="Account"
    :dismissible="!busy"
    :close="{ disabled: busy }"
    :ui="{ content: 'max-w-md' }"
    @after:leave="clearForm"
  >
    <template #body>
      <div class="space-y-5">
        <section class="space-y-3">
          <div class="flex items-center gap-2">
            <UIcon name="i-lucide-user-round" class="size-5" />
            <p class="text-base font-semibold text-highlighted">Account details</p>
          </div>
          <div class="divide-y divide-default rounded-sm border border-default bg-elevated/40">
            <div class="flex items-center gap-3 p-3">
              <UIcon name="i-lucide-user" class="size-4 shrink-0 text-toned" />
              <div class="min-w-0">
                <p class="text-xs font-medium text-toned">Username</p>
                <p class="truncate font-semibold text-highlighted">{{ username }}</p>
              </div>
            </div>
          </div>
          <UBadge v-if="apiKey" color="neutral" variant="soft" size="sm"> API key access </UBadge>
        </section>
        <form
          v-if="!apiKey"
          id="changePasswordForm"
          class="space-y-4"
          @submit.prevent="changePassword"
        >
          <h3 class="flex items-center gap-2 text-base font-semibold text-highlighted">
            <UIcon name="i-lucide-key-round" class="size-5" />
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
          <p v-if="saveError" class="text-sm text-error">{{ saveError }}</p>
        </form>
      </div>
    </template>

    <template #footer>
      <div class="flex w-full flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <UButton
          color="error"
          variant="soft"
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

const me = computed(() => auth.status.value);
const apiKey = computed(() => me.value?.auth_method === 'api_key');
const username = computed(() => me.value?.username || 'Unknown');

const clearForm = (): void => {
  currentPassword.value = '';
  password.value = '';
  confirmation.value = '';
  saveError.value = '';
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
