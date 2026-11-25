<template>
  <UCard>
    <template #header>
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h1 class="text-base font-semibold text-highlighted">
          {{ serverStatus?.setup_required ? 'Create your account' : 'Sign in' }}
        </h1>
        <ThemeButton square :show-label="false" />
      </div>
    </template>
    <div class="space-y-5">
      <UAlert
        v-if="error"
        color="error"
        variant="soft"
        icon="i-lucide-triangle-alert"
        :title="
          probeError
            ? 'Sign-in unavailable'
            : serverStatus?.setup_required
              ? 'Couldn’t create account'
              : 'Couldn’t sign in'
        "
        :description="error"
      />
      <UButton
        v-if="probeError"
        color="neutral"
        variant="outline"
        icon="i-lucide-refresh-cw"
        :loading="checking"
        block
        @click="checkSession"
        >Retry</UButton
      >

      <template v-if="serverStatus && !probeError">
        <form method="post" class="space-y-5" @submit.prevent="signIn">
          <UFormField label="Username" name="username" required>
            <UInput
              id="login-username"
              name="username"
              v-model="username"
              class="w-full"
              icon="i-lucide-user"
              autocomplete="username"
              :maxlength="100"
              :disabled="busy"
              required
            />
          </UFormField>
          <UFormField label="Password" name="password" required>
            <PasswordInput
              id="login-password"
              name="password"
              v-model="password"
              class="w-full"
              icon="i-lucide-lock-keyhole"
              :maxlength="1024"
              :autocomplete="serverStatus.setup_required ? 'new-password' : 'current-password'"
              :disabled="busy"
              required
            />
          </UFormField>
          <template v-if="serverStatus.setup_required">
            <UFormField label="Confirm password" name="confirmation" required>
              <PasswordInput
                id="login-confirmation"
                name="confirmation"
                v-model="confirmation"
                class="w-full"
                icon="i-lucide-lock-keyhole"
                autocomplete="new-password"
                :maxlength="1024"
                :disabled="busy"
                required
              />
            </UFormField>
          </template>
          <UCheckbox v-model="rememberMe" label="Remember me" :disabled="busy" />
          <div
            class="grid gap-3"
            :class="
              serverStatus.oidc_available && !serverStatus.setup_required
                ? 'sm:grid-cols-2'
                : 'grid-cols-1'
            "
          >
            <UButton
              type="submit"
              icon="i-lucide-log-in"
              :loading="signingIn"
              :disabled="busy"
              block
            >
              {{ serverStatus.setup_required ? 'Create account' : 'Sign in' }}
            </UButton>
            <UButton
              v-if="serverStatus.oidc_available && !serverStatus.setup_required"
              type="button"
              color="neutral"
              variant="outline"
              icon="i-lucide-key-round"
              block
              :loading="redirecting"
              :disabled="busy"
              @click="signInExternal"
            >
              Sign in with SSO
            </UButton>
          </div>
        </form>
        <UButton
          v-if="!serverStatus.setup_required"
          type="button"
          color="neutral"
          variant="link"
          block
          :disabled="busy"
          @click="showPasswordReset = true"
        >
          Forgot your credentials?
        </UButton>
      </template>
      <p v-else-if="checking" class="text-center text-sm text-toned">Loading...</p>
    </div>

    <UModal
      v-model:open="showPasswordReset"
      title="Reset your password"
      description="Run this command on the server from the directory containing compose.yml."
    >
      <template #body>
        <div class="space-y-4">
          <div class="mw-terminal relative pr-14">
            <code class="whitespace-pre-wrap break-all text-sm">{{ resetCommand }}</code>
            <UTooltip text="Copy command">
              <UButton
                type="button"
                color="neutral"
                variant="ghost"
                square
                size="sm"
                icon="i-lucide-copy"
                class="absolute right-2 top-2"
                aria-label="Copy command"
                @click="copyText(resetCommand)"
              />
            </UTooltip>
          </div>
          <p class="text-sm text-toned">
            Replace <code>owner</code> with your username. Enter the new password twice when
            prompted, then sign in again.
          </p>
        </div>
      </template>
    </UModal>
  </UCard>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useAuth, type AuthStatus } from '~/composables/useAuth';
import { copyText, to_api_error } from '~/utils';

definePageMeta({ layout: 'auth' });

const auth = useAuth();
const serverStatus = ref<AuthStatus | null>(null);
const username = ref('');
const password = ref('');
const confirmation = ref('');
const rememberMe = ref(false);
const error = ref('');
const redirecting = ref(false);
const signingIn = ref(false);
const probeError = ref(false);
const checking = ref(false);
const showPasswordReset = ref(false);
const resetCommand = 'docker compose exec server millwright auth reset-password --username owner';
const busy = computed(() => signingIn.value || redirecting.value || checking.value);

const checkSession = async (): Promise<void> => {
  checking.value = true;
  probeError.value = false;
  error.value = '';
  try {
    serverStatus.value = await auth.checkStatus();
    if (serverStatus.value.authenticated && (await auth.probe(true))) {
      await navigateTo('/', { replace: true });
    }
  } catch (cause) {
    probeError.value = true;
    error.value = `${to_api_error(cause).message} Try again.`;
  } finally {
    checking.value = false;
  }
};
onMounted(checkSession);

const signIn = async (): Promise<void> => {
  if (!serverStatus.value) return;
  error.value = '';
  if (serverStatus.value.setup_required && password.value !== confirmation.value) {
    error.value = 'Passwords do not match.';
    return;
  }
  signingIn.value = true;
  try {
    await auth.login(
      username.value,
      password.value,
      serverStatus.value.setup_required,
      rememberMe.value,
    );
    password.value = '';
    confirmation.value = '';
    await navigateTo('/', { replace: true });
  } catch (cause) {
    error.value = to_api_error(cause).message;
  } finally {
    signingIn.value = false;
  }
};

const signInExternal = async (): Promise<void> => {
  error.value = '';
  redirecting.value = true;
  const result = await auth.oidcLogin(rememberMe.value);
  if (result.ok) {
    window.location.href = result.authorize_url;
  } else {
    error.value = result.detail || 'Couldn’t start SSO sign-in. Try again.';
    redirecting.value = false;
  }
};
</script>
