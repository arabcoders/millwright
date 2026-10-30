# Development guide

For the standard setup and check commands, see [Contributing](../CONTRIBUTING.md).

## Frontend runtime

Vue type checking requires Node.js because
[`vue-tsc` doesn't work under Bun](https://github.com/oven-sh/bun/issues/4754).
This also affects the type check during production builds.

## Keep development data

In `compose.dev.yml`, the `postgres` and `postgres-test` services use temporary storage. To keep
development data, replace the `postgres` service's `tmpfs` with a named volume at `/var/lib/postgresql`
and declare that volume in the same file. Keep the `127.0.0.1:55432:5432` binding for a server run from source.

Keep test data separate. Set `MW_TEST_DATABASE_URL` to use a different test database. The default is
`postgresql+psycopg://mw_ci:mw_ci@127.0.0.1:55433/mw_ci`. Test helpers create isolated schemas within it.
Never point tests at an instance containing media history you want to keep.

## Run the built interface

From `ui/`, build the frontend:

```bash
bun run build
```

Then start the server and a local node from the repository root:

```bash
uv run python run.py --role all
```

Open `http://localhost:8000` and claim the pending node on **Nodes**. Use `--role server` if you don't
need a local node.

For a separate source node, set `MW_NODE_SERVER_URL` and `MW_NODE_WORKSPACE_DIR`, then run
`uv run python -m node` from `node/`. Claim it on **Nodes**. See
[Add a node](installation.md#add-a-node) for connection and shared-storage setup.

## Use the CLI

Run CLI commands from `server/`:

```bash
uv run millwright libraries list
uv run millwright auth reset-password --username owner
```

## Build the container

`compose.dev.yml` builds `millwright:local` and runs the server and node together in AIO mode.
From the repository root:

```bash
docker compose -f compose.dev.yml up -d --build all-in-one
```

The image includes the frontend and media tools. PostgreSQL uses temporary development storage.
To add media mounts, put them in `compose.dev.override.yml` and pass both files explicitly:

```bash
docker compose -f compose.dev.yml -f compose.dev.override.yml up -d --build all-in-one
```

Root `compose.yml` is for deployment with separate server and node containers. Follow the
[installation guide](installation.md) for that setup.

## Test OIDC locally

Follow [Configure OIDC](authentication.md#configure-oidc), using the frontend address for both the
provider's registered callback and `auth.oidc.redirect_uri`:

```text
http://localhost:8082/api/auth/oidc/callback
```

Set `auth.oidc.post_login_url` to `http://localhost:8082` and `auth.oidc.username` to your local account.
If you change the frontend hostname, update both URLs. A callback sent to the backend won't set the
login cookie on a different frontend host.

## Write tests

Use `tmp_path`, the helpers in `tests/helpers.py`, and fake media tools. Real-FFmpeg tests are opt-in
and marked `slow`. Report browser, real-media, hardware, and shared-storage checks separately from
automated tests.

## Change the database schema

Create a separate migration database and set `MIGRATION_DATABASE_URL` to its connection URL.
From `server/`, bring it to the current schema before editing models:

```bash
uv run millwright db --database-url "$MIGRATION_DATABASE_URL" upgrade
```

After editing the SQLAlchemy models, generate and apply the migration:

```bash
uv run millwright db --database-url "$MIGRATION_DATABASE_URL" revision -m "describe the schema change"
uv run millwright db --database-url "$MIGRATION_DATABASE_URL" upgrade
```

Review the generated migration and test its effect on existing data. Stop an instance's server before
applying schema changes or making direct database writes. Ordinary library and job commands can run
while the server is running.

## Publish documentation

Use relative repository links and standard GitHub Markdown. Published pages also appear in the web
interface. Register new pages in `ui/app/utils/documentation.ts` and
`server/src/server/api/documentation.py`, and link them from the [documentation index](README.md).
