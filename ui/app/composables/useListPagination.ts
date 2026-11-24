import { nextTick, ref } from 'vue';
import { useStorage } from '@vueuse/core';
import { listPageSize } from '~/utils/listPagination';

type Position = 'top' | 'bottom';
const DEFAULT_PAGE_SIZE = 50;

export const useListPagination = (
  storageKey: string,
  updateQuery: (changes: Record<string, string>) => Promise<boolean>,
) => {
  const savedLimit = useStorage(storageKey, DEFAULT_PAGE_SIZE);
  const limit = ref(DEFAULT_PAGE_SIZE);
  const results = ref<HTMLElement | null>(null);

  const applyRouteLimit = (value: string | undefined): void => {
    limit.value = listPageSize(value) ?? listPageSize(savedLimit.value) ?? DEFAULT_PAGE_SIZE;
  };

  const scrollToResults = async (position: Position): Promise<void> => {
    if (position !== 'bottom') return;
    await nextTick();
    results.value?.scrollIntoView({ block: 'start' });
  };

  const changeLimit = async (value: number, position: Position): Promise<void> => {
    if (!(await updateQuery({ limit: String(value) }))) return;
    savedLimit.value = value;
    await scrollToResults(position);
  };

  const changeOffset = async (value: number, position: Position): Promise<void> => {
    if (await updateQuery({ offset: String(value), limit: String(limit.value) }))
      await scrollToResults(position);
  };

  return { limit, results, applyRouteLimit, changeLimit, changeOffset };
};
