# Plugin authoring

Write a plugin when you need a flow step that the bundled ones don't provide. A plugin receives media
information and settings, then returns a decision: continue, skip, reject, request a conversion, or
request an action. Millwright handles execution and file replacement.

For building flows with existing steps, see [Flow recipes](recipes.md). The [bundled plugins](../plugins/)
are working examples for each plugin kind.

## Create a plugin

A plugin directory contains `plugin.toml` and `plugin.py`. Create a directory named `minimum_duration`
for this example, which adds an inspector that skips files shorter than a chosen duration.

Create `plugin.toml`:

```toml
id = "inspector.minimum_duration"
kind = "inspector"
name = "Minimum duration"
description = "Skips files shorter than the configured duration."
author = "Example author"
url = "https://github.com/arabcoders/millwright"
version = "1.0.0"
```

Create `plugin.py` beside it:

```python
from common.ctx import PluginContext
from pydantic import BaseModel, Field


class Inputs(BaseModel):
    min_duration_s: float = Field(default=60, ge=0, description="Minimum duration in seconds")


INPUTS = Inputs


def decide(ctx: PluginContext, inputs: Inputs) -> dict:
    if ctx.probe.duration_s < inputs.min_duration_s:
        return {"type": "skip", "reason": "Shorter than the minimum duration."}
    return {"type": "match"}
```

Put the directory under `~/.config/millwright/plugins/`, or `/config/millwright/plugins/` in a container.
`MW_CONFIG_PATH` changes that configuration directory. Check that the plugin appears on **Plugins**,
then add it to a flow. Connect `match` to the next processing step and `skip` to a Skip result.
If you created the server's `plugins/` directory while it was running, restart it once to pick up that
directory. Subsequent plugin edits reload without restarting.

`decide` also runs during previews. It should work from the supplied context and inputs without changing
files, starting tools, or making network requests. Return a plan or action for work that changes media.
Standard output carries the plugin protocol, so printing there breaks communication with the host.

Plugins are trusted Python code, not sandboxed extensions. Only install code you trust. If you distribute
a plugin as a ZIP, it can be installed through `POST /api/plugins`. Uploaded IDs can't replace bundled
ones. Reinstalling an uploaded ID replaces that uploaded plugin.

## Choose a plugin kind

The kind determines which outputs appear in the flow editor:

| Kind        | Use it for                                                 | Decisions and ports                    |
| ----------- | ---------------------------------------------------------- | -------------------------------------- |
| `filter`    | Allow or reject input                                      | `match`, `veto`                        |
| `inspector` | Inspect media before or after processing                   | `match`, `veto`, `skip`                |
| `transcode` | Request a processing stage                                 | `plan`, `skip`                         |
| `action`    | Request an action without selecting the flow branch itself | `actions`, followed by the `next` port |

For example, a duration inspector returns `match` when the next step should run. A transcode returns
`skip` if the file already meets its requirements. Include a readable `reason` with `skip` and `veto`
so someone opening the job can understand the decision.

Return an error when the plugin can't make a valid decision:

```python
return {"type": "error", "error": "Required source metadata is unavailable."}
```

That stops traversal and records the error against the step. It isn't the same as deliberately skipping a file.

## Describe settings and requirements

`INPUTS` is a Pydantic model. Its defaults, bounds, descriptions, and choices become the settings form
in the editor. Use `Enum` or `Literal` for choices, `list[str]` for string lists, and
`common.plugin.TextField` for multiline text. A password-style field uses
`Field(json_schema_extra={"ui": "password"})`, but masking the form doesn't remove its value from exports.

Custom Pydantic validators run when a flow is saved, before `decide` is called. Unknown settings and
booleans supplied as numeric values are rejected. Without an `INPUTS` model, `decide` receives the raw
settings dictionary.

The manifest requires `id`, `kind`, `name`, `description`, `author`, `url`, and `version`. The ID starts
with its kind and uses lowercase letters, digits, and underscores after the dot, such as
`inspector.minimum_duration`. The suffix `media` is reserved. See
[PluginManifest](../server/src/server/plugins/manifest.py) for validation rules.

Optional manifest settings:

| Field               | Meaning                                                                       |
| ------------------- | ----------------------------------------------------------------------------- |
| `help`              | Longer guidance shown to the person configuring the step                      |
| `facets`            | Named values the plugin can produce, with types for editor suggestions        |
| `requires`          | Worker tags, such as `cpu` or `gpu`. At least one must match                  |
| `requires_encoders` | Fixed encoder requirements. Every listed encoder must be available            |
| `require_plugins`   | Plugins that must have run earlier on every incoming path                     |
| `config_version`    | Positive integer version of plugin configuration and records. Defaults to `1` |
| `migrations`        | Versioned renames for flow settings, actions, events, and delivery labels     |

