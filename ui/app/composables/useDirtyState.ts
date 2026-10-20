import { computed, ref, toValue, type MaybeRefOrGetter } from 'vue';

export const useDirtyState = <T>(source: MaybeRefOrGetter<T>) => {
  const snapshot = ref('');
  const serialize = () => JSON.stringify(toValue(source));
  const markClean = () => (snapshot.value = serialize());
  return { isDirty: computed(() => snapshot.value !== serialize()), markClean };
};
