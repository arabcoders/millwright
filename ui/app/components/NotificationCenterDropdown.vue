<template>
  <UPopover
    :content="{ align: 'end', side: 'bottom', sideOffset: 8 }"
    :ui="{
      content:
        'mw-card rounded-lg border-(--mw-surface-border-soft) bg-elevated/20 backdrop-blur-xl ring-0',
    }"
  >
    <UButton
      color="neutral"
      variant="ghost"
      size="sm"
      icon="i-lucide-bell"
      aria-label="Notifications"
      title="Notifications"
    >
      <span class="hidden sm:inline">Notifications</span>
      <UBadge :color="badgeColor" variant="soft" size="sm">
        {{ center.unreadCount }}/{{ center.notifications.length }}
      </UBadge>
    </UButton>

    <template #content>
      <section class="w-md max-w-[calc(100vw-1rem)]" aria-label="Notifications">
        <header class="mw-border-bottom-soft p-4 sm:px-6">
          <div class="flex items-center justify-between gap-3">
            <div>
              <p class="text-sm font-semibold text-highlighted">Notifications</p>
              <p class="text-xs text-toned">Recent activity and errors</p>
            </div>
            <div class="flex items-center gap-1">
              <UButton
                color="neutral"
                variant="ghost"
                size="xs"
                square
                :icon="allowToasts ? 'i-lucide-bell' : 'i-lucide-bell-off'"
                :aria-label="allowToasts ? 'Pause alerts' : 'Resume alerts'"
                @click="allowToasts = !allowToasts"
              />
              <UButton
                color="neutral"
                variant="ghost"
                size="xs"
                icon="i-lucide-settings"
                @click="settingsOpen = true"
              >
                Settings
              </UButton>
            </div>
          </div>
        </header>

        <ul
          v-if="center.sortedNotifications.length"
          class="max-h-[min(26rem,60dvh)] space-y-2 overflow-y-auto overscroll-contain p-3"
        >
          <li
            v-for="item in center.sortedNotifications"
            :key="item.id"
            class="mw-card flex items-start gap-2.5 rounded-none border-l-4 p-3"
            :class="[
              levelBorder[item.level],
              expandedId === item.id ? 'ring-1 ring-primary/30' : '',
            ]"
          >
            <UIcon
              :name="iconFor(item.level)"
              class="mt-0.5 size-4 shrink-0"
              :class="levelColor[item.level]"
            />
            <div class="min-w-0 flex-1 space-y-1">
              <button
                type="button"
                class="block w-full text-start text-sm font-medium hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                :class="item.seen ? 'text-toned' : 'text-highlighted'"
                :aria-expanded="expandedId === item.id"
                @click="expand(item.id)"
              >
                <span
                  :class="
                    expandedId === item.id
                      ? 'block whitespace-pre-wrap wrap-break-word'
                      : 'block truncate'
                  "
                  >{{ item.message }}</span
                >
              </button>
              <div class="flex flex-wrap items-center gap-1 text-xs text-dimmed">
                <UTooltip :text="formatDate(item.created, 'detail')">
                  <time :datetime="toIsoString(item.created)">
                    {{ formatRelativeTime(item.created, 'en') }}
                  </time>
                </UTooltip>
                <span aria-hidden="true">·</span>
                <button
                  type="button"
                  class="text-toned underline underline-offset-2 hover:text-primary"
                  @click.stop="copyMessage(item.id, item.message)"
                >
                  <span v-if="copiedId === item.id" class="text-success">Copied</span
                  ><span v-else>Copy</span>
                </button>
              </div>
            </div>
            <div class="flex shrink-0 items-center gap-1">
              <UButton
                v-if="!item.seen"
                color="neutral"
                variant="ghost"
                size="xs"
                square
                icon="i-lucide-check"
                aria-label="Mark read"
                @click.stop="center.markRead(item.id)"
              />
              <UButton
                color="neutral"
                variant="ghost"
                size="xs"
                square
                icon="i-lucide-trash-2"
                aria-label="Remove"
                @click.stop="center.remove(item.id)"
              />
            </div>
          </li>
        </ul>
        <UEmpty
          v-else
          variant="naked"
          icon="i-lucide-inbox"
          title="No notifications"
          class="px-4 py-8"
        />

        <footer class="mw-border-top-soft flex flex-wrap justify-end gap-2 p-4 sm:px-6">
          <UButton
            color="neutral"
            variant="ghost"
            size="sm"
            icon="i-lucide-check"
            :disabled="center.unreadCount === 0"
            @click="center.markAllRead()"
            >Mark all read</UButton
          >
          <UButton
            color="neutral"
            variant="ghost"
            size="sm"
            icon="i-lucide-trash-2"
            :disabled="center.notifications.length === 0"
            @click="center.clear()"
            >Clear</UButton
          >
        </footer>
      </section>
    </template>
  </UPopover>

  <UModal v-model:open="settingsOpen" title="Notification settings">
    <template #body>
      <div class="space-y-4">
        <USwitch v-model="allowToasts" label="Show pop-up alerts" />
        <UFormField v-if="allowToasts" label="Destination">
          <USelectMenu
            v-model="toastTarget"
            :items="targetItems"
            value-key="value"
            label-key="label"
            class="w-full"
            @update:model-value="changeTarget"
          />
        </UFormField>
        <UFormField v-if="allowToasts && toastTarget === 'toast'" label="Toast position">
          <USelectMenu
            v-model="toastPosition"
            :items="positionItems"
            value-key="value"
            class="w-full"
          />
        </UFormField>
        <USwitch
          v-if="allowToasts && toastTarget === 'toast'"
          v-model="dismissOnClick"
          label="Dismiss on click"
        />
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { useStorage } from '@vueuse/core';
import { copyText } from '~/utils';
import { formatDate, toIsoString } from '~/utils/date';
import { formatRelativeTime } from '~/utils/relativeTime';
import { useNotificationCenter } from '~/composables/useNotificationCenter';
import {
  useNotification,
  type notificationType,
  type notificationTarget,
  type toastPosition,
} from '~/composables/useNotification';

