<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="changelog">
      <template #actions>
        <UButton
          v-if="groups.length"
          color="neutral"
          :variant="showFilter ? 'soft' : 'outline'"
          size="sm"
          icon="i-lucide-filter"
          :aria-expanded="showFilter"
          aria-controls="changelog-filter"
          @click="showFilter = !showFilter"
          >Filter</UButton
        >
        <UInput
          v-if="showFilter && groups.length"
          id="changelog-filter"
          v-model="query"
          type="search"
          icon="i-lucide-search"
          size="sm"
          placeholder="Search messages, authors, hashes, or tags"
          aria-label="Search commit history"
          class="order-last w-full sm:order-0 sm:w-80"
        />
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
    <div v-if="loading" class="space-y-2">
      <USkeleton v-for="index in 3" :key="index" class="h-24 rounded-xl" />
    </div>
    <UEmpty
      v-else-if="!error && !visibleGroups.length"
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
      <UCollapsible
        v-for="group in visibleGroups"
        :key="group.tag"
        as="section"
        :open="openReleases[group.tag] ?? defaultOpenReleases.includes(group.tag)"
        class="space-y-3"
        @update:open="openReleases[group.tag] = $event"
      >
        <template #default="{ open: expanded }">
          <button
            type="button"
            class="flex w-full flex-wrap items-center justify-between gap-3 text-start"
            :aria-label="`${expanded ? 'Collapse' : 'Expand'} ${group.tag}`"
          >
            <div class="flex min-w-0 items-center gap-3">
              <span class="mw-section-icon">
                <UIcon
                  :name="
                    group.tag === installedGroupTag ? 'i-lucide-badge-check' : 'i-lucide-git-branch'
                  "
                  class="size-5"
                />
              </span>

              <div class="min-w-0 space-y-1">
                <h2 class="truncate text-base font-semibold text-highlighted">{{ group.tag }}</h2>
                <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-toned">
                  <UTooltip :text="formatDate(group.date, 'detail')">
                    <time
                      :datetime="group.date"
                      class="inline-flex cursor-help items-center gap-1.5"
                    >
                      <UIcon name="i-lucide-calendar-days" class="size-3.5 text-muted" />
                      {{ formatRelativeTime(group.date, 'en') }}
                    </time>
                  </UTooltip>
                </div>
              </div>
            </div>

            <div class="flex shrink-0 items-center gap-2">
              <UBadge
                v-if="group.tag === installedGroupTag"
                color="success"
                variant="soft"
                size="sm"
                icon="i-lucide-check"
                >Installed</UBadge
              >
              <UBadge color="neutral" variant="outline" size="sm" icon="i-lucide-list"
                >{{ group.commits.length }}
                {{ group.commits.length === 1 ? 'commit' : 'commits' }}</UBadge
              >
              <UIcon
                name="i-lucide-chevron-right"
                :class="['size-4 text-toned transition-transform', expanded ? 'rotate-90' : '']"
              />
            </div>
          </button>
        </template>

        <template #content>
          <div class="space-y-2">
            <article
              v-for="commit in group.commits"
              :key="commit.full_sha"
              class="min-w-0 max-w-full rounded-md border border-default bg-elevated/20 px-3 py-3 transition-colors hover:bg-elevated/35"
              dir="ltr"
            >
              <NuxtLink
                :to="`${repository}/commit/${commit.full_sha}`"
                target="_blank"
                class="block min-w-0 max-w-full wrap-break-word text-sm font-medium leading-5 text-highlighted transition hover:text-primary"
                >{{ commit.title }}</NuxtLink
              >
              <UCollapsible
                v-if="commit.description"
                v-model:open="openDescriptions[commit.full_sha]"
                class="mt-2 min-w-0 max-w-full"
              >
                <template #default="{ open: expanded }">
                  <button
                    type="button"
                    class="inline-flex max-w-full items-center gap-1 text-xs text-toned hover:text-primary"
                    :aria-label="expanded ? 'Hide commit description' : 'Show commit description'"
                  >
                    <UIcon
                      name="i-lucide-chevron-right"
                      :class="['size-3.5 transition-transform', expanded ? 'rotate-90' : '']"
                    />
                    {{ expanded ? 'Show less' : 'Show more' }}
                  </button>
                </template>
                <template #content>
                  <DocumentationMarkdown
                    :markdown="commit.description"
                    class="changelog-commit-markdown mt-1"
                  />
                </template>
              </UCollapsible>

              <div
                class="mt-2 flex min-w-0 max-w-full flex-wrap items-center gap-2 text-xs text-toned"
              >
                <span
                  class="inline-flex min-w-0 max-w-full items-center gap-1 rounded-sm border border-default px-2 py-1"
                >
                  <UIcon name="i-lucide-user" class="size-3.5 text-muted" />
                  <span class="wrap-break-word">{{ commit.author }}</span>
                </span>
                <UTooltip :text="formatDate(commit.date, 'detail')">
                  <time
                    :datetime="commit.date"
                    class="inline-flex cursor-help items-center gap-1 rounded-sm border border-default px-2 py-1"
                  >
                    <UIcon name="i-lucide-clock-3" class="size-3.5 text-muted" />
                    {{ formatRelativeTime(commit.date, 'en') }}
                  </time>
                </UTooltip>
                <UTooltip :text="`SHA: ${commit.full_sha}`">
                  <NuxtLink
                    :to="`${repository}/commit/${commit.full_sha}`"
                    target="_blank"
                    class="inline-flex items-center gap-1 rounded-sm border border-default px-2 py-1 font-mono transition hover:border-primary hover:text-primary"
                  >
                    <UIcon name="i-lucide-git-commit-horizontal" class="size-3.5 text-muted" />
                    {{ commit.sha }}
                  </NuxtLink>
                </UTooltip>
                <span
                  v-if="commit.full_sha === installedSha"
                  class="inline-flex items-center gap-1 rounded-sm border border-default px-2 py-1 font-medium"
                >
                  <UIcon name="i-lucide-check" class="size-3.5 text-success" />
                  Installed
                </span>
              </div>
            </article>
          </div>
        </template>
      </UCollapsible>
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
  loadChangelog,
  splitCommitMessage,
  type ChangelogGroup,
} from '~/utils/changelog';
import { formatDate } from '~/utils/date';
import { formatRelativeTime } from '~/utils/relativeTime';