For an encoder chosen by the step's settings, put that choice in the plan rather than a fixed manifest
requirement. Worker requirements apply to steps actually visited, not every branch in the graph.

### Upgrade saved configurations and records

Keep the plugin release `version` separate from `config_version`. Increase `config_version` when saved
settings or records need a migration. Each saved plugin node records its own `config_version`. Existing
nodes without that field are version `1`. New steps use the installed plugin's current configuration
version. Actions emitted by plugins also carry `plugin_id` and `config_version` metadata.

Declare migrations in `plugin.toml`, alongside the other manifest fields. For example, the external
notification plugin's configuration version `2` renames its step, action, event, progress field, and
delivery prefix:

```toml
id = "action.notify_external"
version = "1.1.0"
config_version = 2
migrations = [{ from_version = 1, to_version = 2, changes = [
  { type = "rename_plugin", from = "action.notify_media_manager", to = "action.notify_external" },
  { type = "rename_action", from = "notify_media_manager", to = "notify_external" },
  { type = "rename_event", from = "media_manager_notification", to = "external_notification" },
  { type = "rename_field", scope = "event", from = "refresh", to = "progress" },
  { type = "rename_target_prefix", from = "media-manager:", to = "external:" },
] }]
```

The supported changes are:

| Type                   | What it renames                                          |
| ---------------------- | -------------------------------------------------------- |
| `rename_plugin`        | A flow node's plugin ID                                  |
| `rename_action`        | A typed action's `kind`                                  |
| `rename_event`         | A persisted event's type                                 |
| `rename_field`         | A field in plugin inputs, an action, or an event payload |
| `rename_target_prefix` | The prefix of associated notification delivery labels    |

For `rename_field`, `scope` defaults to `inputs`. Use `action` or `event` for those record fields.
For example, `{ type = "rename_field", from = "old_name", to = "new_name" }` renames an input.
Update the plugin's `INPUTS` model to use the new name too. Keep previous IDs in migration declarations,
not as aliases in the current manifest or plugin code. Older actions without plugin metadata are matched
through explicit `rename_action` declarations, not by guessing a plugin from an action name.

Provide every transition needed to reach the current configuration version. Unknown operations, missing
transitions, and conflicting renames report errors without saving partial changes. Migrations cover
Millwright's defined flow, action, and event formats, not arbitrary custom record shapes. They can't
transform values or run arbitrary Python upgrade code.

The server upgrades saved flows on startup and plugin updates, increasing the flow version when its
definition changes. It also applies migrations before saving, previewing, or executing supplied flows.
Unfinished jobs, pending notification deliveries, and unfinished post-actions use the same declarations
before current record models parse them. The server reads this work in batches. Finished job history,
completed attempts, and delivered notifications stay unchanged. A finished job can still have pending
notifications or post-actions, and only that remaining work is migrated. The editor preserves loaded
configuration versions rather than applying migrations itself.

If you retry a finished job, its saved execution configuration is migrated when the retry is requested.

Unrelated configurations stay unchanged. After a successful commit, logs identify the plugin, record,
version transition, and changes applied.

## Read media information

The [PluginContext](../common/src/common/ctx.py) supplies:

| Field               | What it contains                                          |
| ------------------- | --------------------------------------------------------- |
| `probe`             | Media information for the current input                   |
| `source_probe`      | The completed stage's input when inspecting its output    |
| `original_filename` | Original library basename, including extension            |
| `library`           | Library information, currently its name                   |
| `facets`            | Built-in media facts and values from earlier plugins      |
| `sidecar_subtitles` | Candidate subtitle basenames beside the original file     |
| `sidecar_metadata`  | Normalized NFO and yt-dlp metadata, including read errors |
| `metadata_requests` | Metadata changes requested by earlier steps               |

`probe.audio`, `probe.video`, and `probe.subtitle` select stream types. `probe.real_video` excludes
cover art. Raw ffprobe JSON is available as `probe.raw`. See
[ProbeData](../common/src/common/media/probe.py) for the full fields and helpers.

When a flow resumes after conversion, `probe` describes the converted output and `source_probe` describes
that stage's input. Compare those two when checking an output ratio. Use `original_filename` for matching
sidecars, because intermediate workspace names aren't the original library name.

## Add values for later conditions

A matching plugin can return facets for later steps. For example:

```python
return {"type": "match", "facets": {"unexpected_languages": ["jpn"]}}
```

