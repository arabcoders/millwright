#!/usr/bin/env bash
set -euo pipefail

for command in docker curl openssl; do
  if ! command -v "$command" >/dev/null; then
    printf 'Required command not found: %s\n' "$command" >&2
    exit 1
  fi
done
docker compose version >/dev/null
docker info >/dev/null

directory=${1:-.}
umask 077
mkdir -p -- "$directory"
cd -- "$directory"

if [[ ! -f compose.yml ]]; then
  curl -fsSL https://raw.githubusercontent.com/arabcoders/millwright/dev/compose.yml -o compose.yml.download
  mv -- compose.yml.download compose.yml
fi

if [[ ! -f .env ]]; then
  IFS= read -r -p 'Media folder: ' media_dir
  if [[ ! -d "$media_dir" ]]; then
    printf 'Media folder does not exist: %s\n' "$media_dir" >&2
    exit 1
  fi
  media_dir=$(cd -- "$media_dir" && pwd -P)
  media_value=${media_dir//\'/\\\'}
  database_password=$(openssl rand -hex 32)
  cat > .env <<EOF
MEDIA_DIR='$media_value'
POSTGRES_PASSWORD=$database_password
UID=$(id -u)
GID=$(id -g)
EOF
fi

mkdir -p config/millwright/secrets server-data node-config node-data workspace
if [[ ! -f config/millwright/secrets/api_key ]]; then
  secret=$(openssl rand -hex 32)
  printf '%s\n' "$secret" > config/millwright/secrets/api_key
fi

printf '\nInstallation directory: %s\n' "$PWD"
if IFS= read -r -p 'Start Millwright containers now? [y/N] ' answer && [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
  docker compose -f compose.yml up -d --pull always --wait
  printf '\nMillwright is ready on port 8000.\n'
  printf 'Open http://<server-ip>:8000\n'
  printf 'Create your account, claim the pending node on Nodes, then add /media as a library.\n'
else
  printf '\nContainers were not started.\n'
  printf 'To start them, run this from the installation directory:\n'
  printf 'docker compose -f compose.yml up -d --pull always --wait\n'
fi
