<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="changelog">
      <template #actions>
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-refresh-cw"
          :loading="loading"
          @click="reload"
          >{{ error ? 'Retry' : 'Refresh' }}</UButton
        >
      </template>
    </PageHeader>

    <UAlert
      v-if="error"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load commit history"
      :description="error"
    />
    <div v-if="groups.length" class="mw-card flex flex-wrap items-end gap-3 p-3 sm:p-4">
      <UInput
        v-model="query"
        type="search"
        icon="i-lucide-search"
        size="sm"
        placeholder="Search messages, authors, hashes, or tags"
        aria-label="Search commit history"
        class="w-full sm:min-w-56 sm:flex-1 lg:max-w-md"
      />
    </div>
    <div v-if="loading" class="space-y-2">
      <USkeleton v-for="index in 3" :key="index" class="h-24 rounded-xl" />
    </div>
    <UEmpty
      v-else-if="!error && !filteredGroups.length"
      :icon="query.trim() ? 'i-lucide-search-x' : 'i-lucide-scroll-text'"
      :title="query.trim() ? 'No commits match this search.' : 'No commit history.'"
      :description="
        query.trim()
          ? 'Try another message, author, hash, or tag.'
          : 'No commit history has been published yet.'
      "
      variant="naked"
      class="mw-card py-10 sm:py-10 lg:py-10"
    />
    <div v-else-if="!error" class="space-y-5">
      <section v-for="group in filteredGroups" :key="group.tag" class="space-y-3">
        <section class="space-y-3">
          <button
            type="button"
            class="flex w-full flex-wrap items-center justify-between gap-3 text-start"
            :aria-expanded="openReleases.includes(group.tag)"
            @click="toggleRelease(group.tag)"
          >
            <div class="flex min-w-0 items-center gap-3">
              <span class="mw-section-icon">
                <UIcon
                  :name="group.installed ? 'i-lucide-badge-check' : 'i-lucide-git-branch'"
                  class="size-5"
                />
              </span>

              <div class="min-w-0 space-y-1">
                <h2 class="truncate text-base font-semibold text-highlighted">{{ group.tag }}</h2>
                <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-toned">
                  <UTooltip v-if="group.date" :text="`Release Date: ${group.date}`">
                    <span class="inline-flex cursor-help items-center gap-1.5">
                      <UIcon name="i-lucide-calendar-days" class="size-3.5 text-muted" />
                      <span>{{ formatRelativeTime(group.date, 'en') }}</span>
                    </span>
                  </UTooltip>
                </div>
              </div>
            </div>

            <div class="flex shrink-0 items-center gap-2">
              <UBadge
                v-if="group.installed"
                color="success"
                variant="soft"
                size="sm"
                icon="i-lucide-check"
                >Installed</UBadge
              >
              <UBadge color="neutral" variant="outline" size="sm" icon="i-lucide-list"
                >{{ group.commits.length }} commits</UBadge
              >
              <UIcon
                name="i-lucide-chevron-right"
                :class="[
                  'size-4 text-toned transition-transform',
                  openReleases.includes(group.tag) ? 'rotate-90' : '',
                ]"
              />
            </div>
          </button>
          <div v-if="openReleases.includes(group.tag)" class="space-y-2">
            <article
              v-for="commit in group.commits"
              :key="commit.full_sha"
              class="min-w-0 max-w-full rounded-md border border-default bg-elevated/20 px-3 py-3 transition-colors hover:bg-elevated/35"
              dir="ltr"
            >
              <button
                v-if="commit.description.length"
                type="button"
                class="block w-full min-w-0 max-w-full text-start text-sm font-medium leading-5 text-highlighted transition hover:text-primary focus-visible:outline-2 focus-visible:outline-primary"
                :aria-expanded="openDescriptions.includes(commit.sha)"
                @click="toggleDescription(commit.sha)"
              >
                <span class="line-clamp-2 wrap-break-word">{{ commit.title }}</span>
              </button>
              <p
                v-else
                class="line-clamp-2 min-w-0 max-w-full wrap-break-word text-sm font-medium leading-5 text-highlighted"
              >
                {{ commit.title }}
              </p>
              <div v-if="commit.description.length" class="mt-2 min-w-0 max-w-full">
                <button
                  type="button"
                  class="inline-flex max-w-full items-center gap-1 text-xs text-toned hover:text-primary"
                  :aria-expanded="openDescriptions.includes(commit.sha)"
                  aria-label="Show more"
                  @click="toggleDescription(commit.sha)"
                >
                  <UIcon
                    name="i-lucide-chevron-right"
                    :class="[
                      'size-3.5 transition-transform',
                      openDescriptions.includes(commit.sha) ? 'rotate-90' : '',
                    ]"
                  />
                  <span>Show more</span>
                </button>
                <DocumentationMarkdown
                  v-if="openDescriptions.includes(commit.sha)"
                  :markdown="commit.description.join('\n\n')"
                  class="mt-1"
                />
              </div>

              <div
                class="mt-2 flex min-w-0 max-w-full flex-wrap items-center gap-2 text-xs text-toned"
              >
                <span
                  class="inline-flex min-w-0 max-w-full items-center gap-1 rounded-sm border border-default px-2 py-1"
                >
                  <UIcon name="i-lucide-user" class="size-3.5 text-muted" />
                  <span class="wrap-break-word">{{ commit.author }}</span>
                </span>
                <UTooltip :text="`Date: ${commit.date}`">
                  <time
                    :datetime="commit.date"
                    class="inline-flex cursor-help items-center gap-1 rounded-sm border border-default px-2 py-1"
                  >
                    <UIcon name="i-lucide-clock-3" class="size-3.5 text-muted" />
                    <span>{{ formatRelativeTime(commit.date, 'en') }}</span>
                  </time>
                </UTooltip>
                <UTooltip :text="`SHA: ${commit.full_sha}`">
                  <NuxtLink
                    :to="`${repository}/commit/${commit.full_sha}`"
                    target="_blank"
                    class="inline-flex items-center gap-1 rounded-sm border border-default px-2 py-1 font-mono transition hover:border-primary hover:text-primary"
                  >
                    <UIcon name="i-lucide-git-commit-horizontal" class="size-3.5 text-muted" />
                    <span>{{ commit.sha }}</span>
                  </NuxtLink>
                </UTooltip>
                <span
                  v-if="commit.full_sha === installedSha"
                  class="inline-flex items-center gap-1 rounded-sm border border-default px-2 py-1 font-medium"
                >
                  <UIcon name="i-lucide-check" class="size-3.5 text-success" />
                  <span>Installed</span>
                </span>
              </div>
            </article>
          </div>
        </section>
      </section>
    </div>
  </main>
