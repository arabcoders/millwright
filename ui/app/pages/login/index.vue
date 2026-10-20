<template>
  <div class="space-y-4">
    <h1 class="text-center text-2xl font-semibold text-highlighted">
      {{ serverStatus?.setup_required ? 'Create your account' : 'Sign in' }}
    </h1>
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
      <form class="space-y-4" @submit.prevent="signIn">
        <UFormField label="Username" required>
          <UInput
            v-model="username"
            class="w-full"
            autocomplete="username"
            :maxlength="100"
            :disabled="busy"
            required
          />
        </UFormField>
        <UFormField label="Password" required>
          <PasswordInput
            v-model="password"
            class="w-full"
            :maxlength="1024"
            :autocomplete="serverStatus.setup_required ? 'new-password' : 'current-password'"
            :disabled="busy"
            required
          />
        </UFormField>
        <template v-if="serverStatus.setup_required">
          <UFormField label="Confirm password" required>
            <PasswordInput
              v-model="confirmation"
              class="w-full"
              autocomplete="new-password"
              :maxlength="1024"
              :disabled="busy"
              required
            />
          </UFormField>
        </template>
        <UButton type="submit" icon="i-lucide-log-in" :loading="signingIn" :disabled="busy" block>
          {{ serverStatus.setup_required ? 'Create account' : 'Sign in' }}
        </UButton>
      </form>

      <template v-if="serverStatus.oidc_available && !serverStatus.setup_required">
        <USeparator label="or" />
        <UButton
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
      </template>
    </template>
    <p v-else-if="checking" class="text-center text-sm text-toned">Loading...</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useAuth, type AuthStatus } from '~/composables/useAuth';
import { to_api_error } from '~/utils';

definePageMeta({ layout: 'auth' });

const auth = useAuth();
const serverStatus = ref<AuthStatus | null>(null);
const username = ref('');
const password = ref('');
const confirmation = ref('');
const error = ref('');
const redirecting = ref(false);
const signingIn = ref(false);
const probeError = ref(false);
const checking = ref(false);
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
    await auth.login(username.value, password.value, serverStatus.value.setup_required);
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
  const result = await auth.oidcLogin();
  if (result.ok) {
    window.location.href = result.authorize_url;
  } else {
    error.value = result.detail || 'Couldn’t start SSO sign-in. Try again.';
    redirecting.value = false;
  }
};
</script>
