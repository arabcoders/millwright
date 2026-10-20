# Contributing to Millwright

## Before contributing

Open a [GitHub issue](https://github.com/arabcoders/millwright/issues) before starting a contribution.
Describe the problem, who it affects, and what you propose to change. Get maintainer approval before
writing code or opening a pull request. Unsolicited pull requests will be declined.

Millwright is a personal-first project. Bug fixes, useful features, and measured performance improvements
take priority over personal preferences or changes made only to reorganize the code.

## Development

### Requirements

- Python 3.14+ and [uv](https://docs.astral.sh/uv/).
- PostgreSQL 18. The setup below uses Docker Compose to run it.
- FFmpeg and ffprobe. MKVToolNix is optional for subtitle repair and MKV statistics.
- [Bun](https://bun.sh/) for frontend packages and commands.
- A [Node.js version supported by Nuxt](https://nuxt.com/docs/4.x/getting-started/installation#prerequisites)
  for Vue type checking.

### Setup

Create a branch from `dev`:

```bash
git clone https://github.com/arabcoders/millwright.git
cd millwright
git switch dev
git pull origin dev
git switch -c feature/descriptive-name
uv sync
./.githooks/install
```

Start the database and reloading server with a local node from the repository root:

```bash
docker compose -f compose.dev.yml up -d --no-deps --wait postgres
uv run python run.py --role all --dev
```

Development uses AIO by default. The database is temporary and loses its data when the container
stops. Use `--role server` only when you don't need the local node.

In another terminal, start the frontend from `ui/`:

```bash
bun install --frozen-lockfile
NUXT_API_URL=http://127.0.0.1:8000/api bun run dev
```

Open `http://localhost:8082` and create your account. Keep `/api` at the end of `NUXT_API_URL`.

See the [development guide](docs/development.md) for persistent databases, container builds,
local OIDC testing, migrations, and documentation publishing. For deployment and GPU setup, use
the [installation guide](docs/installation.md).

## Checks

Database tests use their own PostgreSQL service:

```bash
docker compose -f compose.dev.yml up -d --no-deps --wait postgres-test
```

Run Python checks from the repository root:

```bash
.githooks/pre-commit --unstaged
```

The hook runs formatting, linting, type checks, and tests. Without an argument it checks staged files.
Use `--all` to check a clean checkout too.

From `ui/`, run:

```bash
bun run format:check
bun run lint:ci
bun run lint:tsc
bun test
bun run build
```

Test behavior that could break and assert its outcome. Reuse existing tests where possible. Update user documentation 
when behavior or setup changes.

## Pull requests

Target `dev`, not `master`, and reference the approved issue. Explain the change, any breaking interfaces
or migration steps, and how you checked it. Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).

AI-assisted tools are allowed. You remain responsible for understanding and reviewing the code, tests,
and documentation you submit. Contributions are licensed under the project's [MIT license](LICENSE).

For short questions, use [Discord](https://discord.gg/G3GpVR8xpb). Millwright is maintained as a solo project, so 
replies may take time.
