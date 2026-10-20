<template>
  <AppRoot>
    <div class="shell-stage shell-surface flex flex-col">
      <div id="main_container" class="shell-root flex flex-col">
        <UDashboardGroup
          storage="local"
          storage-key="mw-shell"
          class="shell-dashboard flex-1"
          :ui="{ base: 'relative flex min-h-full overflow-visible' }"
        >
          <UDashboardSidebar
            v-model:open="showSidebar"
            side="left"
            toggle-side="left"
            :menu="sidebarMenu"
            collapsible
            resizable
            :default-size="15"
            :min-size="10"
            :max-size="20"
            :collapsed-size="4"
            :ui="dashboardSidebarUi"
          >
            <template #header="{ collapsed }">
              <NuxtLink
                to="/"
                class="flex w-full min-w-0 items-center gap-2 rounded-xl transition-colors hover:bg-elevated/60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                :class="collapsed ? 'justify-center p-1' : 'px-1.5 py-1'"
                aria-label="Home"
              >
                <span
                  class="relative inline-flex shrink-0 items-center justify-center transition-all duration-200"
                  :class="
                    collapsed
                      ? 'size-10 rounded-xl bg-elevated/80 ring ring-default shadow-xs'
                      : 'size-9 rounded-lg'
                  "
                >
                  <img
                    :src="uri('/images/favicon-192.png')"
                    alt="Millwright"
                    class="rounded-lg object-contain"
                    :class="collapsed ? 'size-6' : 'size-5'"
                  />
                </span>

                <div v-if="false === collapsed" class="min-w-0">
                  <p class="truncate text-sm font-semibold">Millwright</p>
                  <p class="truncate text-xs text-toned">Transcode Automation</p>
                </div>
              </NuxtLink>
            </template>

            <template #default="{ collapsed }">
              <div class="flex h-full flex-col gap-6 px-1 py-1">
                <div v-for="section in sidebarSections" :key="section.id" class="space-y-2">
                  <p
                    v-if="false === collapsed && section.label"
                    class="px-2 text-[11px] font-semibold tracking-[0.22em] text-toned uppercase"
                  >
                    {{ section.label }}
                  </p>

                  <UNavigationMenu
                    orientation="vertical"
                    :collapsed="collapsed"
                    :items="section.items"
                    :tooltip="true"
                    :ui="navigationUi(collapsed)"
                  />
                </div>
              </div>
            </template>
          </UDashboardSidebar>

          <UDashboardPanel class="min-w-0 bg-transparent" :ui="dashboardPanelUi">
            <template #header>
              <UDashboardNavbar :toggle="false" :title="pageTitle" :ui="dashboardNavbarUi">
                <template #left>
                  <div class="flex items-center gap-2">
                    <UDashboardSidebarToggle class="lg:hidden" />
                    <UDashboardSidebarCollapse class="hidden lg:inline-flex" />
                  </div>
                </template>

                <template #right>
                  <div class="flex items-center gap-1 sm:gap-2">
                    <UButton
                      :color="scanner?.paused ? 'warning' : 'neutral'"
                      variant="ghost"
                      size="sm"
                      class="shrink-0"
                      :icon="scanner?.paused ? 'i-lucide-play' : 'i-lucide-pause'"
                      :label="scanner?.paused ? 'Resume activity' : 'Pause activity'"
                      :title="activityDisabledReason || undefined"
                      :loading="scannerBusy || activityConfirming"
                      :disabled="!!activityDisabledReason"
                      @click="toggleActivity"
                    />

                    <UButton
                      color="neutral"
                      variant="outline"
                      size="sm"
                      icon="i-lucide-search"
                      aria-label="Search"
                      title="Search"
                      class="shrink-0 lg:hidden"
                      @click="openSearch"
                    />

                    <UDashboardSearchButton class="hidden shrink-0 lg:inline-flex" />

                    <ThemeButton :square="true" :show-label="false" />

                    <NotificationCenterDropdown />

                    <UTooltip v-if="disconnected" text="Live updates disconnected. Reconnecting.">
                      <span
                        tabindex="0"
                        role="img"
                        aria-label="Live updates disconnected. Reconnecting."
                        class="inline-flex size-8 shrink-0 items-center justify-center text-warning"
                      >
                        <UIcon name="i-lucide-wifi-off" class="size-4" aria-hidden="true" />
                      </span>
                    </UTooltip>

                    <UTooltip :text="reloadHint">
                      <UButton
                        color="neutral"
                        variant="ghost"
                        size="sm"
                        icon="i-lucide-refresh-cw"
                        aria-label="Refresh"
                        @click="reloadNuxtApp({ force: true })"
                      />
                    </UTooltip>

                    <UDropdownMenu :items="accountMenu" :content="{ align: 'end' }">
                      <UButton
                        color="neutral"
                        variant="ghost"
                        size="sm"
                        icon="i-lucide-user-round"
                        aria-label="Account"
                        title="Account"
                      >
                        <span class="hidden xl:inline">Account</span>
                      </UButton>
                    </UDropdownMenu>
                  </div>
                </template>
              </UDashboardNavbar>
            </template>

            <template #body>
              <div class="relative flex min-h-full min-w-0 max-w-full flex-1 flex-col">
                <NuxtLoadingIndicator />

                <div
                  class="flex min-h-0 min-w-0 max-w-full flex-1 flex-col px-4 py-4 sm:px-5 sm:py-5 lg:px-6"
                >
                  <div class="flex min-h-0 min-w-0 max-w-full flex-1 flex-col gap-4">
                    <UAlert
                      v-if="systemLoaded && !dateTimezone"
                      color="error"
                      variant="soft"
                      icon="i-lucide-triangle-alert"
                      title="Could not load the configured timezone"
                      description="Dates remain hidden rather than using a different timezone. Reload to try again."
                    >
                      <template #actions>
                        <UButton
                          color="neutral"
                          variant="outline"
                          label="Reload"
                          @click="reloadNuxtApp({ force: true })"
                        />
                      </template>
                    </UAlert>
                    <NuxtPage />
                  </div>

                  <footer
                    class="shell-footer mt-auto border-t border-default pt-6 text-sm text-toned"
                  >
                    <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
                      <span class="inline-flex items-center gap-2 font-semibold text-highlighted">
                        <UIcon name="i-lucide-clapperboard" class="size-4" />
                        <span>Millwright</span>
                      </span>

                      <UTooltip
                        v-if="system?.version"
                        :text="`Branch ${system.branch} · Python ${system.python}`"
                      >
                        <span class="text-xs has-tooltip">
                          {{ system.version }} ·
                          {{
                            system.build_date === 'unknown'
                              ? 'unknown'
                              : formatDate(system.build_date, 'date')
                          }}
                        </span>
                      </UTooltip>

                      <UTooltip
                        v-if="system && system.commit_sha !== 'unknown'"
                        :text="system.commit_sha"
                      >
                        <a
                          :href="`https://github.com/arabcoders/millwright/commit/${system.commit_sha}`"
                          target="_blank"
                          rel="noopener noreferrer"
                          class="text-xs has-tooltip underline"
                        >
                          {{ system.commit_sha.slice(0, 7) }}
                        </a>
                      </UTooltip>

                      <span v-if="system?.protocol_version" class="text-xs">
                        protocol {{ system.protocol_version }}
                      </span>
                    </div>
                  </footer>
                </div>

                <Dialog />
              </div>
            </template>
          </UDashboardPanel>

          <UDashboardSearch
            :open="searchOpen"
            v-model:search-term="searchTerm"
            @update:open="handleSearchOpen"
            :groups="searchGroups"
            shortcut="meta_k"
            placeholder="Search"
            :ui="{ modal: 'sm:max-w-3xl h-full sm:h-[28rem]' }"
          >
            <template #footer>
              <UAlert
                v-if="listError || jobError"
                color="error"
                variant="soft"
                icon="i-lucide-triangle-alert"
                title="Search unavailable"
                :description="[listError, jobError].filter(Boolean).join(' ')"
                class="w-full"
              />
            </template>
          </UDashboardSearch>

          <AccountModal v-model:open="accountOpen" />
        </UDashboardGroup>
      </div>
    </div>
  </AppRoot>
