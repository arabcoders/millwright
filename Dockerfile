FROM node:24-slim AS frontend
COPY --from=oven/bun:1.4.2 /usr/local/bin/bun /usr/local/bin/bun
WORKDIR /ui
COPY ui ./
RUN --mount=type=cache,target=/root/.bun/install/cache \
    if [ ! -f exported/index.html ]; then \
      bun install --frozen-lockfile && bun run build; \
    fi

FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY common/pyproject.toml common/pyproject.toml
COPY server/pyproject.toml server/pyproject.toml
COPY node/pyproject.toml node/pyproject.toml
COPY common/src common/src
COPY server/src server/src
COPY node/src node/src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package millwright


FROM python:3.14-slim AS final

RUN sed -i 's/^Components: main$/Components: main contrib non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends util-linux fd-find mkvtoolnix \
    intel-media-va-driver-non-free i965-va-driver libmfx-gen1.2 intel-gpu-tools nvidia-smi libcap2-bin curl \
 && ln -s /usr/bin/fdfind /usr/local/bin/fd \
 && setcap cap_perfmon+ep /usr/bin/intel_gpu_top \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/arabcoders/jellyfin-ffmpeg /usr/bin/ffmpeg /usr/bin/ffmpeg
COPY --from=ghcr.io/arabcoders/jellyfin-ffmpeg /usr/bin/ffprobe /usr/bin/ffprobe

RUN groupadd --gid 1000 millwright \
 && useradd --uid 1000 --gid millwright --create-home millwright \
 && mkdir -p /app /config/millwright /data/state /data/cache /data/workspace \
 && chown -R millwright:millwright /app /config /data

COPY --from=build --chown=millwright:millwright /app/.venv /app/.venv
COPY --from=frontend --chown=millwright:millwright /ui/exported /app/.venv/share/millwright/ui
COPY --chown=millwright:millwright README.md SECURITY.md CONTRIBUTING.md /app/.venv/share/millwright/documentation/
COPY --chown=millwright:millwright docs /app/.venv/share/millwright/documentation/docs
COPY --chown=millwright:millwright plugins /app/.venv/share/millwright/plugins
COPY --chown=millwright:millwright run.py /app/run.py

ENV PATH="/app/.venv/bin:$PATH" \
    IN_CONTAINER=1 \
    UMASK=0002 \
    HOME=/home/millwright \
    XDG_CONFIG_HOME=/config \
    XDG_STATE_HOME=/data/state \
    XDG_CACHE_HOME=/data/cache

USER 1000:1000
RUN test -x "$(command -v ffmpeg)" \
 && test -x "$(command -v ffprobe)" \
 && test -x "$(command -v mkvmerge)" \
 && test -x "$(command -v mkvextract)" \
 && test -x "$(command -v mkvpropedit)" \
 && test -x "$(command -v nice)" \
 && test -x "$(command -v ionice)" \
 && test -x "$(command -v fd)" \
 && test -x "$(command -v intel_gpu_top)" \
 && test -x "$(command -v nvidia-smi)" \
 && test -w /app \
 && test -w /config \
 && test -w /data \
 && test -w "$HOME"
EXPOSE 8000
# MW_ROLE or --role selects server, node, or all (default).
# Single-role arguments go to that role's console script.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
    CMD ["python", "/app/run.py", "--healthcheck"]
ENTRYPOINT ["python", "/app/run.py"]
