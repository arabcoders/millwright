export const LIST_PAGE_SIZES = [10, 50, 100, 200, 300, 500, 750, 999] as const;

export const listPageSize = (value: unknown): number | undefined =>
  LIST_PAGE_SIZES.find((size) => value === size || value === String(size));

export const nearbyPages = (page: number, pageCount: number): number[] => {
  const neighbors = 3;
  const count = Math.min(2 * neighbors + 1, pageCount);
  const first = Math.max(1, Math.min(page - neighbors, pageCount - count + 1));
  return Array.from({ length: count }, (_, index) => first + index);
};
