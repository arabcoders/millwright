<template>
  <aside
    v-if="node || entrySelected"
    class="w-full min-w-0 space-y-4 rounded-lg border border-default p-4"
  >
    <div>
      <h2 class="text-base font-semibold text-highlighted">
        {{ entrySelected ? 'Start settings' : 'Step settings' }}
      </h2>
      <p class="text-sm text-toned">{{ summary }}</p>
      <p v-if="entrySelected" class="mt-2 text-sm text-toned">
        Checked once before any steps run. Leave empty to allow any media file. Files that do not
        match skip this flow.
      </p>
      <template v-if="node?.kind === 'plugin' && pluginManifest">
        <p class="mt-2 text-sm text-toned">{{ pluginManifest.description }}</p>
        <section v-if="pluginManifest.help" class="mt-3 space-y-1">
          <h3 class="text-sm font-medium text-highlighted">Help</h3>
          <p class="text-sm text-toned" :class="{ 'line-clamp-2': !helpExpanded }">
            {{ pluginManifest.help }}
          </p>
          <button
            type="button"
            class="flex w-full items-center justify-end gap-1.5 text-sm font-medium text-primary hover:underline"
            :aria-label="helpExpanded ? 'Collapse help' : 'Expand help'"
            :aria-expanded="helpExpanded"
            @click="helpExpanded = !helpExpanded"
          >
            <UIcon
              :name="helpExpanded ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
              class="size-3.5 shrink-0"
            />
          </button>
        </section>
      </template>
    </div>
    <template v-if="readonly"
      ><div v-if="node?.kind === 'plugin'" class="space-y-3">
        <UBadge>{{ kind }}</UBadge>
        <p class="text-sm">Plugin: {{ node.plugin }}</p>
        <p v-if="!pluginManifest" class="text-sm text-toned">
          Plugin metadata is unavailable. Showing stored inputs.
        </p>
        <UFormField
          v-for="field in readonlyFields"
          :key="field.name"
          :label="field.label"
          :help="field.help"
        >
          <ol
            v-if="Array.isArray(field.value) && field.value.length"
            class="list-decimal space-y-1 pl-5 text-sm"
          >
            <li
              v-for="(value, index) in field.value"
              :key="index"
              class="whitespace-pre-wrap wrap-break-word"
            >
              {{ displayValue(value) }}
            </li>
          </ol>
          <p v-else class="text-sm whitespace-pre-wrap wrap-break-word">
            {{ displayValue(field.value) }}
          </p>
        </UFormField>
        <p v-if="!readonlyFields.length" class="text-sm text-toned">No inputs stored.</p>
        <UCollapsible :key="node.uid">
          <template #default="{ open }">
            <UButton
              color="neutral"
              variant="ghost"
              size="xs"
              class="flex w-full justify-end"
              :trailing-icon="open ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
              >Stored inputs (JSON)</UButton
            >
          </template>
          <template #content>
            <pre class="mt-2 text-xs whitespace-pre-wrap wrap-break-word">{{
              JSON.stringify(node.rawInputs, null, 2)
            }}</pre>
          </template>
        </UCollapsible>
      </div>
      <div v-else-if="readonlyCondition" class="space-y-2">
        <p v-if="readonlyCondition.mode === 'custom'" class="text-sm text-toned">
          Custom nested condition
        </p>
        <p v-else-if="!readonlyCondition.rows.length" class="text-sm text-toned">
          {{
            entrySelected
              ? 'No entry condition. Any media file can enter this flow.'
              : 'No rules stored.'
          }}
        </p>
        <template v-else>
          <p class="text-sm font-medium">
            {{ readonlyCondition.mode === 'all' ? 'All rules must match' : 'Any rule may match' }}
          </p>
          <ul class="list-disc space-y-2 pl-5 text-sm">
            <li v-for="rule in readonlyCondition.rows" :key="rule.uid">
              {{ conditionRule(rule) }}
            </li>
          </ul>
        </template>
        <UCollapsible v-if="storedWhen" :key="entrySelected ? 'start' : node?.uid">
          <template #default="{ open }">
            <UButton
              color="neutral"
              variant="ghost"
              size="xs"
              class="flex w-full justify-end"
              :trailing-icon="open ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
              >Condition (JSON)</UButton
            >
          </template>
          <template #content>
            <pre class="mt-2 text-xs whitespace-pre-wrap wrap-break-word">{{
              JSON.stringify(storedWhen, null, 2)
            }}</pre>
          </template>
        </UCollapsible>
      </div>
      <div v-else-if="node?.kind === 'terminal'">
        <div class="font-medium">Terminal outcome</div>
        <p class="text-sm text-toned">{{ node.outcome }}</p>
      </div>
    </template>
    <div v-else-if="entrySelected" class="space-y-3">
      <ConditionEditor
        v-model="entryCondition"
        label="Run only when"
        empty-hint="No condition. Any media file can enter this flow."
        :facet-options="facetOptions"
        :facts="facts"
      />
      <UButton
        v-if="entryCondition.rows.length || entryCondition.mode === 'custom'"
        type="button"
        color="neutral"
        variant="outline"
        size="xs"
        icon="i-lucide-x"
        @click="entryCondition = emptyCondition()"
        >Clear condition</UButton
      >
    </div>
    <template v-else-if="node?.kind === 'plugin'"
      ><UBadge>{{ kind }}</UBadge
      ><UFormField label="Plugin"
        ><USelectMenu
          :model-value="node.plugin"
          :items="pluginOptions"
          value-key="value"
          class="w-full"
          @update:model-value="pick(String($event))" /></UFormField
      ><UFormField
        v-for="field in fields"
        :key="field.name"
        :label="fieldLabel(field)"
        :help="field.help ?? undefined"
        :required="field.required"
        :error="field.required && node.inputs[field.name] === undefined ? 'Required' : undefined"
        ><USelectMenu
          v-if="isNotificationTargets(field)"
          :model-value="selectedValues(field)"
          :items="props.notificationTargets"
          multiple
          class="w-full"
          @update:model-value="setValues(field, $event)"
        /><UInputMenu
          v-else-if="encoderOptions(field)"
          :model-value="String(node.inputs[field.name] ?? '')"
          :items="encoderOptions(field) ?? []"
          class="w-full"
          @update:model-value="node.inputs[field.name] = String($event ?? '')"
        /><USelectMenu
          v-else-if="field.name === 'audio_when_codecs'"
          :model-value="selectedValues(field)"
          :items="[...new Set([...codecOptions.audioDecoders, ...selectedValues(field)])]"
          multiple
          create-item
          class="w-full"
          @update:model-value="setValues(field, $event)"
          @create="setValues(field, [...selectedValues(field), $event])"
        /><USelectMenu
          v-else-if="field.type === 'strings' && field.values"
          :model-value="selectedValues(field)"
          :items="field.values"
          multiple
          class="w-full"
          @update:model-value="setValues(field, $event)"
        /><USelectMenu
          v-else-if="field.values"
          :model-value="String(node.inputs[field.name] ?? '')"
          :items="field.values"
          class="w-full"
          @update:model-value="node.inputs[field.name] = String($event)"
        /><UInput
          v-else-if="field.type === 'int' || field.type === 'float'"
          :model-value="String(node.inputs[field.name] ?? '')"
          @update:model-value="node.inputs[field.name] = $event === '' ? '' : Number($event)"
          type="number"
          :step="field.type === 'float' ? 'any' : 1"
          :min="field.min ?? undefined"
          :max="field.max ?? undefined"
          class="w-full"
        /><USwitch
          v-else-if="field.type === 'bool'"
          :model-value="node.inputs[field.name] === true"
          @update:model-value="node.inputs[field.name] = $event"
        /><UTextarea
          v-else-if="field.type === 'text'"
          :model-value="String(node.inputs[field.name] ?? '')"
          :rows="3"
          class="w-full"
          @update:model-value="node.inputs[field.name] = String($event)"
        />
        <div v-else-if="field.type === 'strings'" class="space-y-2">
          <div v-for="(value, index) in selectedValues(field)" :key="index" class="flex gap-2">
            <UInput
              :model-value="value"
              :aria-label="`${fieldLabel(field)} ${index + 1}`"
              class="min-w-0 flex-1"
              @update:model-value="
                setValues(
                  field,
                  selectedValues(field).map((item, i) => (i === index ? String($event) : item)),
                )
              "
            />
            <UButton
              type="button"
              color="neutral"
              variant="outline"
              icon="i-lucide-trash-2"
              :aria-label="`Remove ${fieldLabel(field)} ${index + 1}`"
              @click="
                setValues(
                  field,
                  selectedValues(field).filter((_, i) => i !== index),
                )
              "
            />
          </div>
          <UButton
            type="button"
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-plus"
            @click="setValues(field, [...selectedValues(field), ''])"
            >Add value</UButton
          >
        </div>
        <PasswordInput
          v-else-if="field.type === 'password'"
          :key="`${node.uid}:${node.plugin}`"
          :model-value="String(node.inputs[field.name] ?? '')"
          @update:model-value="node.inputs[field.name] = String($event)"
          class="w-full"
        />
        <UInput
          v-else
          :model-value="String(node.inputs[field.name] ?? '')"
          @update:model-value="node.inputs[field.name] = String($event)"
          class="w-full"
        />
        <p
          v-if="isNotificationTargets(field) && props.notificationTargetError"
          class="mt-1 text-sm text-error"
        >
          {{ props.notificationTargetError }}
        </p>
        <p
          v-else-if="isNotificationTargets(field) && !props.notificationTargets.length"
          class="mt-1 text-sm text-toned"
        >
          No targets configured. Add one on the Notifications page.
        </p></UFormField
      ></template
    ><ConditionEditor
      v-else-if="node?.kind === 'condition'"
      v-model="node.condition"
      :facet-options="facetOptions"
      :facts="facts"
    /><UFormField v-else-if="node?.kind === 'terminal'" label="Outcome"
      ><USelectMenu v-model="node.outcome" :items="['done', 'skip', 'veto']" class="w-full"
    /></UFormField>
  </aside>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { CodecOptions, FlowFact, PluginInputField, PluginRow, WhenFacets } from '~/types/api';
