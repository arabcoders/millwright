# Recovery and backups

Use trash to undo a conversion, workspace cleanup to remove temporary files, and backups to recover an
instance or media you've lost. These serve different purposes. Keeping an original in trash isn't a
replacement for a backup on separate storage.

## Retain originals

Set **trash mode** in each library before processing:

| Mode        | What happens to replaced originals                             |
| ----------- | -------------------------------------------------------------- |
| `all`       | Keep every original, including files changed only by remuxing  |
| `transcode` | Keep originals when video or audio is re-encoded               |
| `none`      | Discard the original after the replacement passes verification |

Start with `all` when trying a flow. Set **trash retention days** long enough to inspect the results.
After that period, Millwright deletes expired originals automatically.

Conversions need space for the input copy, output, and any original you're keeping. If storage is tight,
review trash and unused workspaces rather than deleting working files by hand.

## Restore an original

Open **trash**, find the file, and choose **restore**. The dialog shows where the original will go.
If a file already exists there, you can choose to replace it and move that existing file to trash too.

Leave **Sync restored file to disk (fsync)** enabled unless you accept the risk of losing an unsynced
copy after a crash or power failure.

> [!WARNING]
> **Replace existing destination** without **Move destination file to trash** permanently discards the
> file currently at that path.

A restored file isn't automatically converted again by the restore action. If restore reports a conflict,
inspect the existing file before choosing replacement options or retrying.

Trash remains available after deleting a library. **Empty trash** deletes every entry, including ones
hidden by your current filters. Deleting selected entries is permanent too.

To restore from a terminal, use `millwright trash restore ENTRY_ID`. See
[CLI access](usage.md#command-line-and-api-access) for running commands in your container.

## Workspace cleanup

**Workspaces** shows the temporary files left by processing jobs. Use **Prune selected** for selected
eligible rows, or **Prune unused** to remove unused workspaces across the instance. The latter isn't
limited to the current page or filters.

Millwright skips files that are still in use. If a workspace remains protected after a failure, inspect
the job and any surviving node process before trying to free it. Deleting the directory yourself can
interfere with a tool that hasn't stopped.

Pruning doesn't delete library media, trash, job history, or saved logs. Deleting job history doesn't
clean the workspace, so use the appropriate control for each.

## Configuration backups

Use **System operations -> Export configuration backup** to save settings you can reuse or restore later.
Choose a password of at least eight characters and keep it with your backup records. The downloaded
`.mwbackup` file is encrypted.

It includes library definitions, flows, notification targets, incoming webhooks, node settings, plugins,
and server configuration. It also includes credentials from the exported configuration, so keep the
backup and its password private.

It does **not** include your media, trash files, accounts, file index, or job history. Back up Compose
files, environment settings, external secret files, and each node's configuration and state separately.
Node state includes the issued credentials in `node-credentials.json`.

### Restore configuration

Choose **Import configuration backup**, select the file, and enter its password. Review the preview
and choose which parts to restore before applying it.

Check library paths, flows, node settings, and webhook path replacements afterward. Restored libraries
are paused and uploaded plugins are disabled so you can review them before resuming. Changes to server
startup configuration need a restart.

If a restore fails partway through, inspect the current settings before retrying. Some selected items
may already have been restored.

## Database and filesystem backups

To preserve accounts, indexed files, history, and recovery information, back up PostgreSQL as well as
the application files. With the supplied database service, run this on the Docker host:

```bash
docker compose exec -T postgres pg_dump -U millwright -d millwright -Fc > millwright.dump
```

Keep the dump outside the container. Test restoring it with PostgreSQL's `pg_restore` into a separate
database before relying on it.

For a complete instance backup, stop the server and nodes before taking the dump and copying configuration,
secrets, application state, trash, and media. PostgreSQL can stay running for the dump. Copying its live
data directory isn't a substitute for a database backup.

When recovering, restore the matching database and filesystem copies before starting Millwright.
A configuration export alone can't recover accounts or interrupted file replacements. See
[Storage locations](installation.md#configuration-and-storage) for the directories involved.

## Reset data

**System operations -> Reset data** clears the file index and job history. You can choose which
configuration categories to keep. Media files, trash, accounts, and startup configuration aren't deleted.
Retained libraries are paused afterward.

Reset also clears earlier processing decisions. When you resume and scan a library, its files can become
eligible for conversion again. A configuration export can't bring the cleared index or history back.

## Startup recovery

After an interruption, Millwright checks unfinished replacements and restores before accepting new
processing. The interface can open while this happens, but editing and processing wait until it's done.

If recovery fails, open **Logs** and check the reported path or database error. Confirm that the expected
storage is mounted and accessible. Keep partial files and the existing database while resolving the
problem. Removing them can discard the copies Millwright needs to recover.

Some interrupted restores need manual inspection, particularly when overwriting an existing file or
moving it to trash. If you can't identify which copy to keep, preserve both and include the error when
asking for help.

### Open the interface after failed recovery

To inspect the instance without retrying recovery, add this to the server's `environment` in
`compose.yml`, then run `docker compose up -d server`:

```yaml
MW_DISABLE_JOBS: "true"
```

This skips recovery and background processing and blocks node connections. The interface still
allows changes. It doesn't stop tools already running on other machines.

After fixing the recovery error, remove this setting and run `docker compose up -d server` again.
For an ordinary pause, use [Pause activity](usage.md#pause-and-resume) instead.

## Troubleshooting

For a failed job, start with its failure message and execution log. **Logs** also shows server and
connected-node messages. Search for the file path or filter by severity.

| Problem                               | What to check                                                                      |
| ------------------------------------- | ---------------------------------------------------------------------------------- |
| A job stays queued                    | Instance and library pauses, schedule, cooldown, node availability, and job limits |
| A path is missing or access is denied | Container mounts, the path as seen inside the container, and file permissions      |
| New files don't appear                | Scan now, scanner filters, and recurring scans for network-storage changes         |
| A GPU encoder is unavailable          | Device access, host drivers, and the encoders listed on Nodes                      |
| Startup reports HTTP 503              | Recovery messages in the server log                                                |
| Playback fails                        | Copy playback diagnostics while the failure is happening                           |

Correct the cause before using **Requeue**. For transcodes, automatic retries stop after three attempts.
Cancelled jobs stay cancelled until you request another attempt.

From a terminal, you can search recent errors or follow new ones:

```bash
docker compose exec server millwright logs --levels warning,error --limit 20
docker compose exec server millwright logs --search "movie.mkv" --details
docker compose exec server millwright logs --follow --levels error --timeout 60
```

To report a problem, open a [GitHub issue](https://github.com/arabcoders/millwright/issues) with the
affected file or job, relevant settings, and logs. Remove actual credentials before sharing exports or
logs, but keep the paths and tool errors that explain the failure.
