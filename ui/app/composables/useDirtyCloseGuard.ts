import { onBeforeRouteLeave, onBeforeRouteUpdate } from '#app';
import { computed, onBeforeUnmount, toValue, type MaybeRefOrGetter, type Ref } from 'vue';

export const useDirtyCloseGuard = (
  open: Ref<boolean>,
  dirty: MaybeRefOrGetter<boolean>,
  close: () => void,
) => {
  const dialog = useDialog();
  let pending: Promise<boolean> | null = null;
  const guarded = computed(() => open.value && Boolean(toValue(dirty)));
  const requestClose = async () => {
    if (!guarded.value) return (close(), true);
    const { status } = await dialog.confirmDialog({
      title: 'Discard changes?',
      message: 'Your unsaved changes will be lost.',
      confirmText: 'Discard changes',
      confirmColor: 'warning',
    });
    if (status) close();
    return status;
  };
  const request = () => (pending ??= requestClose().finally(() => (pending = null)));
  const handleOpenChange = (value: boolean) => (value ? (open.value = true) : request());
  const guard = async (_to: unknown, _from: unknown, next: (value?: false) => void) =>
    next((await request()) ? undefined : false);
  onBeforeRouteLeave((_to, _from, next) => (guarded.value ? guard(_to, _from, next) : next()));
  onBeforeRouteUpdate((_to, _from, next) => (guarded.value ? guard(_to, _from, next) : next()));
  if (typeof window !== 'undefined') {
    const unload = (event: BeforeUnloadEvent) => {
      if (guarded.value) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', unload);
    onBeforeUnmount(() => window.removeEventListener('beforeunload', unload));
  }
  return { requestClose, handleOpenChange };
};
