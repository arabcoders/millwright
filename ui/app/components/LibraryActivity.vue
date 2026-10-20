<template>
  <template v-if="progress">
    <div v-if="progress.discovery.finished_at">
      <p class="text-xs text-toned">Last scan</p>
      <p>
        <span v-if="progress.discovery.latest_result === 'failed'" class="text-error"
          >Failed ·
        </span>
        <UTooltip :text="formatDate(progress.discovery.finished_at, 'detail')">
          <time :datetime="toIsoString(progress.discovery.finished_at)">{{
            formatRelativeTime(progress.discovery.finished_at, 'en')
          }}</time>
        </UTooltip>
      </p>
    </div>
    <div v-if="progress.scanner.next_run_at">
      <p class="text-xs text-toned">Next scan</p>
      <p>
        <UTooltip :text="formatDate(progress.scanner.next_run_at, 'detail')">
          <time :datetime="toIsoString(progress.scanner.next_run_at)">{{
            formatRelativeTime(progress.scanner.next_run_at, 'en')
          }}</time>
        </UTooltip>
      </p>
    </div>
    <UAlert
      v-if="progress.discovery.error"
      class="sm:col-span-3"
      color="error"
      variant="soft"
      icon="i-lucide-triangle-alert"
      title="Scanner error"
    >
      <template #description>
        <span class="whitespace-pre-wrap break-all">{{ progress.discovery.error }}</span>
      </template>
    </UAlert>
  </template>
</template>

<script setup lang="ts">
import type { LibraryProgress } from '~/types/api';
import { formatDate, toIsoString } from '~/utils/date';
import { formatRelativeTime } from '~/utils/relativeTime';

defineProps<{ progress: LibraryProgress | undefined }>();
</script>
