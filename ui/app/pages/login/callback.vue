<template>
  <div class="space-y-4 text-center">
    <template v-if="error">
      <UAlert
        color="error"
        variant="soft"
        icon="i-lucide-triangle-alert"
        :title="retryable ? 'Sign-in unavailable' : 'Couldn’t sign in'"
        :description="error"
      />

      <UButton
        v-if="retryable"
        color="neutral"
        variant="outline"
        icon="i-lucide-refresh-cw"
        block
        @click="checkSession"
        >Retry</UButton
      >
      <UButton color="neutral" variant="outline" block to="/login">Back to sign-in</UButton>
    </template>

    <template v-else>
      <UIcon name="i-lucide-loader-circle" class="size-6 animate-spin text-primary mx-auto" />
      <p class="text-sm text-toned">Signing you in...</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useAuth } from '~/composables/useAuth';

definePageMeta({ layout: 'auth' });

const auth = useAuth();
const error = ref('');
const retryable = ref(false);
const checkSession = async (): Promise<void> => {
  error.value = '';
  retryable.value = false;
  try {
    const me = await auth.refreshSession();
    if (!me) {
      error.value = 'Please sign in again.';
      return;
    }
    auth.announceChange();
    await navigateTo('/', { replace: true });
  } catch {
    retryable.value = true;
    error.value = auth.lastError.value ?? 'Couldn’t verify your sign-in. Try again.';
  }
};

onMounted(checkSession);
</script>