</template>

<script setup lang="ts">
import type { DropdownMenuItem, NavigationMenuItem } from '@nuxt/ui';
import { ref, computed, onBeforeUnmount, onMounted, watch } from 'vue';
import { useMediaQuery, useMounted } from '@vueuse/core';
import { useAuth } from '~/composables/useAuth';
import { useBackground } from '~/composables/useBackground';
import AppRoot from '~/components/AppRoot.vue';
import ThemeButton from '~/components/ThemeButton.vue';
import { formatPageTitle, to_api_error, uri } from '~/utils';
import { getSidebarSwipeMode } from '~/utils/sidebarSwipe';
import { useDialog } from '~/composables/useDialog';
import Dialog from '~/components/Dialog.vue';
import AccountModal from '~/components/AccountModal.vue';
import WebUiSettingsPanel from '~/components/WebUiSettingsPanel.vue';
import { useEvents, type ServerEvent } from '~/composables/useEvents';
import {
  getActiveNavItem,
  getNavItems,
  getNavSections,
  type NavItem,
} from '~/utils/topLevelNavigation';
import { useGlobalSearch } from '~/composables/useGlobalSearch';
import { useSystem } from '~/composables/useSystem';
import { useLibraryProgress } from '~/composables/useLibraryProgress';
import { dateTimezone, formatDate } from '~/utils/date';
import { notifyRequeueOutcome } from '~/composables/useJobRequeue';
import { notifyJobRequestResult } from '~/composables/useFiles';
import type { SystemInfo } from '~/types/api';

