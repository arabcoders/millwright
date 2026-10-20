# Flow recipes

Use these examples as starting points for your own library. Build them under **Flows**, save the flow,
and [run a dry run](usage.md#preview-a-flow) before assigning it to automatic processing.

In the editor, **Start** connects to the first step. Each step has outputs that lead to another step or
a result. **Done** accepts the converted file, **Skip** leaves it alone, and **Veto** rejects the result.

## Video targets

To convert video to HEVC in an MKV container:

1. Add **Video transcode** and set its target to HEVC.
2. Connect **Start** to that step.
3. Connect its `plan` output to a **Done** result and `skip` to **Skip**.
4. Save the flow and preview it against a few files.

With no encoder selected, HEVC uses `libx265`. AVC and AV1 use `libx264` and `libsvtav1`. Quality mode
lets the encoder choose the bitrate. Try a sample to judge both quality and file size rather than
expecting a fixed percentage saving.

<details>
<summary>Import this example as JSON</summary>

Save this as a JSON file and choose **Flows -> Import flow**:

```json
{
  "_type": "flow",
  "_version": 1,
  "name": "HEVC Archive",
  "definition": {
    "entry": "encode",
    "nodes": [
      {"id": "encode", "type": "plugin", "plugin": "transcode.video", "inputs": {"target": "hevc"}},
      {"id": "done", "type": "terminal", "outcome": "done"},
      {"id": "skip", "type": "terminal", "outcome": "skip"}
    ],
    "edges": [
      {"src": "encode", "port": "plan", "dst": "done"},
      {"src": "encode", "port": "skip", "dst": "skip"}
    ]
  }
}
```

</details>

### Target a source bitrate ratio

If you prefer a bitrate target, select bitrate mode and set a ratio. With the video bitrate source,
`0.5` targets half the reported video bitrate. If that bitrate is unavailable, it uses the whole-file
average, including audio and container data. The ratio doesn't halve the output file size.

The step inputs are:

```json
{"target": "hevc", "rate_control": "bitrate", "ratio": 0.5, "bitrate_source": "video"}
```

Use quality mode when the required source bitrate is unavailable. Quality and bitrate modes are
alternative ways of choosing the encoding target.

### Prefer a hardware encoder

To prefer Intel QSV and use software encoding on nodes without it, leave the fixed encoder unset and
use this priority list:

```json
{"target": "hevc", "encoder_priority": ["hevc_qsv", "libx265"]}
```

The choice is made before processing begins. It doesn't switch encoders halfway through a failed job.
A fixed encoder setting overrides the priority list. See [GPU access](installation.md#hardware-acceleration)
if your preferred encoder isn't available.

## Route by size, codec, or language

Add a condition before the transcode step to limit which files reach it. For example, convert only AVC
files larger than 2 GiB, or skip files without Japanese audio.

The condition editor lets you choose media fields and comparisons. Equivalent JSON conditions are:

| Select files with      | Condition                                      |
| ---------------------- | ---------------------------------------------- |
| Size of at least 2 GiB | `{"media.file.size_bytes":{"gte":2147483648}}` |
| AVC video              | `{"media.video.codec_family":"avc"}`           |
| Video that isn't AV1   | `{"media.video.codec_family":{"ne":"av1"}}`    |
| Japanese audio         | `{"media.audio.languages":{"contains":"jpn"}}` |

Connect the condition's `true` branch to processing and its `false` branch to **Skip**. Conditions only
choose a path. A Japanese-audio condition doesn't remove other audio tracks.

## Remux and convert selected audio

Use **Remux** when you want to change containers without re-encoding video. For example, choose MKV,
AAC audio at `192k`, and DTS as the audio codec to convert. Other audio tracks remain copied.

These are the step inputs, not a complete flow import:

```json
{"container": "mkv", "audio_codec": "aac", "audio_bitrate": "192k", "audio_when_codecs": ["dts"]}
```

Connect `plan` to **Done** and `skip` to **Skip**, as in the video example. Leave the audio-codec filter
empty to convert all selected audio. Use the language and commentary options if you also want to narrow
the track selection.

Audio re-encoding counts as transcoding for the library's original-retention setting, even though the
video is copied.

## Reject an output that shrinks too far

Add **Minimum output ratio** after the conversion step, before **Done**. For example, a minimum size
ratio of `0.4` rejects output smaller than 40% of the input:

```json
{"metric": "size", "min_ratio": 0.4}
```

Connect the inspector's `match` output to **Done**, `veto` to **Veto**, and `skip` to **Skip**.
This checks size, not visual quality. Rejected output doesn't replace your original.

## Embed sidecar metadata

Use **Embed sidecar metadata** to copy titles, descriptions, dates, and genres from Kodi NFO or yt-dlp
JSON files into MKV or MP4 media. Episode NFOs can also provide the show name, season, and episode number.
yt-dlp JSON can provide the artist from its channel or uploader.

For `Example.mkv`, put `Example.nfo` or `Example.info.json` beside it. Add **Embed sidecar metadata**
before the remux or video step, and connect its `next` output to that step. Millwright reads the
sidecars without changing them.

The step offers these settings:

| Setting                 | Default                | Choice                                                                |
| ----------------------- | ---------------------- | --------------------------------------------------------------------- |
| `existing_tags`         | `fill_missing`         | Fill absent fields, or use `overwrite` to replace supplied fields     |
| `source_priority`       | `["nfo", "info_json"]` | The first source with a value wins for each field                     |
| `when_no_media_changes` | `skip`                 | Use `remux` to allow tag changes without an audio or video conversion |

NFO files must describe a Kodi movie or episode. JSON must describe one yt-dlp video, not a playlist or
channel. Missing sidecars are allowed. If tags aren't added, check the job log for sidecar errors and
confirm that the step runs before conversion. Matching tags don't trigger another conversion.

MP4 tag support varies by player. Try a file in your player before applying this to a library.

## Job notifications

Create a target under **Notifications** using an [Apprise URL](https://appriseit.com/services). For
example, name a target `ops`, then add **Notify** to your flow before its first conversion step.
Connect its `next` output to the conversion.

Choose the target and the outcomes you want to receive. For success, failure, and cancellation:

```json
{"targets": ["ops"], "on": ["job.succeeded", "job.failed", "job.cancelled"]}
```

Putting Notify before the conversion means it's already selected if that conversion fails. Disable the
step to stop notifications without deleting it. A notification can occasionally be delivered more than once.

## Notify external applications

Use **Notify external application** to tell another application when Millwright replaces a file.
The supported applications are Radarr and Sonarr, which rescan the published media. This is useful when
a conversion changes the extension or media information the application last saw.

Add the step to the flow's successful path. Select the application, enter its base URL and API key, and
save the flow. Include any URL base, such as `https://example.org/sonarr`, but don't append `/api/v3`.

Millwright sends the refresh only after the converted file has replaced the original. It doesn't send
one for dry runs, skips, or failed conversions. Delivery status and errors appear under the job's
**Notification deliveries**. A notification failure doesn't mark the conversion itself as failed.

Enable **Unmonitor after refresh** only if you want to stop future automatic upgrades. Radarr unmonitors
the movie. Sonarr unmonitors the episodes in that file, not the whole season or series.

If lookup fails, check whether the media manager can recognize the published path and filename. Radarr
can use `{imdb-...}` or `{tmdb-...}` identifiers in the path or filename before trying its filename parser.

Flow and job exports can contain this API key. Keep them private.

For the other direction, where a media manager tells Millwright about new imports, use
[Incoming webhooks](webhooks.md).
