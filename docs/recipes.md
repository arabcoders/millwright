# Flow recipes

Run a [dry run](usage.md#preview-a-flow) before assigning a new flow to a library. Unless marked as a
full import, JSON snippets below show step inputs or conditions, not whole flows.

In the editor, **Start** connects to the first step. Each step has outputs that lead to another step or
a result. **Done** accepts the converted file, **Skip** leaves it alone, and **Veto** rejects the result.

## Video targets

To convert video to HEVC in an MKV container:

Connect **Start** to **Video transcode** with the target set to HEVC. Connect `plan` to **Done** and
`skip` to **Skip**.

Leave the encoder unset to use `libx265` for HEVC, `libx264` for AVC, or `libsvtav1` for AV1. Use quality
mode to choose quality without a fixed bitrate. Try a sample to judge the quality and file size.

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

Select bitrate mode and set a ratio. A ratio of `0.5` targets half the source bitrate, not half the file
size.

The step inputs are:

```json
{"target": "hevc", "rate_control": "bitrate", "ratio": 0.5, "bitrate_source": "video"}
```

Use quality mode if the source bitrate is unavailable.

### Prefer a hardware encoder

To prefer Intel QSV and use software encoding on nodes without it, leave the fixed encoder unset and
use this priority list:

```json
{"target": "hevc", "encoder_priority": ["hevc_qsv", "libx265"]}
```

The first available encoder is used. A fixed encoder overrides this list. Failed encodes aren't retried
with another encoder. See [GPU access](installation.md#hardware-acceleration) if your preferred encoder
isn't available.

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

This **Remux** step changes the container to MKV and converts DTS tracks to AAC. Video and other
audio tracks are copied:

```json
{"container": "mkv", "audio_codec": "aac", "audio_bitrate": "192k", "audio_when_codecs": ["dts"]}
```

Leave the audio-codec filter empty to convert all selected audio.

The [`transcode` trash mode](recovery.md#retain-originals) keeps the original when audio is re-encoded.

## Reject an output that shrinks too far

Add **Minimum output ratio** after the conversion step, before **Done**. For example, a minimum size
ratio of `0.4` rejects output smaller than 40% of the input:

```json
{"metric": "size", "min_ratio": 0.4}
```

Connect the inspector's `match` output to **Done**, `veto` to **Veto**, and `skip` to **Skip**.
This checks size, not visual quality. Rejected output doesn't replace your original.

## Embed sidecar metadata

Use **Embed sidecar metadata** to copy metadata from Kodi NFO or yt-dlp JSON files into MKV or MP4 files.

For `Example.mkv`, put `Example.nfo` or `Example.info.json` beside it. Add **Embed sidecar metadata**
before the remux or video step, and connect its `next` output to that step.

NFO files must describe a Kodi movie or episode. JSON must describe one yt-dlp video, not a playlist
or channel. To update tags when no conversion is needed, choose `remux` for `when_no_media_changes`.
MP4 tag support varies by player.

## Job notifications

Create a target under **Notifications** using an [Apprise URL](https://appriseit.com/services). For
example, name a target `ops`, then add **Notify** to your flow before its first conversion step.
Connect its `next` output to the conversion.

For success, failure, and cancellation:

```json
{"targets": ["ops"], "on": ["job.succeeded", "job.failed", "job.cancelled"]}
```

Notifications can arrive more than once.

## Notify external applications

Use **Notify external application** to tell another application when Millwright replaces a file.
Supported applications are Radarr, Sonarr, Plex, Jellyfin, and Emby.

Add a step to the flow's successful path for each application. Use its base URL, such as
`https://example.org/sonarr`, without an API endpoint.

Each mapping pairs a **Millwright path** with an **Application path**. For example:

| Millwright path     | Application path |
| ------------------- | ---------------- |
| `/mnt/movies`       | `/media/Movies`  |
| `/mnt/series`       | `/media/TV`      |

For Radarr and Sonarr, enable **Unmonitor after refresh** to stop automatic upgrades for the converted
movie or episodes.

Check the job's **Notification deliveries** for status and errors.

Flow and job exports can contain the API key or token. Keep them private.

To receive imports from applications, use [Incoming webhooks](webhooks.md).
