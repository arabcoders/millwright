<template>
  <AppRoot>
    <div class="shell-surface flex min-h-dvh flex-col">
      <div id="main_container" class="flex min-h-dvh flex-col">
        <UDashboardGroup
          storage="local"
          storage-key="mw-shell"
          class="min-h-dvh flex-1"
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
              <UTooltip :text="connectionStatusLabel">
                <NuxtLink
                  to="/"
                  class="flex w-full min-w-0 items-center gap-3 rounded-md transition-colors hover:bg-elevated/60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                  :class="collapsed ? 'justify-center' : ''"
                  :aria-label="`Home. ${connectionStatusLabel}`"
                  @click="showSidebar = false"
                >
                  <span
                    class="relative inline-flex size-10 shrink-0 items-center justify-center rounded-md border border-default bg-elevated/80 shadow-sm"
                  >
                    <img
                      :src="uri('/images/favicon-192.png')"
                      alt="Millwright"
                      class="size-7 rounded-md object-contain"
                    />
                    <span
                      aria-hidden="true"
                      class="absolute right-0 bottom-0 size-2.5 rounded-full ring-2 ring-default"
                      :class="connectionStatusDotClass"
                    />
                  </span>

                  <div v-if="false === collapsed" class="min-w-0">
                    <p class="truncate text-sm font-semibold text-highlighted">Millwright</p>
                    <p class="truncate text-xs text-toned">Transcode Automation</p>
                  </div>
                </NuxtLink>
              </UTooltip>
            </template>

            <template #default="{ collapsed }">
              <div class="flex h-full flex-col gap-5 px-1 py-1">
                <UNavigationMenu
                  v-model="openNavigationGroups"
                  orientation="vertical"
                  variant="link"
                  :collapsed="collapsed"
                  :items="sidebarItems"
                  :tooltip="true"
                  :popover="{
                    mode: 'click',
                    content: { side: 'right', align: 'start' },
                  }"
                  :ui="navigationUi(collapsed)"
                />
              </div>
            </template>
          </UDashboardSidebar>

          <UDashboardPanel class="min-w-0 bg-transparent" :ui="dashboardPanelUi">
            <template #header>
              <UDashboardNavbar :toggle="false" :title="pageTitle" :ui="dashboardNavbarUi">
                <template #left>
                  <div class="flex min-w-0 shrink-0 items-center gap-2">
                    <UDashboardSidebarToggle class="lg:hidden" />
                    <UDashboardSidebarCollapse class="hidden lg:inline-flex" />
                  </div>
                </template>

                <template #right>
                  <div class="flex min-w-0 shrink items-center gap-0.5 sm:gap-2">
                    <UButton
                      :color="scanner?.paused ? 'warning' : 'neutral'"
                      variant="ghost"
                      size="sm"
                      class="shrink-0"
                      :icon="scanner?.paused ? 'i-lucide-play' : 'i-lucide-pause'"
                      :aria-label="scanner?.paused ? 'Resume activity' : 'Pause activity'"
                      :title="
                        activityDisabledReason ||
                        (scanner?.paused ? 'Resume activity' : 'Pause activity')
                      "
                      :loading="scannerBusy || activityConfirming"
                      :disabled="!!activityDisabledReason"
                      @click="toggleActivity"
                    >
                      <span class="hidden xl:inline">
                        {{ scanner?.paused ? 'Resume activity' : 'Pause activity' }}
                      </span>
                    </UButton>

                    <UTooltip text="Search">
                      <UButton
                        color="neutral"
                        variant="ghost"
                        size="sm"
                        icon="i-lucide-search"
                        aria-label="Search"
                        class="shrink-0"
                        @click="openSearch"
                      />
                    </UTooltip>

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
                  class="relative isolate flex min-h-0 min-w-0 max-w-full flex-1 flex-col px-4 py-4 sm:px-5 sm:py-5 lg:px-6"
                >
                  <div
                    v-if="backgroundImage"
                    aria-hidden="true"
                    class="shell-header-artwork pointer-events-none absolute inset-x-0 top-0 -z-10 h-80"
                    :style="{
                      backgroundImage: `url(${backgroundImage})`,
                      opacity: backgroundVisibility,
                    }"
                  />
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

                  <footer class="mt-6 border-t border-default py-6 text-sm text-toned">
                    <div class="grid gap-6 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
                      <div class="min-w-0 space-y-3">
                        <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
                          <NuxtLink
                            href="https://github.com/arabcoders/millwright"
                            target="_blank"
                            rel="noopener noreferrer"
                            class="inline-flex items-center gap-2 font-semibold text-highlighted hover:text-primary hover:underline underline-offset-2"
                          >
                            <UIcon name="i-lucide-github" class="size-4" aria-hidden="true" />
                            <span>Millwright</span>
                          </NuxtLink>
                          <span v-if="system?.version" class="text-xs">{{ system.version }}</span>
                        </div>
                        <div
                          v-if="system"
                          class="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 text-xs"
                        >
                          <span class="inline-flex items-center gap-1.5">
                            <UIcon name="i-lucide-calendar" class="size-4" aria-hidden="true" />
                            <span>
                              Build
                              {{
                                system.build_date === 'unknown'
                                  ? 'Unknown'
                                  : formatDate(system.build_date, 'date')
                              }}
                            </span>
                          </span>
                          <span class="inline-flex min-w-0 items-center gap-1.5">
                            <UIcon
                              name="i-lucide-git-branch"
                              class="size-4 shrink-0"
                              aria-hidden="true"
                            />
                            <span class="sr-only">Branch:</span>
                            <span class="wrap-anywhere font-mono">{{ system.branch }}</span>
                          </span>
                          <UTooltip
                            v-if="system.commit_sha !== 'unknown'"
                            :text="system.commit_sha"
                          >
                            <NuxtLink
                              :href="`https://github.com/arabcoders/millwright/commit/${system.commit_sha}`"
                              target="_blank"
                              rel="noopener noreferrer"
                              class="inline-flex items-center gap-1.5 font-mono hover:text-primary hover:underline underline-offset-2"
                            >
                              <UIcon
                                name="i-lucide-git-commit-horizontal"
                                class="size-4"
                                aria-hidden="true"
                              />
                              <span class="sr-only">Commit:</span>
                              {{ system.commit_sha.slice(0, 7) }}
                            </NuxtLink>
                          </UTooltip>
                        </div>
                      </div>
                      <div
                        v-if="system"
                        class="min-w-0 space-y-3 border-t border-default pt-4 lg:border-t-0 lg:border-s lg:ps-6 lg:pt-0"
                      >
                        <p class="text-xs font-semibold uppercase tracking-[0.2em] text-toned">
                          Runtime
                        </p>
                        <dl class="grid grid-cols-2 gap-4 text-xs">
                          <div class="min-w-0 space-y-1.5">
                            <dt class="text-muted">Python</dt>
                            <dd class="wrap-anywhere font-mono text-default">
                              {{ system.python }}
                            </dd>
                          </div>
                          <div v-if="system.protocol_version" class="min-w-0 space-y-1.5">
                            <dt class="text-muted">Protocol</dt>
                            <dd class="wrap-anywhere font-mono text-default">
                              {{ system.protocol_version }}
                            </dd>
                          </div>
                        </dl>
                      </div>
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
import { useMediaQuery, useMounted, useStorage } from '@vueuse/core';
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
const { connected, on: onEvent } = useEvents();
let stopEvents: (() => void) | null = null;
const mounted = useMounted();
const disconnected = computed(() => mounted.value && !connected.value);
const connectionStatusLabel = computed(() => {
  if (connected.value) return 'Live updates connected.';
  return disconnected.value
    ? 'Live updates disconnected. Reconnecting.'
    : 'Live updates connecting.';
});
const connectionStatusDotClass = computed(() => (connected.value ? 'bg-success' : 'bg-warning'));
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

