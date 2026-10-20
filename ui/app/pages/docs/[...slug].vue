<template>
  <main class="w-full min-w-0 max-w-full space-y-6">
    <PageHeader :page="entry.id">
      <template #actions>
        <UButton
          v-if="entry.route !== '/docs'"
          to="/docs"
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-book-open"
        >
          All documentation
        </UButton>
      </template>
    </PageHeader>
    <article class="mw-card min-w-0 p-4 sm:p-6 lg:p-7">
      <DocumentationMarkdown :file="entry.file" />
    </article>
  </main>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { getDocumentationEntry } from '~/utils/documentation';

const route = useRoute();
const entry = computed(() => {
  const document = getDocumentationEntry(route.params.slug);
  if (!document) throw createError({ statusCode: 404, statusMessage: 'Document not found.' });
  return document;
});
</script>