type SidebarSection = {
  id: string;
  label?: string;
  items: Array<Array<NavigationMenuItem>>;
};

type SwipeMode = 'open' | 'close';

const MOBILE_SIDEBAR_MIN_SWIPE_DISTANCE = 64;

const auth = useAuth();
const {
  enabled: backgroundEnabled,
  visibility: backgroundVisibility,
  image: backgroundImage,
  load: loadBackground,
  clear: clearBackground,
} = useBackground();
const notify = useNotification();
const route = useRoute();
const router = useRouter();
const accountOpen = ref(false);
const overlay = useOverlay();
const {
  searchOpen,
  searchTerm,
  searchable,
  matchedJob,
  listError,
  jobError,
  openSearch,
  closeSearch,
} = useGlobalSearch();
const showSidebar = ref(false);
const system = ref<SystemInfo | null>(null);
const systemLoaded = ref(false);
const { load: loadSystem } = useSystem();
const {
  scanner,
  scannerBusy,
  isLoading: progressLoading,
  ensure: ensureProgress,
  setPaused: setActivityPaused,
} = useLibraryProgress();
const { confirmDialog } = useDialog();
const activityConfirming = ref(false);
const activityDisabledReason = computed(() => {
  if (scannerBusy.value || activityConfirming.value)
    return 'Wait for the activity control to finish.';
  if (progressLoading.value) return 'Loading activity state.';
  if (!scanner.value) return 'Refresh to check the current activity state.';
  return '';
});
const toggleActivity = async (): Promise<void> => {
  if (activityDisabledReason.value || !scanner.value) return;
  const paused = !scanner.value.paused;
  activityConfirming.value = true;
  try {
    if (paused) {
      const { status } = await confirmDialog({
        title: 'Pause activity?',
        message: 'Current work will finish. New work will wait until you resume.',
        confirmText: 'Pause activity',
        confirmIcon: 'i-lucide-pause',
      });
      if (!status) return;
    }
    await setActivityPaused(paused);
  } finally {
    activityConfirming.value = false;
  }
};
const toast = useToast();
const { connected, on: onEvent } = useEvents();
let stopEvents: (() => void) | null = null;
const mounted = useMounted();
const disconnected = computed(() => mounted.value && !connected.value);
const reloadHint = 'Refresh all data';
const mobile = useMediaQuery('(max-width: 1023px)');
const isMobile = computed(() => mounted.value && mobile.value);
const sidebarMenu = computed(() => ({ side: 'left' as const }));