const repository = 'https://github.com/arabcoders/millwright';
const { load: loadSystem } = useSystem();
const groups = ref<ChangelogGroup[]>([]);
const installedSha = ref('');
const loading = ref(false);
const error = ref('');
const query = ref('');
const showFilter = ref(false);
const openReleases = useStorage<Record<string, boolean>>('mw_changelog_open_releases', {});
const openDescriptions = ref<Record<string, boolean>>({});
const installedGroupIndex = computed(() =>
  groups.value.findIndex(
    (group) =>
      group.full_sha === installedSha.value ||
      group.commits.some((commit) => commit.full_sha === installedSha.value),
  ),
);
const installedGroupTag = computed(() => groups.value[installedGroupIndex.value]?.tag);
const defaultOpenReleases = computed(() =>
  groups.value.slice(0, Math.max(installedGroupIndex.value, 0) + 1).map((group) => group.tag),
);
const visibleGroups = computed(() =>
  filterChangelog(groups.value, query.value).map((group) => ({
    ...group,
    commits: group.commits.map((commit) => ({
      ...commit,
      ...splitCommitMessage(commit.message),
    })),
  })),
);

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

watch(showFilter, (visible) => {
  if (!visible) query.value = '';
});

onMounted(() => void reload());
</script>

<style scoped>
.changelog-commit-markdown :deep(.docs-markdown) {
  color: var(--ui-text-toned);
  font-size: 0.875rem;
  line-height: 1.625;
}
</style>
