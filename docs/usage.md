# Using Millwright

## Add a library

Use paths as seen by the server. If Docker mounts a host directory at `/media`, enter `/media` as the
library root, not the host path.

To index files without processing them, scan with **No flow** and leave automatic healthchecks and
MKV statistics off. Set [original retention](recovery.md#retain-originals) before assigning a flow.

## Preview a flow

Use **Dry run** on a library to preview its assigned flow, or select files to preview any saved flow.
The preview doesn't change media. **Deferred** means the result depends on processing output or an
encoder choice the preview can't determine.

Closing a batch preview doesn't cancel it. Export the CSV before restarting the server if you need the report.
Use the [flow recipes](recipes.md) for examples to try on a few files before processing an entire library.

## Find new files automatically

**Watch** picks up local filesystem changes but can miss writes made by other machines on a network
share. Use recurring scans for those shares, or [webhooks](webhooks.md) for Sonarr and Radarr imports
and renames.

Scans ignore hidden files and folders. Check extension and folder filters under **Configuration -> Settings**
if a file doesn't appear.

### Cooldown and schedules

Use **Cooldown** to give incoming files time to finish copying. Manual transcodes, manual healthchecks,
retries, and **Skip cooldown** bypass that wait, not pauses, schedules, or node limits.
Schedules use the [server timezone](installation.md#timezone).

### Limit automatic processing by date

To process only new additions, finish the initial scan with no flow. Set a cutoff after that scan and
select **Added to Millwright** before assigning the flow. Copy and download tools can preserve old
modification dates. Cutoffs don't apply to manual requests or jobs already requested.

## Pause and resume

**Pause activity** prevents new scans and processing across the instance and survives a restart.
Running jobs finish. Cancel a job or scan to stop it. If both the instance and a library are paused,
resume both before expecting work from that library.

## Healthchecks and MKV statistics

Healthchecks read and decode the file. A failed tool run isn't a confirmed media error. Check the job
log for the cause. If hardware decoding fails or reports errors, the check retries on the CPU. A
successful manual check can also release a conversion waiting for that file.

> [!IMPORTANT]
> Server-side MKV statistics modify the library file in place. Cancelling the job doesn't undo tags
> already written. Node-side statistics work on a copy before replacing the original.

## Follow jobs

Search **Jobs** by `job_id:123` or `file_id:123`. The file ID also works on **Files**.
Job snapshots contain metadata, not copies of your media.

Requeuing keeps previous attempts. Jobs started with a selected flow reuse its original settings.
Bulk transcodes use each file's library flow. Deleting a job removes history, not media or
[unused workspaces](recovery.md#workspace-cleanup).

### Delete a library

Deleting a library cancels its active work and removes its file list. It keeps the media, completed job
history, and retained originals in Trash.

## Playback

If the browser can't play the original directly, Millwright can convert it for playback without changing
the library file. For playback failures, copy playback diagnostics while the problem is happening.

## Statistics

Savings count jobs, so converting one file twice can contribute twice. Deleting job history can change
past totals. File-added charts count files still in the library. Language charts count tracks, not files.

## Command-line and API access

Run the CLI from the directory containing `compose.yml`:

```bash
docker compose exec server millwright libraries list
```

Use `millwright --help` to list commands and `millwright --json` for scripts.

### Connect from another machine

Set `MW_API_URL` to your server address and `MW_API_TOKEN` to the instance API key. The installer
stores it in `config/millwright/secrets/api_key`. The key grants administrator access. Use HTTPS.

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