type SwipeState = {
  mode: null | SwipeMode;
  tracking: boolean;
  startX: number;
  startY: number;
  endX: number;
  endY: number;
};

const Swipe: SwipeState = {
  mode: null,
  tracking: false,
  startX: 0,
  startY: 0,
  endX: 0,
  endY: 0,
};

const resetSwipe = (): void => {
  Swipe.mode = null;
  Swipe.tracking = false;
  Swipe.startX = 0;
  Swipe.startY = 0;
  Swipe.endX = 0;
  Swipe.endY = 0;
};

const updateSwipePosition = (touch?: Touch): void => {
  if (!touch) {
    return;
  }

  Swipe.endX = touch.clientX;
  Swipe.endY = touch.clientY;
};

const handleSwipeStart = (event: TouchEvent): void => {
  if (!isMobile.value || event.touches.length !== 1) {
    resetSwipe();
    return;
  }

  const touch = event.touches[0];

  if (!touch) {
    resetSwipe();
    return;
  }

  const swipeMode: SwipeMode | null = getSidebarSwipeMode(
    showSidebar.value,
    touch.clientX,
    navigator,
    false,
    window.innerWidth,
  );

  if (!swipeMode) {
    resetSwipe();
    return;
  }

  Swipe.mode = swipeMode;
  Swipe.tracking = true;
  Swipe.startX = touch.clientX;
  Swipe.startY = touch.clientY;
  updateSwipePosition(touch);
};

const handleSwipeMove = (event: TouchEvent): void => {
  if (!Swipe.tracking || event.touches.length !== 1) {
    return;
  }

  updateSwipePosition(event.touches[0]);
};

const completeSwipe = (): void => {
  if (!Swipe.tracking) {
    return;
  }

  const swipeMode = Swipe.mode;
  const deltaX = Swipe.endX - Swipe.startX;
  const deltaY = Swipe.endY - Swipe.startY;
  const isHorizontalOpenSwipe =
    swipeMode === 'open' &&
    deltaX >= MOBILE_SIDEBAR_MIN_SWIPE_DISTANCE &&
    deltaX > Math.abs(deltaY);
  const isHorizontalCloseSwipe =
    swipeMode === 'close' &&
    deltaX <= -MOBILE_SIDEBAR_MIN_SWIPE_DISTANCE &&
    Math.abs(deltaX) > Math.abs(deltaY);

  resetSwipe();

  if (isHorizontalOpenSwipe) {
    showSidebar.value = true;
  }

  if (isHorizontalCloseSwipe) {
    showSidebar.value = false;
  }
};

const handleSwipeEnd = (event: TouchEvent): void => {
  updateSwipePosition(event.changedTouches[0]);
  completeSwipe();
};

const handleSwipeCancel = (): void => {
  resetSwipe();
};

const dashboardSidebarUi = computed(() => {
  return {
    root: 'shell-surface border-e border-default bg-default/95 backdrop-blur-sm',
    header: 'border-b border-default px-2.5 py-3',
    body: 'gap-3 px-2.5 py-3',
    footer: 'border-t border-default px-2.5 py-3',
  };
});

const dashboardNavbarUi = {
  root: 'border-b border-default bg-transparent px-4 py-3 sm:px-5 lg:px-6',
  title: 'text-sm font-semibold text-highlighted',
  right: 'flex items-center shrink-0 gap-1.5',
};

