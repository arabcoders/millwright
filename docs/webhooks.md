# Incoming webhooks

Connect Sonarr, Radarr, or another application so Millwright learns about new imports and renames
without waiting for a full library scan. Each notification scans the affected files and folders.
Your library's flow, cooldown, and processing settings still decide what happens afterward.

If you want Millwright to notify a media manager after a conversion, use
[Notify external applications](recipes.md#notify-external-applications) instead.

## Connect an application

1. Open **Configuration -> Incoming webhooks -> New webhook**.
2. Choose Sonarr, Radarr, or Generic, give it a name, and save it.
3. Copy the receiver URL. Keep the `?api_key=...` part.
4. In Sonarr or Radarr, add a **Webhook** connection using **POST** and paste that URL. Leave username
   and password empty.
5. Enable import, upgrade, rename, and file/folder deletion notifications, then use **Test**.

A successful test checks the connection without scanning. Import and rename notifications scan the
reported paths. Deletion notifications remove missing files from Millwright's file list after checking
the disk. They don't delete media.

Keep the receiver URL private because it contains the connection key. Delete the webhook in Millwright
to revoke that key.

## Map sending paths to server paths

If Sonarr sees a folder as `/tv` but Millwright sees it as `/media/tv`, add a path replacement:

| Source | Destination |
| ------ | ----------- |
| `/tv`  | `/media/tv` |

A notification for `/tv/Show/episode.mkv` then scans `/media/tv/Show/episode.mkv`.
Don't add replacements when both applications already use the same paths.

The destination must be inside a Millwright library. Replacements apply to whole folder names, and
the longest matching source takes priority.

## Generic requests

Choose Generic for an application or script that can send a file or folder path as JSON. Send a POST
request to the copied receiver URL with `Content-Type: application/json`:

```json
{"path": "/media/tv/Show/episode.mkv"}
```

For several paths:

```json
{"path": ["/media/tv/Show", "/media/movies/Film.mkv"]}
```

Use absolute paths as seen by the sender. Include a deleted path when you want Millwright to check
whether to remove it from its file list. Generic requests don't need an event name.

## Troubleshooting

- If the connection test is unauthorized, copy the current receiver URL again. The instance API key
  and your browser login aren't the webhook key.
- If a path is rejected, check its replacement and make sure the destination belongs to a library.
- If activity or the library is paused, resume it before sending the notification again.
- If the server stopped during a scan, resend the notification or use **Scan now**.

Sonarr and Radarr's grab, health, and application-update notifications are ignored. A successful
notification doesn't mean a conversion started. Check **Files** and **Jobs** for what the library did.

## API reference

Applications integrating directly can manage registrations at `/api/webhooks` and send notifications
to `/api/webhooks/receive`. Management needs an administrator login or instance API key. Receipt uses
the registration's key, passed in the URL, as a Bearer token, or as an HTTP Basic password.

| Response                     | Meaning                                                   |
| ---------------------------- | --------------------------------------------------------- |
| `202`                        | Scan accepted                                             |
| `200` with `status: test`    | Connection test passed                                    |
| `200` with `status: ignored` | Native event doesn't require a scan                       |
| `401`                        | Receiver key is missing, invalid, or revoked              |
| `422`                        | Invalid request or path                                   |
| `409`                        | Activity is paused or reset is in progress                |
| `503`                        | Startup recovery is incomplete or scanning is unavailable |

[Enable the API reference](installation.md#api-reference) for registration fields and request schemas.
