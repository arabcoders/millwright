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

    <div class="flex min-w-0 flex-wrap items-center gap-2">
      <UInput
        v-model.lazy="query"
        type="search"
        placeholder="Release, change, author or commit"
        icon="i-lucide-filter"
        size="sm"
        class="w-full sm:min-w-48 sm:flex-1 lg:max-w-md"
        aria-label="Search changelog"
      />
      <UTooltip :text="`${DEFAULT_LIMIT} most recent releases`">
        <USwitch v-model="latestOnly" size="sm" label="Latest only" />
      </UTooltip>
    </div>

    <section aria-label="Release history" :aria-busy="loading" class="space-y-4">
      <UAlert
        v-if="loading"
        color="info"
        variant="soft"
        icon="i-lucide-loader-circle"
        title="Loading changelog"
        :ui="{ icon: 'animate-spin' }"
      />

      <template v-else>
        <UAlert
          v-if="error"
          color="error"
          variant="soft"
          icon="i-lucide-triangle-alert"
          title="Unable to load changelog"
          :description="error"
        />
        <div v-if="filteredGroups.length" class="divide-y divide-default border-y border-default">
          <section
            v-for="(group, index) in filteredGroups"
            :key="group.tag"
            :aria-labelledby="`changelog-release-${index}`"
            class="py-5"
          >
            <h2>
              <button
                :id="`changelog-release-${index}`"
                type="button"
                class="group flex w-full items-start gap-3 rounded-sm text-start focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                :aria-expanded="openReleases.includes(group.tag)"
                :aria-controls="`changelog-commits-${index}`"
                @click="toggleRelease(group.tag)"
              >
                <UIcon
                  name="i-lucide-chevron-right"
                  :class="[
                    'mt-0.5 size-5 shrink-0 text-muted transition-transform',
                    openReleases.includes(group.tag) ? 'rotate-90' : '',
                  ]"
                  aria-hidden="true"
                />
                <span
                  class="flex min-w-0 flex-1 flex-wrap items-center justify-between gap-x-6 gap-y-2"
                >
                  <span class="flex min-w-0 flex-wrap items-center gap-2">
                    <span
                      class="wrap-anywhere font-mono text-base font-semibold text-highlighted transition-colors group-hover:text-primary"
                    >
                      {{ group.tag }}
                    </span>
                    <UBadge
                      v-if="group.installed"
                      color="success"
                      variant="soft"
                      size="sm"
                      icon="i-lucide-check"
                      >Installed</UBadge
                    >
                  </span>
                  <span class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-toned">
                    <time v-if="group.date" :datetime="group.date" :title="group.date">
                      {{ formatDate(group.date, 'date') }}
                    </time>
                    <span class="tabular-nums">
                      {{ group.commits.length }}
                      {{ group.commits.length === 1 ? 'commit' : 'commits' }}
                    </span>
                  </span>
                </span>
              </button>
            </h2>
            <div
              :id="`changelog-commits-${index}`"
              :hidden="!openReleases.includes(group.tag)"
              class="mt-4 sm:ms-8"
            >
              <ul v-if="openReleases.includes(group.tag)" class="divide-y divide-default">
                <li v-for="commit in group.commits" :key="commit.full_sha">
                  <article class="min-w-0 max-w-full space-y-2 py-3" dir="ltr">
                    <NuxtLink
                      :to="`${repository}/commit/${commit.full_sha}`"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="block wrap-anywhere text-sm font-medium leading-6 text-highlighted transition-colors hover:text-primary focus-visible:outline-none focus-visible:underline"
                    >
                      {{ commit.title }}
                    </NuxtLink>
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
                        class="changelog-commit-markdown mt-1"
                      />
                    </div>

                    <div
                      class="flex min-w-0 max-w-full flex-wrap items-center gap-x-4 gap-y-1 text-xs text-toned"
                    >
                      <span class="inline-flex min-w-0 max-w-full items-center gap-1.5">
                        <UIcon name="i-lucide-user" class="size-3.5 text-muted" />
                        <span class="wrap-anywhere">{{ commit.author }}</span>
                      </span>
                      <UTooltip :text="`Date: ${commit.date}`">
                        <time :datetime="commit.date" class="cursor-help">
                          <span>{{ formatRelativeTime(commit.date, 'en') }}</span>
                        </time>
                      </UTooltip>
                      <UTooltip :text="`SHA: ${commit.full_sha}`">
                        <NuxtLink
                          :to="`${repository}/commit/${commit.full_sha}`"
                          target="_blank"
                          rel="noopener noreferrer"
                          :aria-label="`SHA: ${commit.full_sha}`"
                          class="inline-flex items-center gap-1.5 font-mono transition-colors hover:text-primary focus-visible:outline-none focus-visible:underline"
                        >
                          <UIcon
                            name="i-lucide-git-commit-horizontal"
                            class="size-3.5 text-muted"
                          />
                          <span>{{ commit.sha }}</span>
                        </NuxtLink>
                      </UTooltip>
                      <span
                        v-if="
                          matchesSha(commit.full_sha, installedSha) ||
                          matchesSha(commit.sha, installedSha)
                        "
                        class="inline-flex items-center gap-1.5 font-medium text-success"
                      >
                        <UIcon name="i-lucide-check" class="size-3.5 text-success" />
                        <span>Installed</span>
                      </span>
                    </div>
                  </article>
                </li>
              </ul>
              <UEmpty
                v-if="openReleases.includes(group.tag) && !group.commits.length"
                size="sm"
                variant="naked"
                icon="i-lucide-git-commit-horizontal"
                title="No commits"
              />
            </div>
          </section>
        </div>
        <UEmpty
          v-else-if="!error"
          :icon="query.trim() ? 'i-lucide-search' : 'i-lucide-scroll-text'"
          :title="query.trim() ? 'No matching changelog entries' : 'No changelog entries'"
        />
      </template>
    </section>
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
  matchesSha,
  type ChangelogGroup,
} from '~/utils/changelog';
import { formatDate } from '~/utils/date';
import { formatRelativeTime } from '~/utils/relativeTime';

const repository = 'https://github.com/arabcoders/millwright';
const DEFAULT_LIMIT = 10;
const { load: loadSystem } = useSystem();
const groups = ref<ChangelogGroup[]>([]);
const installedSha = ref('');
const loading = ref(false);
const error = ref('');
const query = ref('');
const latestOnly = useStorage<boolean>('mw_changelog_latest_only', true);
const openReleases = useStorage<string[]>('mw_changelog_open_release_tags', []);
const openDescriptions = ref<string[]>([]);
let openDefaultsInitialized = false;
const visibleGroups = computed(() =>
  latestOnly.value ? groups.value.slice(0, DEFAULT_LIMIT) : groups.value,
);
const filteredGroups = computed(() =>
  filterChangelog(visibleGroups.value, query.value).map((group) => ({
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
    groups.value = await loadChangelog(system?.version ?? '', uri('/CHANGELOG.json'));
  } catch (cause: unknown) {
    error.value = cause instanceof Error ? cause.message : 'An unknown error occurred.';
  } finally {
    loading.value = false;
  }
};

watch([groups, installedSha], ([items, sha]) => {
  if (openDefaultsInitialized || items.length < 1) return;
  openReleases.value = initialChangelogReleases(items, sha);
  openDefaultsInitialized = true;
});

onMounted(() => void reload());
</script>

<style>
.changelog-commit-markdown .docs-markdown {
  font-size: 0.875rem;
  color: var(--ui-text-toned);
}
</style>