If the plugin is `inspector.audio_languages`, a condition sees this as
`audio_languages.unexpected_languages`. Use bare names in the return value. The host adds the plugin
suffix. Dotted names and reserved `media.*` names are rejected.

Built-in facts already describe size, codecs, languages, and other media properties. Use
`GET /api/flows/facts` to list available fields and types. Unknown measurements are omitted rather
than guessed. A missing fact doesn't match even a negative comparison.

For example, this condition requires non-HEVC video and English audio:

```json
{
  "all": [
    {"media.video.codec_family": {"ne": "hevc"}},
    {"media.audio.languages": {"contains": "eng"}}
  ]
}
```

Comparisons include `ne`, `gt`, `gte`, `lt`, `lte`, `contains`, and `not_contains`. `all` and `any` combine
conditions. A custom facet needs a producing plugin on every path that reaches its condition. After
a conversion, plugin facets remain available while `media.*` facts are recalculated from the output.

## Return a processing plan

Plans use [FFmpegPlan](../common/src/common/plan/models.py), not a command string. For example, a plan
that copies streams into MKV is:

```python
from common.plan import FFmpegPlan

return {"type": "plan", "plan": FFmpegPlan(container="mkv")}
```

Streams are copied unless the plan requests encoding or another change. The renderer handles stream
mapping, metadata, and the output muxer. `extras` are individual FFmpeg argument tokens, not shell
syntax, and can't override renderer-owned options such as `-f`.

Use `AudioPlanInputs` and `StreamPolicyInputs` when your plugin needs the same selection settings as
the bundled steps. Language values use three-letter codes such as `eng` and `jpn`. Empty language lists
leave selection unrestricted. Audio filtering keeps at least one audio track, and forced subtitles
survive language filtering unless `keep_forced` is disabled.

For audio conversion, supply both codec and bitrate. `audio_when_codecs` narrows conversion to matching
source codecs. An empty list converts all selected audio. Quality and bitrate video modes are separate:
quality mode uses the encoder's quality control, while bitrate mode requires a known source bitrate.
See [Video targets](recipes.md#video-targets) for input examples.

### Continue after processing

Returning a plan pauses the flow while that stage runs. After output verification, traversal resumes
at the step's `plan` edge. It can inspect the measured output, request another stage, or finish.

Flows can't contain loops. Each output port connects to one destination, and conditions need both
`true` and `false` branches. A flow must contain a transcode step and reach a result: `done`, `skip`,
or `veto`. The [HEVC recipe](recipes.md#video-targets) includes a complete import example.

Only a final accepted output replaces library media. Earlier stages stay in the workspace if a later
stage fails. Each stage is verified against its own input, not the file from the beginning of the flow.

## Request actions

Action plugins return typed actions, which can be used directly in a decision:

```python
from common.actions import ExtractSubtitles

return {
    "type": "actions",
    "actions": [ExtractSubtitles(formats=["srt"], languages=["eng"])],
}
```

| Action                | Result                                                                                                                       |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `extract_subtitles`   | Extract selected subtitles and publish sidecars beside the final media                                                       |
| `notify`              | Send selected job outcomes to named notification targets                                                                     |
| `notify_external`     | Notify a configured external application after replacement. Supports Radarr and Sonarr refreshes, with optional unmonitoring |
| `preserve_timestamps` | Preserve the file times captured for processing                                                                              |
| `embed_metadata`      | Apply sidecar metadata during planning                                                                                       |

Required subtitle extraction happens before encoding. If removed tracks would otherwise be lost when
discarding the original, their sidecars must be saved first. A collision leaves the original protected.
Other post-actions run after success, and their failures can produce a warning without undoing the media
replacement. File-writing actions need the completing node's workspace and tools.

Put job-notification steps before the first conversion if you want failure and cancellation notifications.
External-application notifications wait for final replacement regardless of where the action appears on
the successful path. See [Job notifications](recipes.md#job-notifications) and
[Notify external applications](recipes.md#notify-external-applications) for setup.

## Include sidecar metadata

If your transcode plugin supports the [Embed sidecar metadata step](recipes.md#embed-sidecar-metadata),
call `common.plan.metadata_updates.apply_metadata(plan, ctx)` before deciding whether the plan needs
work. The bundled remux and video plugins show this usage. Put metadata in `FFmpegPlan.metadata`, not
raw argument strings.

## Debug a plugin

Start with the **Plugins** page for load errors and the job's decision log for evaluation errors.
Saving an invalid flow reports its graph or input errors without starting a conversion.

Plugin calls have a default 10-second limit. If a call times out, reduce the work done in `decide`.
Reloading a plugin doesn't change plans already stored for existing jobs.