import type { DraftNode } from '~/utils/stepDraft';
import { initializeInputs } from '~/utils/stepDraft';
import { conditionRule, emptyCondition, type DraftCondition } from '~/utils/condition';
import ConditionEditor from './ConditionEditor.vue';
const node = defineModel<DraftNode | null>('node', { default: null });
const entryCondition = defineModel<DraftCondition>('entryCondition', { default: emptyCondition });
const props = defineProps<{
  plugins: PluginRow[];
  codecOptions: CodecOptions;
  facetOptions: string[];
  facts: FlowFact[];
  notificationTargets: string[];
  notificationTargetError: string;
  readonly?: boolean;
  storedWhen?: WhenFacets;
  entrySelected?: boolean;
}>();
const row = (id: string) => props.plugins.find((p) => p.manifest.id === id);
const kind = computed(() =>
  node.value?.kind === 'plugin' ? (row(node.value.plugin)?.manifest.kind ?? 'plugin') : '',
);
const summary = computed(() =>
  props.entrySelected
    ? 'Flow-wide eligibility'
    : node.value?.kind === 'plugin'
      ? (row(node.value.plugin)?.manifest.name ?? node.value.plugin)
      : node.value?.kind === 'condition'
        ? 'Condition'
        : 'Result',
);
const readonlyCondition = computed(() =>
  props.entrySelected
    ? entryCondition.value
    : node.value?.kind === 'condition'
      ? node.value.condition
      : null,
);
const pluginManifest = computed(() =>
  node.value?.kind === 'plugin' ? row(node.value.plugin)?.manifest : undefined,
);
const helpExpanded = ref(false);
watch(
  () => [node.value?.uid, node.value?.kind === 'plugin' ? node.value.plugin : undefined],
  () => {
    helpExpanded.value = false;
  },
);
const readable = (name: string) =>
  name.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