const dashboardPanelUi = {
  root: 'min-w-0 min-h-screen max-w-full flex flex-1 flex-col bg-transparent',
  body: 'flex min-h-0 min-w-0 max-w-full flex-1 flex-col overflow-y-visible p-0',
};

const navigationUi = (collapsed: boolean) => ({
  root: 'w-full',
  list: 'gap-1.5',
  link: collapsed
    ? 'justify-center rounded-lg px-2 py-2'
    : 'rounded-lg px-2.5 py-2 text-sm font-medium text-default transition-colors',
  linkLeadingIcon: collapsed ? 'size-5' : 'size-4',
  linkLabel: collapsed ? 'hidden' : 'truncate',
});

const makeNavigationItem = (item: NavItem, activeId?: string): NavigationMenuItem => ({
  label: item.label,
  icon: item.icon,
  to: item.to,
  value: item.id,
  active: item.id === activeId,
});

const sidebarSections = computed<Array<SidebarSection>>(() => {
  const items = getNavItems().filter((entry) => entry.sidebarVisible);
  const activeId = getActiveNavItem(route, items)?.id;
  return getNavSections().map((section) => ({
    id: section.id,
    label: section.label,
    items: [
      items
        .filter((entry) => entry.section === section.id)
        .map((entry) => makeNavigationItem(entry, activeId)),
    ],
  }));
});

const handleSearchOpen = (open: boolean): void => {
  if (open) void openSearch();
  else if (searchOpen.value) closeSearch();
};

const openSearchResult = (path: string): void => {
  closeSearch();
  void router.push(path);
};

const searchGroups = computed(() => [
  ...getNavSections()
    .map((section) => ({
      id: section.id,
      label: section.label,
      items: getNavItems()
        .filter((entry) => entry.section === section.id)
        .map((entry) => ({
          label: entry.label,
          description: entry.description,
          icon: entry.icon,
          suffix: entry.to,
          onSelect: (): void => openSearchResult(entry.to),
        })),
    }))
    .filter((section) => section.items.length > 0),
  ...(matchedJob.value
    ? [
        {
          id: 'job-result',
          label: 'Jobs',
          ignoreFilter: true,
          items: [
            {
              label: `Job #${matchedJob.value.id}`,
              description: `${matchedJob.value.kind} · ${matchedJob.value.status}`,
              icon: 'i-lucide-list-todo',
              onSelect: (): void => {
                if (matchedJob.value) openSearchResult(`/jobs?job=${matchedJob.value.id}`);
              },
            },
          ],
        },
      ]
    : []),
  ...(searchTerm.value.trim()
    ? (
        [
          {
            id: 'node-results',
            label: 'Nodes',
            items: searchable.value.nodes,
            icon: 'i-lucide-server',
            path: '/nodes',
            key: 'node',
          },
          {
            id: 'library-results',
            label: 'Libraries',
            items: searchable.value.libraries,
            icon: 'i-lucide-folders',
            path: '/libraries',
            key: 'library',
          },
          {
            id: 'flow-results',
            label: 'Flows',
            items: searchable.value.flows,
            icon: 'i-lucide-workflow',
            path: '/flows',
            key: 'flow',
          },
        ] as const
      ).map((group) => ({
        id: group.id,
        label: group.label,
        items: group.items.map((item) => ({
          label: `${group.label.slice(0, -1)} ${'display_name' in item ? item.display_name : item.name} #${item.id}`,
          icon: group.icon,
          onSelect: (): void => openSearchResult(`${group.path}?${group.key}=${item.id}`),
        })),
      }))
    : []),
]);

const activeNavItem = computed(() => getActiveNavItem(route));

const pageTitle = computed(() => activeNavItem.value?.label ?? 'Millwright');

const documentTitle = computed(() => activeNavItem.value?.label ?? 'Millwright');

