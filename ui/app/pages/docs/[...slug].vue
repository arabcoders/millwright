<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <NuxtLink
      v-if="entry.route !== '/docs'"
      to="/docs"
      class="inline-flex items-center gap-2 text-sm text-toned hover:text-primary"
    >
      <UIcon name="i-lucide-arrow-left" class="size-4" aria-hidden="true" />
      All documentation
    </NuxtLink>
    <PageHeader v-else :page="entry.id" />

    <template v-if="entry.route === '/docs'">
      <section
        v-for="group in groups"
        :key="group.id"
        :aria-labelledby="`docs-${group.id}`"
        class="space-y-3"
      >
        <h2 :id="`docs-${group.id}`" class="text-sm font-semibold text-highlighted">
          {{ group.title }}
        </h2>
        <ul class="mw-card divide-y divide-default overflow-hidden">
          <li v-for="document in group.entries" :key="document.id">
            <NuxtLink
              :to="document.route"
              class="group flex items-center gap-3 p-4 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary"
            >
              <UIcon :name="document.icon" class="size-5 shrink-0 text-toned" aria-hidden="true" />
              <div class="min-w-0 flex-1 space-y-1">
                <h3
                  class="wrap-anywhere text-sm font-semibold text-highlighted group-hover:text-primary"
                >
                  {{ document.title }}
                </h3>
                <p class="wrap-anywhere text-sm text-toned">{{ document.description }}</p>
              </div>
              <UIcon
                name="i-lucide-chevron-right"
                class="size-4 shrink-0 text-muted"
                aria-hidden="true"
              />
            </NuxtLink>
          </li>
        </ul>
      </section>
    </template>
    <DocumentationMarkdown v-else :file="entry.file" />
  </main>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { getDocumentationEntry, getDocumentationGroups } from '~/utils/documentation';

const route = useRoute();
const groups = getDocumentationGroups();
const entry = computed(() => {
  const document = getDocumentationEntry(route.params.slug);
  if (!document) throw createError({ statusCode: 404, statusMessage: 'Document not found.' });
  return document;
});
</script>
