# Installation

You'll need an x86-64 machine with Docker Compose, curl, and OpenSSL. FFmpeg and the other media
tools are included in the image.

## Install

Run this in the directory where you want to keep Millwright:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/arabcoders/millwright/HEAD/install.sh)
```

Enter your media folder when prompted. The installer asks before starting compose file. Press Enter to skip startup.
Rerunning it keeps your existing configuration and credentials.

Open `http://YOUR_SERVER_IP:8000` and create your account. Use your server's IP address, or
`http://localhost:8000` if your browser is on the same machine.

Open **Nodes** and click **Claim** on the pending node. Then [add a library](usage.md#add-a-library),
using `/media` for the folder you selected during installation.

## Update Millwright

From your installation directory, run:

```bash
docker compose up -d --pull always
```

Update any nodes on other machines too, using the same image version. See
[Backups](recovery.md#database-and-filesystem-backups) before updating.

## Configuration and storage

Change processing limits, scanning, playback, and history retention under **Configuration -> Settings**.
These changes don't need a restart.

Other server settings are in `config/millwright/config.toml` inside your installation directory.
See the [configuration example](../server/src/server/config.example.toml) for available options.
After editing this file, restart the server:

```bash
docker compose restart server
```

Keep these directories when updating or moving your installation:

| Directory      | Contents                                       |
| -------------- | ---------------------------------------------- |
| `config/`      | Server settings and access keys                |
| `server-data/` | Logs, retained originals, and uploaded plugins |
| `node-config/` | Node settings                                  |
| `node-data/`   | The node's saved connection details            |
| `workspace/`   | Files being processed                          |

Accounts, settings, and job history are stored in the PostgreSQL volume `db-data`, not these
directories. Include the database in your [backups](recovery.md#database-and-filesystem-backups).
Don't use `docker compose down --volumes` for updates. It deletes the database volume.

For disk space problems, see [Workspace cleanup](recovery.md#workspace-cleanup).

To move retained originals, let active work and recovery finish, then stop the server and nodes. Copy
the complete trash directory to the new location. Mount it writable in the server container and set
`trash_dir` in `config/millwright/config.toml` to its container path before starting Millwright again.
Changing `trash_dir` doesn't move files. Keep the old copy until you've verified the Trash entries and
tested a restore.

## Add a node

Add a node when you want another machine to run conversions. It needs access to the server and a
shared workspace, not your media folders.

### Share the workspace

Mount a network share on the server and node machines, for example at `/mnt/millwright-workspace`.
Both machines need write access, and the share must support file locking.

In the server's `compose.yml`, replace `./workspace:/workspace` with:

```yaml
- /mnt/millwright-workspace:/workspace
```

Make the same change for any node running on the server machine. If you're moving an existing
workspace, stop the server and nodes and copy the contents of `workspace/` to the share first.

### Connect the new node

Create a directory for the new node and save the following as `compose.yml`. Replace
`YOUR_SERVER_IP` with your server's address and adjust the shared folder's host path if needed:

```yaml
services:
  node:
    image: ghcr.io/arabcoders/millwright:latest
    user: "${UID:-1000}:${GID:-1000}"
    environment:
      MW_ROLE: node
      UMASK: ${UMASK:-0002}
      MW_NODE_SERVER_URL: http://YOUR_SERVER_IP:8000
      MW_NODE_WORKSPACE_DIR: /workspace
      TZ: ${TZ:-UTC}
    volumes:
      - ./node-config:/config
      - ./node-data:/data
      - /mnt/millwright-workspace:/workspace
    restart: unless-stopped
```

Create its storage directories and use your local account's user and group IDs:

```bash
mkdir -p node-config node-data
printf 'UID=%s\nGID=%s\n' "$(id -u)" "$(id -g)" >> .env
```

Start the node:

```bash
docker compose up -d --pull always
```

Open **Nodes** in the web interface and click **Claim**. Use **Edit** to set the node's processing limits.

New node registration is enabled by default. The **Allow new nodes** switch on **Nodes** stops new
registration.

Keep each node's `node-config` and `node-data` directories separate. The node saves its credentials
there and reconnects automatically after a restart. Only the workspace is shared.

You can keep or remove the node on the server machine. For connections across an untrusted network,
use [HTTPS](#reverse-proxies) for the server address.

## Hardware acceleration

Give the node access to its GPU, then check **Nodes** for available encoders. Try a conversion and
inspect the result before applying a hardware flow to a library.

### Intel GPU access

On the machine running the node, find the GPU's group IDs:

```bash
getent group video render
stat -c '%n %g' /dev/dri/card* /dev/dri/renderD*
```

Set `VIDEO_GID` and `RENDER_GID` in that machine's `.env`, then add this to the `node` service in
`compose.yml`:

```yaml
devices:
  - /dev/dri:/dev/dri
group_add:
  - "${VIDEO_GID}"
  - "${RENDER_GID}"
```

Apply the change:

```bash
docker compose up -d node
```

The image includes Intel drivers. Apply the same device settings to the server if it runs GPU
healthchecks. To display Intel GPU utilization, also add `cap_add: [PERFMON]` and allow performance
counter access on the host. This isn't required for QSV encoding.

For other GPUs, configure the host driver and Docker device access before checking the node's encoders.

## Custom configuration

After editing `compose.yml` or `.env`, run `docker compose up -d` to apply the changes.

### Use an existing PostgreSQL database

Remove the `postgres` service and the server's `depends_on` section from `compose.yml`.
Set `MW_DATABASE_URL` under the server's `environment` to your PostgreSQL 18 connection URL:

```yaml
environment:
  MW_DATABASE_URL: postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE
```

Changing `POSTGRES_PASSWORD` in `.env` doesn't change an existing database password. If you change
the database password, update the server's connection settings to match.

### Timezone

Add your timezone to `.env`, for example:

```dotenv
TZ=Asia/Kuwait
```

Millwright uses this for schedules and displayed dates. UTC is the default. If you've set `tz` in
`config/millwright/config.toml`, change it there instead.

### Reverse proxies

To use HTTPS or a domain name, point your reverse proxy at the server on port 8000.

- Preserve the public `Host` header.
- Send `X-Forwarded-Proto: https` when the proxy handles HTTPS.
- Allow WebSocket upgrades for `/api/hub` and `/api/events`.
- Set `FORWARDED_ALLOW_IPS` in the server's Compose environment to the proxy's IP address or network.

For OIDC login, follow the callback instructions in [Authentication](authentication.md#configure-oidc).

### API reference

To enable API documentation, add this at the top level of `config/millwright/config.toml` and restart
the server:

```toml
enable_docs = true
```

Open `/api/docs` for Swagger, `/api/redoc` for ReDoc, or `/api/openapi.json` for the schema.
Set `enable_docs = false` and restart to turn them off.

### File access errors

If Millwright reports that it can't read or write a folder, check the path in the error message.
On the host, inspect the folder's user and group IDs:

```bash
stat -c '%u:%g' /path/to/directory
```

Set `UID` and `GID` in `.env` to the account that should access your media. That account also needs
access to the installation's storage directories. Changing these settings doesn't change existing
file permissions. Don't change your media's owner just to match the image's `1000:1000` default.

For recovery errors or an instance that won't start, see [Startup recovery](recovery.md#startup-recovery).
