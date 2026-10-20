import { ref } from 'vue';
import { useStorage } from '@vueuse/core';
import { ensure_api_success, request, to_api_error } from '~/utils';

const enabled = useStorage('background_enabled', true);
const visibility = useStorage('background_visibility', 0.02);
const image = ref('');
const loading = ref(false);
const empty = ref(false);
const error = ref('');
let controller: AbortController | null = null;

const clear = (): void => {
  controller?.abort();
  controller = null;
  loading.value = false;
  if (image.value) URL.revokeObjectURL(image.value);
  image.value = '';
  empty.value = false;
  error.value = '';
};

const load = async (force = false): Promise<void> => {
  if (!enabled.value || loading.value) return;
  const active = new AbortController();
  controller = active;
  loading.value = true;
  empty.value = false;
  error.value = '';
  let nextUrl = '';
  try {
    const response = await request(`/api/background${force ? '?force=true' : ''}`, {
      signal: active.signal,
      cache: 'no-store',
    });
    if (active.signal.aborted) return;
    if (response.status === 204) {
      empty.value = true;
      return;
    }
    await ensure_api_success(response);
    const blob = await response.blob();
    if (active.signal.aborted) return;
    nextUrl = URL.createObjectURL(blob);
    const picture = new Image();
    picture.src = nextUrl;
    await picture.decode();
    if (active.signal.aborted) return;
    const previous = image.value;
    image.value = nextUrl;
    nextUrl = '';
    if (previous) URL.revokeObjectURL(previous);
  } catch (cause) {
    if (!active.signal.aborted) error.value = to_api_error(cause).message;
  } finally {
    if (nextUrl) URL.revokeObjectURL(nextUrl);
    if (controller === active) {
      controller = null;
      loading.value = false;
    }
  }
};

export const useBackground = () => ({
  enabled,
  visibility,
  image,
  loading,
  empty,
  error,
  load,
  clear,
});