</template>

<script setup lang="ts">
import { useStorage } from '@vueuse/core';
import { computed, onMounted, ref, watch } from 'vue';
import { useSystem } from '~/composables/useSystem';
import { uri } from '~/utils';
import {
  filterChangelog,
  formatCommitMessage,
  initialChangelogReleases,
  isInstalledGroup,
  loadChangelog,
  type ChangelogGroup,
} from '~/utils/changelog';
import { formatRelativeTime } from '~/utils/relativeTime';

const repository = 'https://github.com/arabcoders/millwright';
const { load: loadSystem } = useSystem();
const groups = ref<ChangelogGroup[]>([]);
const installedSha = ref('');
const installedVersion = ref('');
const loading = ref(false);
const error = ref('');
const query = ref('');
const openReleases = useStorage<string[]>('mw_changelog_open_release_tags', []);
const openDescriptions = ref<string[]>([]);
let openDefaultsInitialized = false;
const filteredGroups = computed(() =>
  filterChangelog(groups.value, query.value).map((group) => ({
    ...group,
    installed: isInstalledGroup(group, installedSha.value),
    commits: group.commits.map((commit) => ({
      ...commit,
      ...formatCommitMessage(commit.message),
    })),
  })),
);

const toggleRelease = (tag: string): void => {
  if (openReleases.value.includes(tag)) {
    openReleases.value = openReleases.value.filter((entry) => entry !== tag);
    return;
  }
  openReleases.value = [...openReleases.value, tag];
};

const toggleDescription = (sha: string): void => {
  if (openDescriptions.value.includes(sha)) {
    openDescriptions.value = openDescriptions.value.filter((entry) => entry !== sha);
    return;
  }
  openDescriptions.value = [...openDescriptions.value, sha];
};

const reload = async (): Promise<void> => {
  if (loading.value) return;
  loading.value = true;
  error.value = '';
  try {
    const system = await loadSystem();
    installedSha.value = system?.commit_sha ?? '';
    installedVersion.value = system?.version ?? '';
    groups.value = await loadChangelog(installedVersion.value, uri('/CHANGELOG.json'));
  } catch (cause: unknown) {
    error.value = cause instanceof Error ? cause.message : 'An unknown error occurred.';
  } finally {
    loading.value = false;
  }
};

watch([groups, installedVersion], ([items, version]) => {
  if (openDefaultsInitialized || !version || items.length < 1) return;
  openReleases.value = initialChangelogReleases(items, version, installedSha.value);
  openDefaultsInitialized = true;
});

onMounted(() => void reload());
</script>
