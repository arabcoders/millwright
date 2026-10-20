<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader page="changelog">
      <template #actions>
        <UInput
          v-if="groups.length"
          v-model="query"
          type="search"
          icon="i-lucide-search"
          size="sm"
          placeholder="Search messages, authors, hashes, or tags"
          aria-label="Search commit history"
          class="w-full sm:w-80"
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
      <details
        v-for="(group, index) in visibleGroups"
        :key="group.tag"
        :open="index === 0 || !!query.trim()"
        class="mw-card p-4"
      >
        <summary class="cursor-pointer text-sm font-semibold text-highlighted">
          <span class="ml-2 inline-flex flex-wrap items-center gap-2">
            <span>{{ group.tag }}</span>
            <UBadge
              v-if="group.full_sha === installedGroupSha"
              color="success"
              variant="soft"
              size="sm"
              icon="i-lucide-check"
              >Installed</UBadge
            >
            <UBadge color="neutral" variant="outline" size="sm"
              >{{ group.commits.length }} commits</UBadge
            >
            <time :datetime="group.date" class="font-normal text-toned">
              {{ formatDate(group.date) }}
            </time>
          </span>
        </summary>
        <div class="mt-4 space-y-3">
          <article
            v-for="commit in group.commits"
            :key="commit.full_sha"
            class="min-w-0 rounded-lg border border-default p-3"
          >
            <NuxtLink
              :to="`${repository}/commit/${commit.full_sha}`"
              target="_blank"
              class="wrap-break-word text-sm font-medium text-highlighted hover:text-primary"
              >{{ commit.title }}</NuxtLink
            >
            <UCollapsible v-if="commit.description" class="mt-2">
              <template #default="{ open: expanded }">
                <UButton
                  color="neutral"
                  variant="ghost"
                  size="sm"
                  :trailing-icon="expanded ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
                  >{{ expanded ? 'Hide description' : 'Show description' }}</UButton
                >
              </template>
              <template #content>
                <DocumentationMarkdown :markdown="commit.description" class="mt-3" />
              </template>
            </UCollapsible>
            <div class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-toned">
              <span class="inline-flex items-center gap-1">
                <UIcon name="i-lucide-user" class="size-3.5" />
                {{ commit.author }}
              </span>
              <time :datetime="commit.date">
                {{ formatDate(commit.date) }}
              </time>
              <NuxtLink
                :to="`${repository}/commit/${commit.full_sha}`"
                target="_blank"
                :title="commit.full_sha"
                class="inline-flex items-center gap-1 font-mono hover:text-primary"
              >
                <UIcon name="i-lucide-git-commit-horizontal" class="size-3.5" />
                {{ commit.sha }}
              </NuxtLink>
              <UBadge
                v-if="commit.full_sha === installedSha"
                color="success"
                variant="soft"
                size="sm"
                icon="i-lucide-check"
                >Installed</UBadge
              >
            </div>
          </article>
        </div>
      </details>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useSystem } from '~/composables/useSystem';
import { uri } from '~/utils';
import {
  filterChangelog,
  loadChangelog,
  splitCommitMessage,
  type ChangelogGroup,
} from '~/utils/changelog';
import { formatDate } from '~/utils/date';

const repository = 'https://github.com/arabcoders/millwright';
const { load: loadSystem } = useSystem();
const groups = ref<ChangelogGroup[]>([]);
const installedSha = ref('');
const loading = ref(false);
const error = ref('');
const query = ref('');
const installedGroupSha = computed(
  () =>
    groups.value.find(
      (group) =>
        group.full_sha === installedSha.value ||
        group.commits.some((commit) => commit.full_sha === installedSha.value),
    )?.full_sha,
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

onMounted(() => void reload());
</script>