const center = useNotificationCenter();
const { requestBrowserPermission } = useNotification();
const toast = useToast();
const settingsOpen = ref(false);
const allowToasts = useStorage('allow_toasts', true);
const toastTarget = useStorage<notificationTarget>('toast_target', 'toast');
const toastPosition = useStorage<toastPosition>('toast_position', 'top-right');
const dismissOnClick = useStorage('toast_dismiss_on_click', true);
const expandedId = ref<string | null>(null);
const copiedId = ref<string | null>(null);

const targetItems = [
  { label: 'Toast', value: 'toast' },
  { label: 'Browser notification', value: 'browser' },
];
const positionItems = [
  { label: 'Top left', value: 'top-left' },
  { label: 'Top center', value: 'top-center' },
  { label: 'Top right', value: 'top-right' },
  { label: 'Bottom left', value: 'bottom-left' },
  { label: 'Bottom center', value: 'bottom-center' },
  { label: 'Bottom right', value: 'bottom-right' },
];

const badgeColor = computed(() => {
  switch (center.severityLevel) {
    case 'error':
    case 'warning':
    case 'success':
    case 'info':
      return center.severityLevel;
    default:
      return 'neutral';
  }
});

const iconFor = (level: notificationType): string =>
  ({
    info: 'i-lucide-info',
    success: 'i-lucide-badge-check',
    warning: 'i-lucide-circle-alert',
    error: 'i-lucide-triangle-alert',
  })[level];

const levelColor: Record<notificationType, string> = {
  info: 'text-info',
  success: 'text-success',
  warning: 'text-warning',
  error: 'text-error',
};

const levelBorder: Record<notificationType, string> = {
  info: 'border-l-info',
  success: 'border-l-success',
  warning: 'border-l-warning',
  error: 'border-l-error',
};

const expand = (id: string): void => {
  expandedId.value = expandedId.value === id ? null : id;
  center.markRead(id);
};

const copyMessage = (id: string, message: string): void => {
  copiedId.value = id;
  copyText(message, false, false);
  window.setTimeout(() => {
    if (copiedId.value === id) copiedId.value = null;
  }, 2000);
};

const changeTarget = async (): Promise<void> => {
  if (toastTarget.value !== 'browser') return;
  const permission = await requestBrowserPermission().catch(() => 'denied');
  if (permission !== 'granted') {
    toastTarget.value = 'toast';
    toast.add({
      title: 'Browser notifications unavailable',
      description: 'Using toast notifications instead.',
      color: 'warning',
    });
  }
};
</script>
