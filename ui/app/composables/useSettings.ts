import { ref } from 'vue';
import { parse_api_error, request } from '~/utils';
import { useDirtyState } from '~/composables/useDirtyState';
import type { OperationalSettings } from '~/types/api';

export const useSettings = () => {
  const draft = ref<OperationalSettings | null>(null);
  const loading = ref(false);
  const saving = ref(false);
  const error = ref<string | null>(null);
  const { isDirty, markClean } = useDirtyState(draft);
  markClean();

  const read = async (options?: RequestInit): Promise<OperationalSettings> => {
    const response = await request('/api/settings', options);
    if (!response.ok)
      throw new Error(await parse_api_error(await response.json().catch(() => null)));
    return (await response.json()) as OperationalSettings;
  };

  const load = async (): Promise<void> => {
    if (loading.value || saving.value) return;
    loading.value = true;
    error.value = null;
    try {
      draft.value = await read();
      markClean();
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : 'Could not load settings.';
    } finally {
      loading.value = false;
    }
  };

  const save = async (): Promise<boolean> => {
    if (!draft.value || loading.value || saving.value) return false;
    saving.value = true;
    error.value = null;
    try {
      draft.value = await read({ method: 'PATCH', body: JSON.stringify(draft.value) });
      markClean();
      return true;
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : 'Could not save settings.';
      return false;
    } finally {
      saving.value = false;
    }
  };

  return { draft, loading, saving, error, isDirty, load, save };
};
