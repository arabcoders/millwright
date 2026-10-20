<template>
  <UModal
    v-model:open="open"
    title="Choose a folder"
    :ui="{ content: 'w-full sm:max-w-lg', body: 'p-4 sm:p-6' }"
  >
    <template #body>
      <div class="space-y-3">
        <nav
          class="flex min-w-0 items-center gap-1 overflow-x-auto rounded-lg border border-default bg-muted/30 p-1.5"
          aria-label="Path"
        >
          <UButton
            color="neutral"
            variant="ghost"
            size="xs"
            square
            icon="i-lucide-home"
            aria-label="Home"
            title="Home"
            @click="load('/')"
          />
          <template v-for="segment in segments" :key="segment.path">
            <UIcon name="i-lucide-chevron-right" class="size-3 shrink-0 text-muted" />
            <button
              type="button"
              class="flex shrink-0 items-center gap-1 rounded px-1.5 py-1 text-xs text-toned hover:bg-elevated hover:text-highlighted"
              @click="load(segment.path)"
            >
              <UIcon name="i-lucide-folder" class="size-3.5 shrink-0" />
              <span class="truncate" dir="ltr">{{ segment.label }}</span>
            </button>
          </template>
        </nav>

        <div class="space-y-1.5">
          <label for="directory-search" class="text-sm font-medium text-highlighted"
            >Search subdirectories</label
          >
          <UInput
            id="directory-search"
            v-model="query"
            icon="i-lucide-search"
            placeholder="Search subdirectories"
            aria-describedby="directory-search-help"
            class="w-full"
          />
          <p id="directory-search-help" class="text-xs text-toned">
            Filters subdirectories in the current folder.
          </p>
        </div>

        <div class="max-h-64 overflow-y-auto rounded-lg border border-default">
          <button
            v-for="item in filteredItems"
            :key="item.path"
            type="button"
            class="flex w-full items-center gap-2 px-3 py-2 text-start text-sm text-toned hover:bg-muted"
            @click="load(item.path)"
          >
            <UIcon name="i-lucide-folder" class="size-4 shrink-0 text-toned" />
            <span class="truncate" dir="ltr">{{ item.name }}</span>
          </button>
          <UEmpty
            v-if="!loading && !items.length"
            icon="i-lucide-folder-open"
            title="No subdirectories"
            description="This folder has no subdirectories."
            class="p-4"
          />
          <UEmpty
            v-else-if="!loading && !filteredItems.length"
            icon="i-lucide-search-x"
            title="No matching subdirectories"
            :description="`No folders match “${query}”.`"
            class="p-4"
          />
          <div v-if="loading" class="flex justify-center px-3 py-2">
            <UIcon name="i-lucide-loader-circle" class="size-4 animate-spin text-toned" />
          </div>
        </div>
      </div>
    </template>
    <template #footer>
      <div class="flex w-full flex-col gap-2 sm:flex-row sm:justify-end">
        <UButton color="primary" icon="i-lucide-check" @click="select">
          Select this folder
        </UButton>
      </div>
    </template>
  </UModal>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useApiErrorMessage } from '~/composables/useApiErrorMessage';
import type { DirectoryEntry } from '~/types/api';
import { useDirectories } from '~/composables/useDirectories';

const props = withDefaults(defineProps<{ open: boolean; initialPath?: string }>(), {
  initialPath: '/',
});
const emit = defineEmits<{
  'update:open': [value: boolean];
  select: [path: string];
}>();
const notify = useNotification();
const { messageFor } = useApiErrorMessage();
const { load: loadDirectory } = useDirectories();

const open = computed({
  get: () => props.open,
  set: (value: boolean) => emit('update:open', value),
});

const currentPath = ref('/');
const items = ref<DirectoryEntry[]>([]);
const loading = ref(false);
const query = ref('');
let loadGeneration = 0;

const filteredItems = computed(() => {
  const normalized = query.value.trim().toLowerCase();
  return normalized
    ? items.value.filter((item) => item.name.toLowerCase().includes(normalized))
    : items.value;
});

const segments = computed<Array<{ label: string; path: string }>>(() => {
  const parts = currentPath.value.split('/').filter(Boolean);
  const result: Array<{ label: string; path: string }> = [];
  let acc = '';
  for (const part of parts) {
    acc += `/${part}`;
    result.push({ label: part, path: acc });
  }
  return result;
});

const load = async (path: string): Promise<void> => {
  const generation = ++loadGeneration;
  loading.value = true;
  try {
    const result = await loadDirectory(path);
    if (generation !== loadGeneration) return;
    if (!result.ok) {
      notify.error(messageFor(undefined, 'Could not load folders.'));
      return;
    }
    const data = result.data;
    currentPath.value = data.path;
    items.value = data.items;
    query.value = '';
  } catch {
    if (generation !== loadGeneration) return;
    notify.error(messageFor(undefined, 'Could not load folders.'));
  } finally {
    if (generation === loadGeneration) {
      loading.value = false;
    }
  }
};

const select = (): void => {
  emit('select', currentPath.value);
  open.value = false;
};

watch(
  () => props.open,
  (value) => {
    if (value) {
      query.value = '';
      currentPath.value = props.initialPath;
      void load(props.initialPath);
    }
  },
);
</script>