const fieldLabel = (field: PluginInputField) =>
  'label' in field && typeof field.label === 'string' ? field.label : readable(field.name);
const encoderOptions = (field: PluginInputField): string[] | null => {
  if (field.name === 'audio_codec') return props.codecOptions.audioEncoders;
  if (
    field.name === 'encoder' &&
    node.value?.kind === 'plugin' &&
    (node.value.plugin === 'transcode.video' || node.value.plugin === 'transcode.burn_subtitles')
  )
    return props.codecOptions.videoEncoders;
  return null;
};
const fields = computed<PluginInputField[]>(() =>
  node.value?.kind === 'plugin' ? (row(node.value.plugin)?.inputs ?? []) : [],
);
const readonlyFields = computed(() => {
  if (node.value?.kind !== 'plugin') return [];
  const values = node.value.rawInputs;
  return [...new Set([...fields.value.map((field) => field.name), ...Object.keys(values)])].map(
    (name) => {
      const field = fields.value.find((field) => field.name === name);
      return {
        name,
        label: field ? fieldLabel(field) : readable(name),
        help: field?.help ?? undefined,
        value: values[name],
      };
    },
  );
});
const displayValue = (value: unknown): string => {
  if (value === undefined) return 'Not stored';
  if (value === null) return 'Not set (null)';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (Array.isArray(value) && !value.length) return 'Empty list';
  if (typeof value === 'object') return 'Structured value (see JSON below)';
  return value === '' ? 'Empty string' : String(value);
};
const pluginOptions = computed(() =>
  props.plugins.map((p) => ({ label: p.manifest.name, value: p.manifest.id })),
);
const pick = (id: string) => {
  if (!node.value || node.value.kind !== 'plugin') return;
  if (node.value.plugin === id) return;
  node.value.plugin = id;
  node.value.configVersion = row(id)!.manifest.config_version;
  node.value.rawInputs = {};
  node.value.initialInputs = {};
  node.value.inputs = {};
  initializeInputs(node.value, row(id)?.inputs ?? []);
};
const isNotificationTargets = (field: PluginInputField) =>
  node.value?.kind === 'plugin' &&
  node.value.plugin === 'action.notify' &&
  field.name === 'targets';
const selectedValues = (field: PluginInputField): string[] => {
  const value = node.value?.kind === 'plugin' ? node.value.inputs[field.name] : undefined;
  return Array.isArray(value) ? value : [];
};
const setValues = (field: PluginInputField, values: unknown) => {
  if (node.value?.kind === 'plugin')
    node.value.inputs[field.name] = Array.isArray(values) ? values.map(String) : [];
};
</script>
