<template>
  <div class="min-w-0">
    <div v-if="loading" class="flex min-h-40 items-center justify-center text-toned" role="status">
      <UIcon name="i-lucide-loader-circle" class="size-7 animate-spin" />
      <span class="sr-only">Loading documentation</span>
    </div>
    <UAlert
      v-else-if="error"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Could not load documentation"
      :description="error"
    >
      <template #actions>
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-refresh-cw"
          @click="load"
        >
          Retry
        </UButton>
      </template>
    </UAlert>
    <div v-else ref="body" class="docs-markdown" v-html="content" @click="followLink" />
  </div>
</template>

<script setup lang="ts">
import DOMPurify from 'dompurify';
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { ensure_api_success, request, to_api_error } from '~/utils';
import { renderDocumentation } from '~/utils/documentationMarkdown';

const props = defineProps<{ file?: string; markdown?: string }>();
const route = useRoute();
const body = ref<HTMLElement | null>(null);
const text = ref('');
const loading = ref(false);
const error = ref('');
let controller: AbortController | undefined;

// The reader also displays Git commit messages. Never insert their active HTML into the app.
const content = computed(() =>
  DOMPurify.sanitize(renderDocumentation(text.value, props.file, route.path), {
    ADD_ATTR: ['target'],
  }),
);

const scrollToHeading = async (): Promise<void> => {
  await nextTick();
  if (!route.hash || !body.value) return;
  const id = decodeURIComponent(route.hash.slice(1));
  body.value.querySelector(`[id="${CSS.escape(id)}"]`)?.scrollIntoView();
};

const load = async (): Promise<void> => {
  controller?.abort();
  const pending = new AbortController();
  controller = pending;
  error.value = '';
  text.value = '';
  loading.value = true;
  try {
    if (props.markdown !== undefined) text.value = props.markdown;
    else if (props.file) {
      const response = await request(`/api/docs/${props.file}`, {
        signal: pending.signal,
        headers: { Accept: 'text/markdown' },
      });
      await ensure_api_success(response);
      const result = await response.text();
      if (!pending.signal.aborted) text.value = result;
    }
  } catch (cause) {
    if (!pending.signal.aborted) error.value = to_api_error(cause).message;
  } finally {
    if (!pending.signal.aborted) {
      loading.value = false;
      await scrollToHeading();
    }
  }
};

const followLink = async (event: MouseEvent): Promise<void> => {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  )
    return;
  const anchor = (event.target as Element | null)?.closest('a');
  if (!anchor || anchor.target === '_blank' || anchor.hasAttribute('download')) return;
  const url = new URL(anchor.href);
  if (url.origin !== window.location.origin) return;
  if (
    url.pathname !== '/changelog' &&
    url.pathname !== '/docs' &&
    !url.pathname.startsWith('/docs/')
  )
    return;
  event.preventDefault();
  await navigateTo(`${url.pathname}${url.search}${url.hash}`);
  await scrollToHeading();
};

watch([() => props.file, () => props.markdown], load, { immediate: true });
watch(() => route.hash, scrollToHeading);
onBeforeUnmount(() => controller?.abort());
</script>

<style>
.docs-markdown {
  min-width: 0;
  color: var(--ui-text);
  font-size: 0.975rem;
  line-height: 1.75;
}
.docs-markdown > :first-child {
  margin-top: 0;
}
.docs-markdown > :last-child {
  margin-bottom: 0;
}
.docs-markdown :where(h1, h2, h3, h4, h5, h6) {
  margin-top: 1.75em;
  color: var(--ui-text-highlighted);
  font-weight: 700;
  line-height: 1.3;
  overflow-wrap: anywhere;
  scroll-margin-top: 1rem;
}
.docs-markdown h1 {
  font-size: clamp(1.8rem, 1.5rem + 1vw, 2.4rem);
}
.docs-markdown h2 {
  font-size: 1.5rem;
  border-top: 1px solid var(--ui-border);
  padding-top: 0.5rem;
}
.docs-markdown h3 {
  font-size: 1.25rem;
}
.docs-markdown h4 {
  font-size: 1.1rem;
}
.docs-markdown :where(p, ul, ol, pre, blockquote, .markdown-alert, .docs-table, details, hr) {
  margin-top: 1rem;
}
.docs-markdown :where(p, li, blockquote, th, td) {
  overflow-wrap: anywhere;
}
.docs-markdown strong {
  color: var(--ui-text-highlighted);
  font-weight: 700;
}
.docs-markdown :where(ul, ol) {
  padding-left: 1.5rem;
}
.docs-markdown ul {
  list-style: disc;
}
.docs-markdown ol {
  list-style: decimal;
}
.docs-markdown li + li {
  margin-top: 0.35rem;
}
.docs-markdown a {
  color: var(--ui-primary);
  text-decoration: underline;
  text-underline-offset: 0.18em;
}
.docs-markdown a:hover {
  color: var(--ui-text-highlighted);
}
.docs-markdown :not(pre) > code {
  border: 1px solid var(--ui-border);
  border-radius: 0.35rem;
  background: var(--ui-bg-muted);
  padding: 0.1rem 0.3rem;
  font-size: 0.875em;
  overflow-wrap: anywhere;
}
.docs-markdown pre {
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid var(--ui-border);
  border-radius: 0.5rem;
  background: var(--ui-bg-muted);
  padding: 1rem;
}
.docs-markdown pre code {
  white-space: pre;
  font-size: 0.875em;
}
.docs-markdown blockquote {
  border-left: 0.25rem solid var(--ui-border-accented);
  padding: 0.5rem 1rem;
  color: var(--ui-text-toned);
}
.docs-markdown hr {
  border: 0;
  border-top: 1px solid var(--ui-border);
}
.docs-markdown .docs-table {
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid var(--ui-border);
  border-radius: 0.5rem;
}
.docs-markdown table {
  width: max-content;
  min-width: 100%;
  border-collapse: collapse;
}
.docs-markdown :where(th, td) {
  padding: 0.6rem 0.8rem;
  border-bottom: 1px solid var(--ui-border);
  text-align: left;
  vertical-align: top;
}
.docs-markdown th {
  background: var(--ui-bg-muted);
  font-weight: 700;
  color: var(--ui-text-highlighted);
}
.docs-markdown tbody tr:last-child td {
  border-bottom: 0;
}
.docs-markdown img {
  max-width: 100%;
  height: auto;
  border-radius: 0.5rem;
}
.docs-markdown details summary {
  cursor: pointer;
  color: var(--ui-text-highlighted);
}
.docs-markdown .markdown-alert {
  --alert-color: var(--ui-info);
  border-left: 0.25rem solid var(--alert-color);
  border-radius: 0.5rem;
  padding: 0.75rem 1rem;
  background: color-mix(in oklab, var(--alert-color) 8%, var(--ui-bg));
}
.docs-markdown .markdown-alert > :first-child,
.docs-markdown blockquote > :first-child {
  margin-top: 0;
}
.docs-markdown .markdown-alert-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  color: var(--alert-color);
  font-weight: 700;
}
.docs-markdown .markdown-alert-title svg {
  fill: currentColor;
}
.docs-markdown .markdown-alert-tip {
  --alert-color: var(--ui-success);
}
.docs-markdown .markdown-alert-important {
  --alert-color: var(--ui-primary);
}
.docs-markdown .markdown-alert-warning {
  --alert-color: var(--ui-warning);
}
.docs-markdown .markdown-alert-caution {
  --alert-color: var(--ui-error);
}
</style>
