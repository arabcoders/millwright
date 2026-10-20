<template>
  <UApp :toaster="toaster">
    <UAlert
      v-if="auth.lastError.value"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Sign-in unavailable"
      :description="auth.lastError.value"
    >
      <template #actions>
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-refresh-cw"
          :loading="retrying"
          @click="retryAuth"
          >Retry</UButton
        >
      </template>
    </UAlert>
    <slot />
  </UApp>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { AUTH_CHANGE_KEY, useAuth } from '~/composables/useAuth';
import { useStorage } from '@vueuse/core';
import type { toastPosition } from '~/composables/useNotification';

const anim = useStorage<boolean>('page_anims', true);
const auth = useAuth();
const route = useRoute();
useEventListener(window, 'storage', async (event: StorageEvent) => {
  if (event.key !== AUTH_CHANGE_KEY) return;
  try {
    const me = await auth.refreshSession();
    if (!me) await navigateTo('/login');
    else if (route.path === '/login') await navigateTo('/');
  } catch {
    // Keep the current page and drafts. The session alert provides a retry.
  }
});
const retrying = ref(false);
const retryAuth = async (): Promise<void> => {
  retrying.value = true;
  try {
    if (!(await auth.probe(true))) await navigateTo('/login');
  } catch {
    // The persistent alert shows session check failures.
  } finally {
    retrying.value = false;
  }
};
const toastPos = useStorage<toastPosition>('toast_position', 'top-right');

const toaster = computed(() => ({
  position: toastPos.value,
  max: 5,
  expand: true,
  progress: true,
}));

watch(anim, (on) => document.documentElement.classList.toggle('no-page-anim', !on), {
  immediate: true,
});
</script>