const dashboardSidebarUi = {
  root: 'shell-surface border-e border-default bg-transparent',
  header: 'px-3 py-5',
  body: 'gap-3 px-2.5 py-3',
  footer: 'border-t border-default px-2.5 py-3',
};

const dashboardNavbarUi = {
  root: 'border-b border-default bg-transparent px-4 py-3 backdrop-blur-md sm:px-5 lg:px-6',
  title: 'hidden text-sm font-medium text-muted md:flex',
  right: 'flex min-w-0 shrink items-center gap-0.5 sm:gap-1.5',
};

const dashboardPanelUi = {
  root: 'min-w-0 min-h-screen max-w-full flex flex-1 flex-col bg-transparent',
  body: 'flex min-h-0 min-w-0 max-w-full flex-1 flex-col overflow-y-visible p-0',
};

const navigationUi = (collapsed: boolean) => ({
  root: 'w-full',
  list: 'gap-1.5',
  link: collapsed
    ? 'justify-center rounded-md px-2 py-2 text-default'
    : 'rounded-md px-2.5 py-2 text-sm font-medium text-default transition-colors',
  linkLeadingIcon: collapsed ? 'size-5' : 'size-4',
  linkLabel: collapsed ? 'hidden' : 'truncate',
  childList: collapsed ? '' : 'ms-4 border-s border-default ps-2',
  childLink: 'py-2 text-default before:bg-transparent',
  childLinkIcon: 'group-data-[active]:text-primary',
});

const makeNavigationItem = (item: NavItem, activeId?: string): NavigationMenuItem => ({
  label: item.label,
  icon: item.icon,
  to: item.to,
  exact: item.matchPath === '/',
  value: item.id,
  active: item.id === activeId,
  onSelect: (): void => {
    showSidebar.value = false;
  },
});

const sidebarItems = computed<NavigationMenuItem[]>(() => {
  const items = getNavItems().filter((entry) => entry.sidebarVisible);
  const activeId = getActiveNavItem(route, items)?.id;
  return getNavSections().map((section) => ({
    value: section.id,
    label: section.label,
    'aria-label': section.label,
    icon: section.icon,
    active: activeNavItem.value?.section === section.id,
    children: items
      .filter((entry) => entry.section === section.id)
      .map((entry) => makeNavigationItem(entry, activeId)),
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
const openNavigationGroups = useStorage<string[]>(
  'mw-sidebar-open-groups',
  activeNavItem.value ? [activeNavItem.value.section] : [],
);

watch(
  () => route.path,
  () => {
    const section = activeNavItem.value?.section;
    if (section && !openNavigationGroups.value.includes(section)) {
      openNavigationGroups.value = [...openNavigationGroups.value, section];
    }
    showSidebar.value = false;
  },
  { immediate: true },
);

const pageTitle = computed(() => activeNavItem.value?.label ?? 'Millwright');

const documentTitle = computed(() => activeNavItem.value?.label ?? 'Millwright');

useHead(() => ({
  title: formatPageTitle(documentTitle.value),
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
    {
      label: 'Refresh all data',
      icon: 'i-lucide-refresh-cw',
      onSelect: (): void => {
        reloadNuxtApp({ force: true });
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
  notify.error(`${operation} failed: ${library ?? 'Unknown library'}: ${media} / ${error}`);
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
