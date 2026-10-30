# Live API updates

Use `/api/events` to receive job progress and file, library, and node updates in another client.
Use the HTTP API to load data and perform actions. See [API access](usage.md#command-line-and-api-access)
for authentication and [the API reference](installation.md#api-reference) for endpoints.

## Connect

1. Send `POST /api/auth/ws-ticket` with your instance API key or browser login.
2. Read `ticket` and `ttl_s` from the response.
3. Open `wss://millwright.example.org/api/events?ticket=TICKET` before the ticket expires.

Use `Authorization: Bearer API_KEY` for a separate application. A signed-in browser client must send
`X-Millwright-Request: 1` on the ticket request and use the same origin as Millwright.

A ticket lasts 30 seconds and works once. Request a new ticket for each connection, including reconnects.
Missing, expired, or reused tickets close the connection with code `1008`. A browser session expiring
or being revoked also closes its connections.

Use HTTPS/WSS across untrusted networks. If a reverse proxy blocks the connection, check that it allows
WebSocket upgrades for `/api/events`.

## Read events

Each message has `type` and `payload`. For example:

```json
{
  "type": "job.progress",
  "payload": {
    "job_id": 12,
    "progress": {"percent": 50},
    "updated_at": "2026-10-02T12:00:00Z"
  }
}
```

Some messages contain a batch. Handle each entry in `payload.events` as an ordinary event:

```json
{
  "type": "events.batch",
  "payload": {
    "events": [
      {"type": "job.progress", "payload": {"job_id": 12, "progress": {"percent": 50}, "updated_at": "2026-10-02T12:00:00Z"}}
    ]
  }
}
```

| Event                               | Use it to                                              |
| ----------------------------------- | ------------------------------------------------------ |
| `job.status`                        | Update a known job using its complete `job` object     |
| `job.progress`                      | Update `progress` and `updated_at` for `job_id`        |
| `job.request_result`, `job.requeue` | Show a background request's result and reason          |
| `job.deletion`, `job.deleted`       | Show a deletion result or remove a job from the list   |
| `job.cancel_requested`              | Show cancellation in progress                          |
| `file.status`                       | Update the file list or show scan totals               |
| `node.status`                       | Update a node's connection state and active job counts |
| `node.registration`                 | Update registration availability and pending nodes     |
| `library.scan`, `library.deletion`  | Show scan and deletion results                         |
| `library.progress`                  | Update scanning and metadata-reading activity          |
| `scanner.state`, `probing.state`    | Show scanning or metadata-reading status               |
| `stats`                             | Update queue and node summaries                        |
| `events.resync`                     | Detect that updates were missed                        |

Individual file updates contain `media_file_id`, `library_id`, and `file`. On `action: "pruned"`, remove
the file from your client's list. Scan totals instead use `action: "scanned"` with `added`, `updated`,
and `pruned` counts.

Node registration updates contain `enabled` and the complete `pending` list. Each pending request
contains `request_id` and `capabilities`. Load the initial state from `GET /api/nodes/registration`.
Claim a connected request with `POST /api/nodes/pending/{request_id}/claim`. A `202` response starts
registration. The node becomes available only after saving its credentials and verifying the workspace.
Use `PATCH /api/settings` with `node_registration_enabled` to allow or stop new requests.

For initial library activity, request `GET /api/libraries/progress`. Active metadata reads include
`media_file_id`, `path`, `tool`, `started_at`, and nullable `percent`. These activity counters reset
when the server restarts.

## Handle missed updates

Events aren't a replayable history. Progress updates can be combined, and a slow client can receive
`events.resync` with `reason: "overflow"` when it falls behind.

Load current data through the HTTP API after reconnecting or receiving `events.resync`. Don't assume
that opening a new connection makes an old list current. In Millwright's web interface, reload the page
to refresh the view.