useHead(() => ({
  title: formatPageTitle(documentTitle.value),
  htmlAttrs: {
    class: backgroundImage.value ? 'bg-fanart' : undefined,
    style: backgroundImage.value
      ? {
          'background-image': `url(${backgroundImage.value})`,
          '--mw-shell-opacity': String(1 - backgroundVisibility.value),
        }
      : undefined,
  },
}));

const logout = async (): Promise<void> => {
  const { status } = await confirmDialog({
    title: 'Log out',
    message: 'End the current session on this device?',
    confirmText: 'Log out',
  });

  if (true !== status) {
    return;
  }

  try {
    await auth.logout();
    await navigateTo('/login');
  } catch (error) {
    notify.error(`Could not confirm logout. ${to_api_error(error).message}`);
  }
};

const accountMenu = computed<DropdownMenuItem[][]>(() => [
  [
    {
      label: 'Account',
      icon: 'i-lucide-user-round',
      onSelect: (): void => {
        accountOpen.value = true;
      },
    },
    {
      label: 'Web UI settings',
      icon: 'i-lucide-settings-2',
      onSelect: (): void => {
        const panel = overlay.create(WebUiSettingsPanel, { destroyOnClose: true });
        panel.open();
      },
    },
  ],
  ...(auth.status.value?.auth_method === 'session'
    ? [
        [
          {
            label: 'Log out',
            icon: 'i-lucide-log-out',
            color: 'error' as const,
            onSelect: (): void => {
              void logout();
            },
          },
        ],
      ]
    : []),
]);

const handleEvent = (event: ServerEvent): void => {
  if (event.type === 'job.request_result') {
    notifyJobRequestResult(event.payload);
    return;
  }
  if (event.type === 'job.requeue') {
    notifyRequeueOutcome(event.payload);
    return;
  }
  if (event.type === 'job.deletion') {
    if (event.payload.outcome === 'failed') {
      const { job_id: id, reason } = event.payload;
      notify.error(`Deletion failed for job #${id}${reason ? `: ${reason}` : '.'}`);
    }
    return;
  }
  if (event.type !== 'operator.alert') {
    return;
  }

  const { library_name: library, media_path: media, operation, error } = event.payload;
  toast.add({
    title: `${operation} failed`,
    description: `${library ?? 'Unknown library'}: ${media} / ${error}`,
    color: 'error',
  });
};

onMounted(async () => {
  void loadBackground();
  stopEvents = onEvent(handleEvent);
  void ensureProgress();
  document.addEventListener('touchstart', handleSwipeStart, {
    passive: true,
    capture: true,
  });
  document.addEventListener('touchmove', handleSwipeMove, {
    passive: true,
    capture: true,
  });
  document.addEventListener('touchend', handleSwipeEnd, {
    passive: true,
    capture: true,
  });
  document.addEventListener('touchcancel', handleSwipeCancel, {
    passive: true,
    capture: true,
  });

  try {
    system.value = await loadSystem();
  } catch {
    system.value = null;
  } finally {
    systemLoaded.value = true;
  }
});

onBeforeUnmount(() => {
  clearBackground();
  stopEvents?.();
  document.removeEventListener('touchstart', handleSwipeStart, true);
  document.removeEventListener('touchmove', handleSwipeMove, true);
  document.removeEventListener('touchend', handleSwipeEnd, true);
  document.removeEventListener('touchcancel', handleSwipeCancel, true);
});

watch(backgroundEnabled, (enabled) => {
  if (enabled) void loadBackground();
  else clearBackground();
});

watch(isMobile, (v) => {
  if (v) {
    return;
  }

  showSidebar.value = false;
  resetSwipe();
});
</script>

<style>
.shell-stage {
  min-height: 100vh;
  min-height: 100dvh;
}

.shell-root {
  min-height: 100vh;
  min-height: 100dvh;
}

.shell-dashboard {
  min-height: 100vh;
  min-height: 100dvh;
}

.shell-footer {
  padding-top: 1.5rem;
  padding-bottom: 1.5rem;
}
</style>
