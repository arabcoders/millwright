<template>
  <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
    <StatCard
      label="Active workspaces"
      :value="snapshot?.summary.active_count ?? 'Unavailable'"
      :hint="snapshot?.summary.count == null ? undefined : `${snapshot.summary.count} total`"
      icon="i-lucide-folder-tree"
      to="/workspaces"
    />
    <StatCard
      label="Size used"
      :value="bytes(snapshot?.summary.size_bytes)"
      icon="i-lucide-hard-drive"
      hint="Total file sizes"
      tooltip="Deleting files may free less space when they share disk data."
    />
    <StatCard
      label="Unused workspaces"
      :value="snapshot?.summary.unused_count ?? 'Unavailable'"
      :hint="bytes(snapshot?.summary.unused_size_bytes)"
      icon="i-lucide-trash-2"
      to="/workspaces"
    />
    <StatCard
      label="Free space"
      :value="bytes(snapshot?.summary.free_bytes)"
      icon="i-lucide-hard-drive-download"
    />
  </div>
</template>

<script setup lang="ts">
import type { WorkspaceSnapshot } from '~/types/api';
import { formatBytes } from '~/utils';

defineProps<{ snapshot: WorkspaceSnapshot | null }>();
const bytes = (value: number | null | undefined): string =>
  value == null ? 'Unavailable' : formatBytes(value);
</script>
