# Using Millwright

Millwright can process a file you select or look after a library automatically. A library tells it where
to find media. A flow tells it which files need work and what to change. Nodes do the processing.

A typical setup converts new imports to your preferred codec, keeps the original for a few days, and
notifies your media manager when the new file is ready. You can also use a library just to browse and
check files without converting anything.

## Add a library

Open **Libraries** and create a library. Add the folders as Millwright sees them. For example, a host
folder mounted at `/media` in Docker must be entered as `/media`, not its host path.

For an initial look at your media, select **No flow** and leave **Health check** and **MKV statistics**
off. Choose **Scan now**. Millwright finds the files, reads their media information, and lists them under
**Files**. You can add more root folders to the same library later.

Scanning alone doesn't convert files. Assigning a flow enables processing, so choose your
[original-retention policy](recovery.md#retain-originals) before doing that.

## Show library artwork

Open **Account -> Web UI settings** to choose a theme or enable **Library artwork background**.
Adjust **Background visibility** or choose **Another background** to change the image. If your libraries
have no artwork, the interface uses its default background. These preferences save in your browser.

## Choose what to change

Open **Flows** to create a flow or start with a preset. A flow can convert video, change containers,
select audio and subtitle tracks, or combine several steps.

Conditions let you treat different files differently. For example, you could convert only large AVC
files, leave existing HEVC files alone, or require a particular audio language. You don't need to write
Python to build a flow. The editor provides the available steps and their settings.

The [recipes](recipes.md) cover common setups. The [plugin guide](plugins.md) is for writing new steps
that aren't already provided.

### Preview a flow

Choose **Actions -> Dry run** on a library to see what its flow would do. A dry run doesn't change media.
Review the matched and skipped files before starting a conversion.

Some results are **Deferred** because they depend on a converted file or an encoder choice that isn't
available during the preview. They're not failed conversions.

You can also select files on **Files** and choose **Dry run** to try a saved flow on that selection.
Use **Export CSV** to keep a report. Closing the preview doesn't cancel it. Use its cancel control if
you want to stop it, and export anything you need before restarting the server.

### Try one file

On **Files**, open a file and choose **Transcode**. Select a flow, use **Dry run** to preview it,
then choose **Transcode** to start. Follow progress on **Jobs**.

Keep [originals in Trash](recovery.md#retain-originals) while testing a new flow.

## Find new files automatically

Each library offers several scanning options:

| Option              | When to use it                                  |
| ------------------- | ----------------------------------------------- |
| **Scan now**        | Check the whole library immediately             |
| **Scan on start**   | Check for changes whenever Millwright starts    |
| **Watch**           | Pick up local filesystem changes as they happen |
| **Recurring scans** | Check the whole library at a regular interval   |

Watch can't see every change made through network storage. Use recurring scans if other machines write
to a network share. You can use Watch and recurring scans together.

Sonarr and Radarr can also [notify Millwright](webhooks.md) about new imports and renames. This lets it scan the 
affected paths without waiting for the next full scan.

Hidden files and folders aren't scanned. Change extension and ignored-folder filters under
**Configuration -> Settings**. Use **Actions -> Cancel scan** to stop a scan.

### Cooldown and schedules

Set a **Cooldown** to give new or changing files time to finish copying before automatic processing.
Set it to zero if no delay is needed. **Skip cooldown** removes the wait for selected files. It doesn't
override a pause, a schedule, or the need for a free node.

A weekly **Schedule** limits when library processing can run. This is useful for keeping conversions out of the hours 
when you use the machine for other work. Schedules use the [server timezone](installation.md#timezone).

Manual transcodes, healthchecks, and retries bypass cooldown, but can still wait for other processing limits.

### Limit automatic processing by date

Use the library's **Cutoff** if you want to process new files without converting the existing library.
For example, scan the library with no flow first, then set a cutoff using **Added to Millwright** before
assigning a flow. Files indexed before the cutoff won't be picked for new automatic processing.

You can compare the date a file was added, when a changed source was first seen, or its file modification
date. File modification dates can be preserved by download and copy tools, so they don't always tell you
when a file was added to the library. With several dates selected, choose whether **All selected dates**
or **Any selected date** must meet the cutoff.

Manual requests and jobs already requested aren't stopped by the cutoff. Clear it to remove the date filter.

## Pause and resume

Use **Pause activity** in the header to stop new scans and processing across the instance. Running jobs
finish. Queued jobs wait until you choose **Resume activity**. The pause remains in effect after a restart.

**Pause library** affects only that library. If both the instance and library are paused, resume both.
Node pause and capacity controls are on **Nodes**.

To stop work that's already running, cancel the job or scan rather than pausing activity.

## Browse and check files

Open **Files**, or click a library name to browse just that library. Search by filename, path, or media
information and combine the library, processing, and health filters. Use `file_id:123` to find one file.

File details show video, audio, and subtitle information. When previous job snapshots are available,
you can compare them with the current file. These snapshots are media information, not backup copies.

Use **Refresh metadata** if the displayed information needs rereading. **Health check** decodes the file
to look for media errors. You can run a healthcheck manually or enable automatic checks in a library.

### Healthchecks and MKV statistics

Healthchecks take CPU and disk time because they read and decode the media. A failed tool run isn't the
same as a confirmed media error. Open the job log to see why the check failed. Hardware decoding is
available, with a CPU retry when hardware decoding fails or reports errors.

A successful manual healthcheck can also start pending conversions.

MKV statistics add missing stream statistics tags to MKV files. Enable this when you need those tags,
not as a requirement for ordinary scanning.

Under **Configuration -> Settings**, choose whether healthchecks and MKV statistics run on the server,
nodes, or both. Set each node's job limits under **Nodes -> Edit**.

> [!IMPORTANT]
> Server-side MKV statistics modify the library file in place. Stopping the job doesn't undo tags already
> written. Node-side statistics work on a copy before replacing the original.

## Follow jobs

**Jobs** shows conversions, healthchecks, metadata reads, and MKV statistics. Open a job for progress,
logs, the steps it followed, and recorded input and output details. Search by path, `job_id:123`, or
`file_id:123`.

Pending or staging work can be waiting for preparation. Queued work needs a free compatible worker.
If it doesn't start, check pauses, schedules, cooldown, and node availability.

- **Cancel** stops the job without scheduling another attempt.
- **Requeue** tries the job again and keeps previous attempts for comparison. Jobs started with a
  selected flow reuse its original settings.
- **Delete** removes job history, not library media. Use [workspace cleanup](recovery.md#workspace-cleanup)
  separately to remove unused temporary files.

Bulk actions apply to the selected rows on the current page. A failed row remains selected so you can
inspect it. Bulk transcodes use each file's library flow. Export a job before deleting it if you want to
keep its logs and media snapshots.

### Delete a library

Choose **Actions -> Delete library** to stop tracking its folders. Millwright cancels that library's active
work and removes its file list. It keeps your media files, completed job history, and retained originals
in Trash. Large deletions can take time. A failed deletion stays visible so you can retry it.

## Playback

Choose **Play** on a file to watch it in the browser. You can switch audio and subtitle tracks and resume
from the position saved in that browser. If the browser can't play the original directly, Millwright
converts it for playback without changing the library file.

For playback failures, use the bug-icon control or **Copy playback diagnostics** on the error screen.
Capture the diagnostics while the problem is happening.

## Settings and statistics

Use **Configuration -> Settings** for processing limits, scanning, playback, and history retention. Paths, 
authentication, and timezone are [server configuration](installation.md#configuration-and-storage).

**Statistics** shows job outcomes, space savings, and media inventory. Transcodes and remuxes are counted
separately. Larger output files produce negative savings. Totals count jobs, so converting the same file
twice can contribute twice. Inventory language charts count tracks, not files.

Charts refresh every five minutes. **Refresh** loads the latest available chart data. File-added charts
describe files still in the library, and deleting history can change earlier size totals.

## Notifications and integrations

- Create an Apprise target under **Notifications** and add a [notification step](recipes.md#job-notifications)
  to the flows you want to hear about.
- Use [incoming webhooks](webhooks.md) to receive imports and renames from other applications.
- Use [Notify external applications](recipes.md#notify-external-applications) to tell Radarr or Sonarr
  when a converted file is ready.

## Command-line and API access

You can manage libraries and jobs from a terminal too. With Compose, run commands from the directory
containing `compose.yml`:

```bash
docker compose exec server millwright libraries list
docker compose exec server millwright jobs show 7
docker compose exec server millwright jobs requeue 7
```

Use `millwright --help` or a resource's `--help` for more commands, and `--json` when using the output
in a script.

### Connect from another machine

Set `MW_API_URL` to your server address and `MW_API_TOKEN` to the instance API key from `config.toml`.
The default key file is `secrets/api_key` beside that configuration file. This key grants administrator
access, so keep it private and use HTTPS.

```bash
export MW_API_URL=https://millwright.example.org
export MW_API_TOKEN="$(cat /path/to/api_key)"
docker run --rm -e MW_API_URL -e MW_API_TOKEN --entrypoint millwright \
  ghcr.io/arabcoders/millwright:latest libraries list
```

HTTP clients send the same key as `Authorization: Bearer API_KEY`. To browse endpoint examples and
download the OpenAPI schema, [enable the API reference](installation.md#api-reference).
Use [Live API updates](events.md) to receive progress and status changes in another client.

For failed jobs, startup problems, and logs, see [Troubleshooting](recovery.md#troubleshooting).
