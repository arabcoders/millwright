import type { WatchStopHandle } from 'vue';

type RTimeElementState = HTMLElement & { _next_timer?: number; _stop_rtime?: WatchStopHandle };

const parseInterval = (arg: string | undefined): number => {
  if (!arg) {
    return 60 * 1000;
  }

  const match = arg.match(/^(\d+)([smhd])$/);

  if (!match) {
    return 60 * 1000;
  }

  const [, numStr, unit] = match;
  const num = parseInt(String(numStr), 10);

  switch (unit) {
    case 'd':
      return num * 24 * 3600 * 1000;
    case 'h':
      return num * 3600 * 1000;
    case 'm':
      return num * 60 * 1000;
    case 's':
      return num * 1000;
    default:
      return 60 * 1000;
  }
};

export default defineNuxtPlugin((nuxtApp) => {
  nuxtApp.vueApp.directive('rtime', {
    mounted(el: RTimeElementState, binding) {
      const intervalMs = parseInterval(binding.arg);
      const update = () => {
        el.textContent = formatRelativeTime(binding.value, 'en');
      };

      update();
      el._next_timer = window.setInterval(update, intervalMs);
    },
    updated(el: RTimeElementState, binding) {
      if (binding.oldValue !== binding.value) {
        if (null != el._next_timer) clearInterval(el._next_timer);
        el._stop_rtime?.();

        const intervalMs = parseInterval(binding.arg);
        const update = () => {
          el.textContent = formatRelativeTime(binding.value, 'en');
        };

        update();
        el._next_timer = window.setInterval(update, intervalMs);
      }
    },
    beforeUnmount(el: RTimeElementState) {
      if (null != el._next_timer) clearInterval(el._next_timer);
      el._stop_rtime?.();
    },
  });

  return {};
});
