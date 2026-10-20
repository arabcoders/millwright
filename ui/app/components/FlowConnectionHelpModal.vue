<template>
  <UModal
    :open="open"
    title="Connect nodes"
    description="Drag output handles to connect steps."
    :ui="{ content: 'w-full sm:max-w-2xl', body: 'p-4 sm:p-6' }"
    @update:open="$emit('update:open', $event)"
  >
    <template #body>
      <ol class="list-decimal space-y-4 pl-5 text-sm text-toned">
        <li>Drag a labeled output handle to the input connector at the top of another node.</li>
        <li>
          Drag Start's <code class="rounded bg-elevated px-1">next</code> output to select the entry
          node.
        </li>
        <li>
          Conditions use <code class="rounded bg-elevated px-1">true</code> and
          <code class="rounded bg-elevated px-1">false</code>. Processing nodes use outputs such as
          <code class="rounded bg-elevated px-1">Convert</code>/<code
            class="rounded bg-elevated px-1"
            >skip</code
          >
          or <code class="rounded bg-elevated px-1">next</code>.
        </li>
        <li>
          A Result's <code class="rounded bg-elevated px-1">next</code> connects only to action
          nodes and runs after a successful transcode. It does not change the selected result.
        </li>
        <li>Click a line to remove it.</li>
      </ol>
    </template>
  </UModal>
</template>

<script setup lang="ts">
defineProps<{ open: boolean }>();
defineEmits<{ 'update:open': [open: boolean] }>();
</script>
