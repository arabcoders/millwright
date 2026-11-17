<template>
  <UModal
    :open="open"
    title="Connect nodes"
    description="Connect steps on the canvas or in step settings."
    :ui="{ content: 'w-full sm:max-w-2xl', body: 'p-4 sm:p-6' }"
    @update:open="$emit('update:open', $event)"
  >
    <template #body>
      <ol class="list-decimal space-y-4 pl-5 text-sm text-toned">
        <li>
          Drag a labeled output handle to another node's input connector. Arrange changes the
          connection direction.
        </li>
        <li>
          Select a node with Enter or Space. Use Connections in its settings to choose or disconnect
          each output.
        </li>
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
        <li>
          Drag either end of a line to reconnect it. Press Delete on a selected step to remove it.
        </li>
      </ol>
    </template>
  </UModal>
</template>

<script setup lang="ts">
defineProps<{ open: boolean }>();
defineEmits<{ 'update:open': [open: boolean] }>();
</script>
