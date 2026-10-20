from __future__ import annotations

# CLI output intentionally writes directly to stdout and stderr.
# ruff: noqa: T201, EM102
import argparse
import asyncio
import configparser
import getpass
import json
import math
import os
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from alembic import command
from alembic.util import CommandError
from common.dates import Dates
from common.media.probe import probe_from_raw
from common.media.tools import ProbeError, run_ffprobe
from common.secrets import resolve_secret
from sqlalchemy import func, inspect, make_url, select
from sqlalchemy.exc import SQLAlchemyError

from server.core.config import load_config
from server.core.db import create_engine_and_session, migration_config, prepare_db
from server.core.paths import (
    cache_dir,
    config_file,
    healthcheck_dir,
    state_dir,
    uploaded_plugins_dir,
    workspace_dir,
)
from server.flows.definition import FlowDefinition
from server.flows.sidecars import sidecar_metadata, sidecar_subtitles
from server.serialize import iso_utc

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncConnection


TIMEOUT = 10.0
TAIL = "bytes=-262144"


def _positive_days(value: str) -> int:
    days = int(value)
    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return days


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return seconds


def _log_levels(value: str) -> str:
    levels = [part.strip().lower() for part in value.split(",")]
    if any(level not in {"debug", "info", "warning", "error", "critical"} for level in levels):
        raise argparse.ArgumentTypeError("choose comma-separated levels: debug, info, warning, error, critical")
    return ",".join(levels)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="millwright", description="Manage Millwright through its local HTTP API.")
    parser.add_argument("-u", "--url", default=os.environ.get("MW_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("-k", "--token", default=os.environ.get("MW_API_TOKEN"))
    parser.add_argument("-j", "--json", action="store_true", dest="as_json")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Reset an account password")
    auth_actions = auth.add_subparsers(dest="action", required=True)
    password_reset = auth_actions.add_parser("reset-password", help="Reset a password and end the account's sessions")
    password_reset.add_argument("--username", required=True)
    jobs = commands.add_parser("jobs", help="Inspect and control jobs", description="List, inspect, and control jobs.")
    actions = jobs.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("-s", "--status")
    listing.add_argument("-n", "--limit", type=int)
    listing.add_argument(
        "-b",
        "--sort-by",
        choices=("id", "created_at", "updated_at", "priority", "status", "path"),
        default="created_at",
    )
    listing.add_argument("-d", "--sort-dir", choices=("asc", "desc"), default="desc")
    show = actions.add_parser("show")
    show.add_argument("job_id", type=int)
    watch = actions.add_parser("watch")
    watch.add_argument("job_id", type=int)
    watch.add_argument("-t", "--timeout", type=_positive_seconds)
    log = actions.add_parser("log")
    log.add_argument("job_id", type=int)
    log.add_argument("-a", "--attempt", type=int)
    log.add_argument("-c", "--complete", action="store_true")
    for name in ("cancel", "requeue", "bump"):
        action = actions.add_parser(name)
        action.add_argument("job_id", type=int)
    bulk = actions.add_parser("bulk")
    bulk.add_argument("bulk_action", choices=("cancel", "requeue", "bump"))
    bulk.add_argument("job_id", nargs="+", type=int)
    for name in ("system", "stats"):
        commands.add_parser(name)
    logs = commands.add_parser("logs", help="Read and follow operational logs")
    logs.add_argument("-n", "--limit", type=int, default=100, help="Entries per page, 1 to 500 (default: 100)")
    logs.add_argument("-l", "--levels", type=_log_levels, help="Comma-separated levels. Error includes critical")
    logs.add_argument("-g", "--logger", help="Exact logger name, such as uvicorn.error or server.playback")
    logs.add_argument("-s", "--search", help="Case-insensitive text search across messages, fields, and exceptions")
    cursor = logs.add_mutually_exclusive_group()
    cursor.add_argument("-b", "--before", type=int, help="Read older entries before this log ID")
    cursor.add_argument("-a", "--after", type=int, help="Read newer entries after this log ID")
    logs.add_argument(
        "-d", "--details", action="store_true", help="Include structured fields, source, and stack traces"
    )
    logs.add_argument(
        "-f", "--follow", action="store_true", help="Print recent entries, then poll for new matches every second"
    )
    logs.add_argument("-t", "--timeout", type=_positive_seconds, help="Stop following after this many seconds")
    directories = commands.add_parser("directories")
    directories.add_argument("path")
    files = commands.add_parser("files", help="Search and manage media files")
    file_actions = files.add_subparsers(dest="action", required=True)
    listing = file_actions.add_parser("list")
    listing.add_argument("-q", "--query")
    listing.add_argument("-s", "--transcode-status")
    listing.add_argument("-H", "--health-status")
    listing.add_argument("-l", "--library-id", type=int)
    listing.add_argument("-n", "--limit", type=int)
    listing.add_argument("-o", "--offset", type=int)
    listing.add_argument(
        "-b",
        "--sort-by",
        choices=(
            "id",
            "path",
            "size",
            "duration_s",
            "bit_rate",
            "transcode_status",
            "health_status",
            "created_at",
            "updated_at",
        ),
        default="id",
    )
    listing.add_argument("-d", "--sort-dir", choices=("asc", "desc"), default="desc")
    for name in ("show", "healthcheck", "transcode"):
        command = file_actions.add_parser(name)
        command.add_argument("file_id", type=int)
    bulk = file_actions.add_parser("bulk")
    bulk.add_argument("bulk_action", choices=("healthcheck", "transcode"))
    bulk.add_argument("file_id", nargs="+", type=int)
    api = commands.add_parser("api", help="Call an API route directly")
    api.add_argument("method", choices=("GET", "POST", "PUT", "PATCH", "DELETE"))
    api.add_argument("path")
    body = api.add_mutually_exclusive_group()
    body.add_argument("-d", "--data")
    body.add_argument("-f", "--data-file", type=Path)
    trash = commands.add_parser("trash", help="Manage trash entries")
    trash_actions = trash.add_subparsers(dest="action", required=True)
    restore = trash_actions.add_parser("restore")
    restore.add_argument("entry_id")
    restore.add_argument("-r", "--replace-existing", action="store_true")
    restore.add_argument("-m", "--move-to-trash", action="store_true")
    restore.add_argument(
        "-S",
        "--no-sync-to-disk",
        dest="sync_to_disk",
        action="store_false",
        help="Skip extra disk syncing during restore",
    )
    libraries = commands.add_parser("libraries", help="Manage libraries")
    library_actions = libraries.add_subparsers(dest="action", required=True)
    library_actions.add_parser("list")
    library_actions.add_parser("progress")
    for name in ("show", "scan", "cancel", "resume", "delete"):
        command = library_actions.add_parser(name)
        command.add_argument("library_id", type=int)
    scanner = library_actions.add_parser(
        "scanner", help="Pause or resume new scans, probes, transcodes, and healthchecks"
    )
    scanner.add_argument("scanner_action", choices=("pause", "resume", "status"))
    create = library_actions.add_parser("create")
    create.add_argument("name")
    create.add_argument("root", nargs="+")
    create.add_argument("-c", "--cooldown-s", type=int, default=0)
    create.add_argument("-w", "--watch", action="store_true")
    create.add_argument("-s", "--scan-on-start", action="store_true")
    create.add_argument(
        "-i", "--scan-interval-s", type=int, help="Scan interval in seconds. Must be positive. Omit to disable"
    )
    create.add_argument("-m", "--mkv-stats", action="store_true")
    create.add_argument("-H", "--healthcheck-enabled", action="store_true")
    create.add_argument(
        "--processing-cutoff-at",
        help="Automatic processing cutoff as an ISO datetime. Uses server timezone if no offset",
    )
    create.add_argument(
        "--processing-cutoff-fields", nargs="+", choices=("created_at", "first_seen", "mtime"), default=["created_at"]
    )
    create.add_argument("--processing-cutoff-match", choices=("all", "any"), default="all")
    create.add_argument("-T", "--trash-mode", choices=("none", "transcode", "all"), default="none")
    create.add_argument("-r", "--trash-retention-days", type=_positive_days, default=30)
    update = library_actions.add_parser("update")
    update.add_argument("library_id", type=int)
    update_body = update.add_mutually_exclusive_group(required=True)
    update_body.add_argument("-d", "--data")
    update_body.add_argument("-f", "--data-file", type=Path)
    flows = commands.add_parser("flows", help="Manage flow definitions")
    flow_actions = flows.add_subparsers(dest="action", required=True)
    flow_actions.add_parser("list")
    show_flow = flow_actions.add_parser("show")
    show_flow.add_argument("name")
    flow_actions.add_parser("facts")
    for name in ("save", "rename"):
        command = flow_actions.add_parser(name)
        command.add_argument("name")
        if name == "rename":
            command.add_argument("new_name")
        command.add_argument("file", type=Path)
        command.add_argument("-v", "--expected-version", type=int, required=name == "rename")
    delete = flow_actions.add_parser("delete")
    delete.add_argument("name")
    delete.add_argument("-v", "--expected-version", type=int, required=True)
    dry_run = flow_actions.add_parser(
        "dry-run", description="Preview a saved flow on a local file, or a JSON definition on an indexed file."
    )
    dry_run.add_argument("target", help="Local media path with --flow. Otherwise, an indexed file ID")
    dry_run.add_argument("file", type=Path, nargs="?", help="Flow definition JSON, for indexed-file mode")
    dry_run.add_argument(
        "-f", "--flow", dest="flow_name", help="Saved flow ID or case-insensitive name. Probes the path locally"
    )
    plugins = commands.add_parser("plugins", help="Manage plugins")
    plugin_actions = plugins.add_subparsers(dest="action", required=True)
    plugin_actions.add_parser("list")
    plugin_show = plugin_actions.add_parser("show")
    plugin_show.add_argument("plugin_id")
    for name in ("enable", "disable", "restart", "stop"):
        command = plugin_actions.add_parser(name)
        command.add_argument("plugin_id")
    install = plugin_actions.add_parser("install")
    install.add_argument("archive", type=Path)
    nodes = commands.add_parser("nodes", help="List and remove nodes")
    node_actions = nodes.add_subparsers(dest="action", required=True)
    node_actions.add_parser("list")
    remove = node_actions.add_parser("remove")
    remove.add_argument("node_id", type=int)
    commands.add_parser("info", help="Show resolved local paths and database")
    db = commands.add_parser("db", help="Inspect and maintain the PostgreSQL database")
    db.add_argument("--database-url", help="PostgreSQL URL or file:path. Defaults to MW_DATABASE_URL or config")
    db_actions = db.add_subparsers(dest="action")
    db_actions.add_parser("tables")
    query = db_actions.add_parser("query")
    query.add_argument("sql")
    query.add_argument("-w", "--write", action="store_true", help="Allow this statement to modify the database")
    revision = db_actions.add_parser("revision", help="Generate a schema migration from the current models")
    revision.add_argument("-m", "--message", required=True)
    db_actions.add_parser("upgrade", help="Initialize the database or apply pending migrations")
    return parser


def _normalize_json_flag(argv: list[str]) -> list[str]:
    json_flags = [arg for arg in argv if arg in {"--json", "-j"}]
    return json_flags + [arg for arg in argv if arg not in {"--json", "-j"}]


def _request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        response = client.request(method, path, **kwargs)
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"request failed: {exc}") from exc
    if not 200 <= response.status_code < 300:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError, AttributeError:
            detail = response.text
        raise RuntimeError(f"API error ({response.status_code}): {detail}")
    return response


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("API returned invalid JSON") from exc


def _body(data: str | None, path: Path | None, *, object_only: bool = False) -> Any:
    try:
        raw = data if data is not None else path.read_text() if path is not None else None
        value = json.loads(raw) if raw is not None else None
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"invalid JSON: {exc}") from exc
    if object_only and not isinstance(value, dict):
        raise RuntimeError("JSON body must be an object")
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _server_dates(client: httpx.Client) -> Dates:
    return Dates(_json(_request(client, "GET", "/api/system"))["timezone"])


def _print_log(entry: dict[str, Any], dates: Dates, *, details: bool) -> None:
    message = entry["message"].rstrip().replace("\n", "\n  ")
    print(
        f"{dates.format(entry['timestamp'])} [{entry['logger']}] [{entry['level'].upper():<5}] {message} id={entry['id']}"
    )
    if details:
        _preview_fields(entry["fields"])
        source = entry.get("source")
        if source:
            print(f"  Source: {source['path']}:{source['line']} in {source['function']}")
    exception = entry.get("exception")
    if exception:
        if details:
            print("  Traceback (most recent call last):")
            for frame in exception["stack"]:
                print(f"    File {frame['path']!r}, line {frame['line']}, in {frame['function']}")
        summary = f"{exception['type']}: {exception['message']}".rstrip().replace("\n", "\n  ")
        print(f"  {summary}")


def _run_logs(args: argparse.Namespace, client: httpx.Client) -> int:
    if not 1 <= args.limit <= 500:
        raise RuntimeError("--limit must be between 1 and 500")
    if args.before is not None and args.before < 1:
        raise RuntimeError("--before must be a positive log ID")
    if args.after is not None and args.after < 0:
        raise RuntimeError("--after must be a non-negative log ID")
    if args.follow and args.before is not None:
        raise RuntimeError("--before cannot be combined with --follow. Use --after to resume following")
    if args.timeout is not None and not args.follow:
        raise RuntimeError("--timeout requires --follow")
    params = {
        key: value
        for key, value in (
            ("limit", args.limit),
            ("levels", args.levels),
            ("logger", args.logger),
            ("search", args.search),
            ("before", args.before),
            ("after", args.after),
        )
        if value is not None
    }
    deadline = time.monotonic() + args.timeout if args.timeout is not None else None
    dates = None
    try:
        while True:
            value = _json(_request(client, "GET", "/api/logs", params=params))
            entries = value["entries"]
            if value["truncated"]:
                print("Requested cursor precedes retained log history. Showing available entries.", file=sys.stderr)
            if args.as_json and not args.follow:
                _print_json(value)
            else:
                for entry in entries:
                    if args.as_json:
                        print(json.dumps(entry, separators=(",", ":")))
                    else:
                        if dates is None:
                            dates = _server_dates(client)
                        _print_log(entry, dates, details=args.details)
            if not args.follow:
                if not args.as_json:
                    if not entries:
                        print("No matching log entries.")
                    if value["next_before"] is not None:
                        print(f"Older entries: use --before {value['next_before']}", file=sys.stderr)
                return 0
            sys.stdout.flush()
            if deadline is not None and time.monotonic() >= deadline:
                return 0
            if "after" in params and len(entries) == args.limit:
                # Drain full forward pages before advancing to the unfiltered high-water mark.
                params["after"] = entries[-1]["id"]
                continue
            params["after"] = value["latest_id"]
            time.sleep(1 if deadline is None else min(1, max(0, deadline - time.monotonic())))
            if deadline is not None and time.monotonic() >= deadline:
                return 0
    except KeyboardInterrupt:
        return 0


def _print_jobs(rows: list[dict[str, Any]]) -> None:
    print("ID STATUS KIND ATTEMPTS NODE FILE")
    for row in rows:
        attempt = row.get("latest_attempt") or {}
        file = row.get("file") or {}
        print(
            row.get("id"),
            row.get("status", "-"),
            row.get("kind", "-"),
            row.get("attempts", 0),
            row.get("node_name") or attempt.get("node_name") or "-",
            file.get("path") or "-",
        )


def _size_text(value: float | None) -> str:
    if not value:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def _duration_text(seconds: float | None) -> str:
    if not seconds:
        return "-"
    total = round(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _watch_line(job: dict[str, Any]) -> str:
    progress = job.get("progress") or {}
    fields = [f"job {job.get('id', '-')} status={job.get('status', '-')}"]
    if isinstance(progress.get("percent"), (int, float)):
        fields.append(f"percent={progress['percent']}")
    fields.extend(f"{key}={progress[key]}" for key in ("fps", "speed") if progress.get(key) is not None)
    return " ".join(fields)


def _probe_text(probe: dict[str, Any]) -> str:
    streams = probe.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    dims = f" {video['width']}x{video['height']}" if video and video.get("width") else ""
    video_text = f"{video.get('codec') or 'unknown'}{dims}" if video else "no video"
    audio = sum(1 for stream in streams if stream.get("codec_type") == "audio")
    subs = sum(1 for stream in streams if stream.get("codec_type") == "subtitle")
    duration = probe.get("duration_s")
    rate = f" · {probe['size_bytes'] * 8 / duration / 1000:.0f} kbps" if probe.get("size_bytes") and duration else ""
    return (
        f"{_size_text(probe.get('size_bytes'))} · {_duration_text(duration)}{rate} · "
        f"{video_text} · {audio} audio · {subs} subtitles"
    )


def _resolve_flow(rows: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    if selector.isdecimal():
        match = next((row for row in rows if row["id"] == int(selector)), None)
        if match is not None:
            return match
    exact = next((row for row in rows if row["name"] == selector), None)
    if exact is not None:
        return exact
    matches = [row for row in rows if row["name"].casefold() == selector.casefold()]
    if not matches:
        raise RuntimeError(f"Flow not found: {selector!r}. Use 'millwright flows list' to see names and IDs")
    if len(matches) > 1:
        choices = ", ".join(f"{row['id']} ({row['name']})" for row in matches)
        raise RuntimeError(f"Ambiguous flow name {selector!r}: {choices}. Use a flow ID")
    return matches[0]


def _preview_fields(fields: dict[str, Any], indent: str = "  ") -> None:
    for key, value in fields.items():
        if value is None:
            continue
        label = key if "." in key else key.replace("_", " ")
        if isinstance(value, dict):
            print(f"{indent}{label}:")
            _preview_fields(value, indent + "  ")
        elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            print(f"{indent}{label}:")
            for number, item in enumerate(value, 1):
                print(f"{indent}  {number}.")
                _preview_fields(item, indent + "    ")
        else:
            text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
            print(f"{indent}{label}: {text}")


def _print_dry_run(
    result: dict[str, Any],
    definition: dict[str, Any],
    *,
    source: str,
    flow_label: str,
    probe: dict[str, Any] | None = None,
) -> None:
    print(f"Flow: {flow_label}")
    print(f"Source: {source}")
    if probe is not None:
        print(f"Media: {_probe_text(probe)}")
    status = result["status"]
    print(f"\nResult: {status}")
    if result.get("reason"):
        print(f"  Reason: {result['reason']}")
    nodes = {node["id"]: node for node in definition.get("nodes", [])}
    edges = {(edge["src"], edge["port"]): edge["dst"] for edge in definition.get("edges", [])}
    steps = result.get("steps", [])
    if steps or definition.get("entry"):
        print("\nSteps")
    if definition.get("when"):
        matched = bool(steps)
        print(f"  0. Start (condition) -> {'true' if matched else 'false'}")
        print("     Condition:")
        _preview_fields(definition["when"], "       ")
        print(f"     Branch: true -> {definition['entry']}" if matched else "     Branch: end")
    elif definition.get("entry"):
        print(f"  0. Start -> {definition['entry']}")
    for number, step in enumerate(steps, 1):
        node_id, outcome = step["node"], step["outcome"]
        node = nodes.get(node_id, {})
        kind = node.get("type", "step")
        label = node.get("plugin") or step.get("step") or kind
        display = "convert" if outcome == "plan" else outcome
        print(f"  {number}. {node_id} ({label}) -> {display}")
        if node.get("when"):
            print("     Condition:")
            _preview_fields(node["when"], "       ")
        if node.get("inputs"):
            print("     Settings:")
            _preview_fields(node["inputs"], "       ")
        detail = step.get("reason")
        if detail:
            print(f"     {detail}")
        if step.get("error"):
            print(f"     Error: {step['error']}")
        if step.get("facets"):
            print("     Emitted facts:")
            _preview_fields(step["facets"], "       ")
        if outcome not in {"deferred", "error"}:
            port = "next" if kind == "terminal" or outcome == "actions" else outcome
            destination = edges.get((node_id, port))
            display = "convert" if port == "plan" else port
            print(f"     Branch: {display} -> {destination}" if destination else "     Branch: end")

    continuation = result.get("continuation")
    if continuation is not None:
        print(f"\nResume at: {continuation['node']}")
    if result.get("plan") is not None:
        print("\nConversion settings")
        _preview_fields(result["plan"])
    if result.get("stream_log"):
        print("\nStream decisions")
        for stream in result["stream_log"]:
            identity = " / ".join(str(stream[key]) for key in ("codec", "language", "title") if stream.get(key))
            print(f"  #{stream['stream_index']} {identity}: {stream['outcome']}")
            if stream.get("detail"):
                print(f"    {stream['detail']}")
    for key, title in (("actions", "Stage actions"), ("post_actions", "Final actions")):
        if result.get(key):
            print(f"\n{title}")
            for action in result[key]:
                print(f"  {action['kind']}")
                _preview_fields({key: value for key, value in action.items() if key != "kind"}, "    ")
    if result.get("requires") or result.get("requires_encoders"):
        print("\nWorker requirements")
        if result.get("requires"):
            print(f"  Any worker tag: {', '.join(result['requires'])}")
        if result.get("requires_encoders"):
            print(f"  All encoders: {', '.join(result['requires_encoders'])}")
    if result.get("facets"):
        print("\nAvailable facts")
        _preview_fields(result["facets"])
    if result.get("argv") is not None:
        print("\nFFmpeg arguments")
        print(f"  {shlex.join(result['argv'])}")
    elif result.get("plan") is not None:
        print("\nFFmpeg arguments: pending worker encoder selection")


def _print_job(job: dict[str, Any], dates: Dates) -> None:
    for key in ("id", "status", "kind", "priority", "node_id", "error", "warning"):
        if job.get(key) is not None:
            print(f"{key}: {job[key]}")
    file = job.get("file") or {}
    if file.get("path"):
        print(f"file: {file['path']}")
    print("\nAttempts")
    for attempt in job.get("attempts", []):
        print(
            f"  id={attempt.get('id')} number={attempt.get('attempt_number')} status={attempt.get('status')} "
            f"result={attempt.get('result') or '-'} node={attempt.get('node_name') or attempt.get('node_id') or '-'} "
            f"claimed={dates.format(attempt.get('claimed_at'))} started={dates.format(attempt.get('started_at'))} "
            f"finished={dates.format(attempt.get('finished_at'))} error={attempt.get('error') or '-'}"
        )
    succeeded = next(
        (
            attempt
            for attempt in job.get("attempts", [])
            if attempt.get("result") == "succeeded" and attempt.get("input_probe") and attempt.get("output_probe")
        ),
        None,
    )
    if succeeded is not None:
        before, after = succeeded["input_probe"], succeeded["output_probe"]
        print("\nBefore and after")
        print(f"  before: {_probe_text(before)}")
        print(f"  after:  {_probe_text(after)}")
        if before.get("size_bytes") and after.get("size_bytes"):
            delta = after["size_bytes"] - before["size_bytes"]
            pct = delta / before["size_bytes"] * 100
            print(f"  size:   {_size_text(abs(delta))} {'saved' if delta < 0 else 'added'} ({pct:+.2f}%)")
    print("\nEvents")
    for event in job.get("events", []):
        attempt = f" attempt={event['attempt_id']}" if event.get("attempt_id") is not None else ""
        print(f"  {dates.format(event.get('ts'))} {event.get('type')}{attempt}")


def _print_files(rows: list[dict[str, Any]]) -> None:
    print("ID LIBRARY TRANSCODE HEALTH CONTAINER CODEC RESOLUTION PATH")
    for row in rows:
        resolution = f"{row.get('width') or '-'}x{row.get('height') or '-'}"
        print(
            row.get("id"),
            row.get("library_id"),
            row.get("transcode_status", "-"),
            row.get("health_status", "-"),
            row.get("container", "-"),
            row.get("video_codec", "-"),
            resolution,
            row.get("path", "-"),
        )


def _print_file(file: dict[str, Any]) -> None:
    for key in (
        "id",
        "library_id",
        "path",
        "size",
        "container",
        "video_codec",
        "width",
        "height",
        "duration_s",
        "transcode_status",
        "health_status",
    ):
        if file.get(key) is not None:
            print(f"{key}: {file[key]}")
    print(f"active job: {file.get('active_job_kind') or '-'} ({file.get('active_job_status') or '-'})")
    print("jobs:")
    for job in file.get("jobs", [])[:5]:
        print(f"  {job.get('id')} {job.get('kind')} {job.get('status')} {job.get('error') or ''}".rstrip())
    probe = file.get("probe") or {}
    print(
        f"probe: {probe.get('container', '-')} duration={probe.get('duration_s', '-')} streams={len(probe.get('streams', []))}"
    )


def _print_plugins(rows: list[dict[str, Any]]) -> None:
    print("ID KIND VERSION STATE SOURCE DESCRIPTION")
    for row in rows:
        manifest = row.get("manifest") or {}
        print(
            manifest.get("id", "-"),
            manifest.get("kind", "-"),
            manifest.get("version", "-"),
            row.get("state", "-"),
            row.get("source", "-"),
            manifest.get("description", "-"),
        )


def _print_plugin(plugin: dict[str, Any]) -> None:
    manifest = plugin.get("manifest") or {}
    for key in ("id", "kind", "version", "description"):
        if manifest.get(key) is not None:
            print(f"{key}: {manifest[key]}")
    print(f"state: {plugin.get('state', '-')}")
    print(f"source: {plugin.get('source', '-')}")
    for key in ("requires", "require_plugins"):
        requirements = manifest.get(key)
        if requirements:
            print(f"{key}: {', '.join(str(item) for item in requirements)}")
    for field in plugin.get("inputs") or []:
        print(f"input: {field.get('name', '-')} ({field.get('type', '-')})")


def _print_nodes(rows: list[dict[str, Any]], dates: Dates) -> None:
    print("ID NAME STATE LAST_SEEN TRANSCODES HEALTHCHECKS MKV_STATS WORKSPACE_READY FREE_BYTES")
    for row in rows:
        print(
            row.get("id", "-"),
            row.get("display_name", "-"),
            row.get("state", "-"),
            dates.format(row.get("last_seen_at")),
            f"{row.get('active_transcodes', '-')}/{row.get('max_transcodes', '-')}",
            f"{row.get('active_healthchecks', '-')}/{row.get('max_healthchecks', '-')}",
            f"{row.get('active_mkv_stats', '-')}/{row.get('max_mkv_stats', '-')}",
            row.get("workspace_ready", "-"),
            row.get("workspace_free_bytes", "-"),
        )


def _run_info() -> int:
    state = state_dir()
    config = None
    note = None
    try:
        config = load_config(config_file())
    except ValueError as exc:
        note = f"config: unreadable ({exc})"
    for key, value in (
        ("config_file", config_file()),
        ("state_dir", state),
        ("cache_dir", cache_dir()),
        (
            "database_url",
            make_url(config.database_url).render_as_string(hide_password=True) if config else "unavailable",
        ),
        ("workspace_dir", workspace_dir(config)),
        ("healthcheck_dir", healthcheck_dir()),
        ("trash", state / "trash"),
        ("uploaded_plugins_dir", uploaded_plugins_dir(config)),
    ):
        print(f"{key}: {value}")
    if note:
        print(note)
    return 0


async def _db_tables(conn: AsyncConnection) -> list[tuple[str, int]]:
    from sqlalchemy import MetaData, Table

    names = await conn.run_sync(lambda sync: sorted(inspect(sync).get_table_names()))
    counts = []
    for name in names:
        table = Table(name, MetaData())
        counts.append((name, (await conn.execute(select(func.count()).select_from(table))).scalar_one()))
    return counts


def _run_db(args: argparse.Namespace) -> int:
    try:
        if args.database_url:
            url = resolve_secret(args.database_url)
        elif configured_url := os.environ.get("MW_DATABASE_URL"):
            url = resolve_secret(configured_url, base_dir=config_file().parent)
        else:
            url = load_config(config_file()).database_url
        display = make_url(url).render_as_string(hide_password=True)
        if args.action is None:
            print(display)
            return 0
        return _db_operation(args, url)
    except (SQLAlchemyError, CommandError, configparser.Error, OSError, ValueError) as exc:
        raise RuntimeError(f"database error: {exc}") from exc


def _db_operation(args: argparse.Namespace, url: str) -> int:
    engine, _ = create_engine_and_session(url)
    try:
        if args.action == "revision":
            config = migration_config(engine, config_file=Path(__file__).resolve().parents[2] / "alembic.ini")
            command.revision(config, message=args.message, autogenerate=True)
        elif args.action == "upgrade":
            prepare_db(engine)
            print(f"database upgraded: {engine.url.render_as_string(hide_password=True)}")
        else:

            async def query() -> None:
                async with engine.begin() as conn:
                    if not (args.action == "query" and args.write):
                        await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    if args.action == "tables":
                        counts = await _db_tables(conn)
                    else:
                        result = await conn.exec_driver_sql(args.sql)
                        value = (
                            [dict(row) for row in result.mappings()]
                            if result.returns_rows
                            else {"rows_affected": result.rowcount}
                        )
                if args.action == "tables":
                    if args.as_json:
                        _print_json(dict(counts))
                    else:
                        for name, count in counts:
                            print(f"{name}: {count}")
                else:
                    print(json.dumps(value, indent=2, sort_keys=True, default=str))

            asyncio.run(query())
        return 0
    finally:
        asyncio.run(engine.dispose())


def _execute(args: argparse.Namespace, client: httpx.Client) -> int:
    if args.command == "auth":
        password = getpass.getpass("New password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if not password:
            raise RuntimeError("Password cannot be empty.")
        if password != confirmation:
            raise RuntimeError("Passwords do not match.")
        _request(client, "POST", "/api/auth/reset-password", json={"username": args.username, "password": password})
        print(f"Password reset for {args.username}. All Millwright sessions for this account have ended.")
        return 0
    if args.command == "logs":
        return _run_logs(args, client)
    if args.command == "api":
        if not args.path.startswith("/api/"):
            raise RuntimeError("API path must begin with /api/")
        data = None
        if args.data is not None or args.data_file is not None:
            data = _body(args.data, args.data_file)
        response = (
            _request(client, args.method, args.path, json=data)
            if data is not None
            else _request(client, args.method, args.path)
        )
        if response.status_code != 204:
            try:
                _print_json(response.json())
            except ValueError:
                print(response.text, end="")
        return 0
    if args.command == "trash":
        if args.move_to_trash and not args.replace_existing:
            raise RuntimeError("--move-to-trash requires --replace-existing")
        value = _json(
            _request(
                client,
                "POST",
                f"/api/trash/{quote(args.entry_id, safe='')}/restore",
                json={
                    "replace_existing": args.replace_existing,
                    "move_to_trash": args.move_to_trash,
                    "sync_to_disk": args.sync_to_disk,
                },
            )
        )
        if args.as_json:
            _print_json(value)
        else:
            print(f"restored {value['id']} to {value['restored_path']}")
        return 0
    if args.command == "libraries":
        base = "/api/libraries"
        if args.action == "list":
            value = _json(_request(client, "GET", base))
        elif args.action == "progress":
            value = _json(_request(client, "GET", f"{base}/progress"))
        elif args.action == "scanner":
            if args.scanner_action == "status":
                value = _json(_request(client, "GET", f"{base}/scanner"))
            else:
                value = _json(_request(client, "POST", f"{base}/scanner/{args.scanner_action}"))
        elif args.action == "cancel":
            value = _json(_request(client, "POST", f"{base}/{args.library_id}/scan/cancel"))
        elif args.action == "show":
            value = _json(_request(client, "GET", f"{base}/{args.library_id}"))
        elif args.action == "delete":
            value = _json(_request(client, "DELETE", f"{base}/{args.library_id}"))
        elif args.action == "scan":
            value = _json(_request(client, "POST", f"{base}/{args.library_id}/scan"))
        elif args.action == "resume":
            value = _json(_request(client, "PATCH", f"{base}/{args.library_id}", json={"suspended": False}))
        elif args.action == "create":
            cutoff = args.processing_cutoff_at
            if cutoff is not None:
                dates = _server_dates(client)
                try:
                    cutoff = iso_utc(dates.parse(cutoff))
                except ValueError as exc:
                    raise RuntimeError(f"invalid processing cutoff: {exc}") from exc
            value = _json(
                _request(
                    client,
                    "POST",
                    base,
                    json={
                        "name": args.name,
                        "roots": args.root,
                        "cooldown_s": args.cooldown_s,
                        "schedule": None,
                        "watch": args.watch,
                        "scan_on_start": args.scan_on_start,
                        "scan_interval_s": args.scan_interval_s,
                        "mkv_stats": args.mkv_stats,
                        "healthcheck_enabled": args.healthcheck_enabled,
                        "processing_cutoff": {
                            "at": cutoff,
                            "fields": args.processing_cutoff_fields,
                            "match": args.processing_cutoff_match,
                        }
                        if cutoff is not None
                        else {},
                        "trash_mode": args.trash_mode,
                        "trash_retention_days": args.trash_retention_days,
                    },
                )
            )
        else:
            value = _json(
                _request(
                    client,
                    "PATCH",
                    f"{base}/{args.library_id}",
                    json=_body(args.data, args.data_file, object_only=True),
                )
            )
        if args.as_json:
            _print_json(value)
        elif args.action == "list":
            for row in value:
                policy = f"trash {row.get('trash_mode', 'none')} ({row.get('trash_retention_days', 30)} days)"
                state = " suspended (review settings before resuming)" if row.get("suspended") else ""
                print(f"{row.get('id')} {row.get('name')} ({', '.join(row.get('roots', []))}) {policy}{state}")
        elif args.action == "scanner":
            print(f"Global activity {'paused' if value['paused'] else 'resumed'}")
        elif args.action in {"scan", "cancel"}:
            print(f"library {value['library_id']} scan {value['status'].replace('_', ' ')}")
        elif args.action == "delete":
            print(f"library {value['library_id']} deletion accepted. User files and trash are kept.")
        elif args.action == "progress":
            dates = _server_dates(client)
            print(f"Global activity {'paused' if value['scanner']['paused'] else 'resumed'}")
            for row in value["libraries"]:
                print(
                    f"library {row['library_id']} discovery {row['discovery']['status']} "
                    f"found {row['discovery']['found']} added {row['discovery']['added']} "
                    f"updated {row['discovery']['updated']} "
                    f"reason {row['discovery']['reason']} probing {row['probing']['status']} "
                    f"processed {row['probing']['processed']} remaining {row['probing']['remaining']} "
                    f"activity paused {row['scanner']['paused']} next {dates.format(row['scanner']['next_run_at'])} "
                    f"latest {row['discovery']['latest_result']} at {dates.format(row['discovery']['finished_at'])}"
                )
        else:
            policy = f"trash {value.get('trash_mode', 'none')} ({value.get('trash_retention_days', 30)} days)"
            state = " suspended (review settings before resuming)" if value.get("suspended") else ""
            print(f"{value.get('id')} {value.get('name')} {policy}{state}")
        return 0
    if args.command == "flows":
        base = "/api/flows"
        if args.action == "list":
            value = _json(_request(client, "GET", base))
        elif args.action == "facts":
            value = _json(_request(client, "GET", f"{base}/facts"))
        elif args.action == "show":
            value = _json(_request(client, "GET", f"{base}/{quote(args.name, safe='')}"))
        elif args.action == "save":
            definition = _body(None, args.file, object_only=True)
            value = _json(
                _request(
                    client,
                    "PUT",
                    f"{base}/{quote(args.name, safe='')}",
                    json={"definition": definition, "expected_version": args.expected_version},
                )
            )
        elif args.action == "rename":
            definition = _body(None, args.file, object_only=True)
            value = _json(
                _request(
                    client,
                    "PATCH",
                    f"{base}/{quote(args.name, safe='')}",
                    json={"name": args.new_name, "definition": definition, "expected_version": args.expected_version},
                )
            )
        elif args.action == "delete":
            response = _request(
                client, "DELETE", f"{base}/{quote(args.name, safe='')}?expected_version={args.expected_version}"
            )
            if not args.as_json:
                print(f"deleted {args.name}")
            return 0
        else:
            definition: dict[str, Any]
            preview_probe: dict[str, Any] | None = None
            if args.flow_name is not None:
                if args.file is not None:
                    raise RuntimeError("--flow mode takes one local media path, not a definition file")
                try:
                    path = Path(args.target).expanduser().resolve()
                    regular = path.is_file()
                except (OSError, ValueError) as exc:
                    raise RuntimeError(f"could not read local media path {args.target}: {exc}") from exc
                if not regular:
                    raise RuntimeError(f"local media file not found or not a regular file: {path}")
                saved = _resolve_flow(_json(_request(client, "GET", base)), args.flow_name)
                definition = saved["definition"]
                flow_label = f"{saved['name']} (ID {saved['id']}, v{saved['version']})"
                binary = shutil.which("ffprobe")
                if binary is None:
                    raise RuntimeError("ffprobe is unavailable on this CLI host. Install it or add it to PATH")
                try:
                    raw = asyncio.run(run_ffprobe(binary, path))
                    probe = probe_from_raw(path, path.stat().st_size, raw)
                    sidecars = sidecar_subtitles(str(path))
                except (OSError, ValueError, ProbeError) as exc:
                    raise RuntimeError(f"could not probe local media file {path}: {exc}") from exc
                preview_probe = probe.model_dump(mode="json")
                body: dict[str, Any] = {
                    "flow": definition,
                    "probe": preview_probe,
                    "sidecar_subtitles": sidecars,
                    "sidecar_metadata": sidecar_metadata(
                        str(path), FlowDefinition.model_validate(definition)
                    ).model_dump(mode="json"),
                }
            else:
                if args.file is None:
                    raise RuntimeError("use --flow NAME_OR_ID LOCAL_PATH, or FILE_ID DEFINITION_JSON")
                try:
                    file_id = int(args.target)
                except ValueError as exc:
                    raise RuntimeError("indexed-file mode requires a numeric file ID") from exc
                definition = _body(None, args.file, object_only=True)
                body = {"file_id": file_id, "flow": definition}
                flow_label = str(args.file)
            value = _json(_request(client, "POST", f"{base}/dry-run", json=body))
        if args.as_json:
            _print_json(value)
        elif args.action == "list":
            for row in value:
                print(f"{row.get('id')} {row.get('name')} v{row.get('version')}")
        elif args.action == "facts":
            for row in value:
                print(f"{row.get('key')} ({row.get('type')})")
        elif args.action == "dry-run":
            source = str(path) if args.flow_name is not None else f"indexed file ID {file_id}"
            _print_dry_run(value, definition, source=source, flow_label=flow_label, probe=preview_probe)
        else:
            print(f"{value.get('id')} {value.get('name')} v{value.get('version')}")
        return 0
    paths = {"system": "/api/system", "stats": "/api/stats"}
    if args.command in paths:
        value = _json(_request(client, "GET", paths[args.command]))
        if args.as_json:
            _print_json(value)
        else:
            if args.command == "stats" and (backlog := value.get("backlog")) and backlog.get("estimated_finish_at"):
                value = {
                    **value,
                    "backlog": {
                        **backlog,
                        "estimated_finish_at": _server_dates(client).format(backlog["estimated_finish_at"]),
                    },
                }
            for key in sorted(value):
                print(f"{key}: {value[key]}")
        return 0
    if args.command == "directories":
        value = _json(_request(client, "GET", "/api/directories", params={"path": args.path}))
        if args.as_json:
            _print_json(value)
        else:
            print(value["path"])
            for item in value.get("items", []):
                print(f"{item['name']}\t{item['path']}")
        return 0
    if args.command == "files":
        if args.action == "bulk":
            value = _json(
                _request(client, "POST", "/api/files/bulk", json={"action": args.bulk_action, "file_ids": args.file_id})
            )
            if args.as_json:
                _print_json(value)
            else:
                for item in value["accepted"]:
                    print(f"File {item['file_id']}: {item['kind']} requested as job {item['job_id']}.")
                for item in value["failed"]:
                    print(f"File {item['file_id']} rejected: {item['detail']}")
            return 1 if value["failed"] else 0
        if args.action == "list":
            params = {
                key: value
                for key, value in (
                    ("query", args.query),
                    ("library_id", args.library_id),
                    ("transcode_status", args.transcode_status),
                    ("health_status", args.health_status),
                    ("limit", args.limit),
                    ("offset", args.offset),
                    ("sort_by", args.sort_by),
                    ("sort_dir", args.sort_dir),
                )
                if value is not None
            }
            value = _json(_request(client, "GET", "/api/files", params=params))
            _print_json(value) if args.as_json else _print_files(value["items"])
            return 0
        if args.action == "show":
            value = _json(_request(client, "GET", f"/api/files/{args.file_id}"))
            _print_json(value) if args.as_json else _print_file(value)
            return 0
        value = _json(_request(client, "POST", f"/api/files/{args.file_id}/{args.action}"))
        if args.as_json:
            _print_json(value)
        else:
            print(f"Job {value['job_id']}: {value['kind']} requested.")
        return 0
    if args.command == "plugins":
        base = "/api/plugins"
        if args.action == "list":
            value = _json(_request(client, "GET", base))
        elif args.action == "show":
            value = _json(_request(client, "GET", f"{base}/{quote(args.plugin_id, safe='')}"))
        elif args.action == "install":
            try:
                data = args.archive.read_bytes()
            except OSError as exc:
                raise RuntimeError(f"cannot read archive: {exc}") from exc
            value = _json(_request(client, "POST", base, files={"file": (args.archive.name, data)}))
        else:
            plugin_id = quote(args.plugin_id, safe="")
            if args.action in ("enable", "disable"):
                value = _json(
                    _request(client, "PATCH", f"{base}/{plugin_id}", json={"enabled": args.action == "enable"})
                )
            else:
                value = _json(_request(client, "POST", f"{base}/{plugin_id}/{args.action}"))
        if args.as_json:
            _print_json(value)
        elif args.action == "list":
            _print_plugins(value)
        elif args.action in ("show", "install"):
            if args.action == "install":
                print(f"installed {value.get('manifest', {}).get('id', '-')} ({value.get('state', '-')})")
            else:
                _print_plugin(value)
        else:
            print(f"{value.get('manifest', {}).get('id', args.plugin_id)}: {value.get('state', 'updated')}")
        return 0
    if args.command == "nodes":
        base = "/api/nodes"
        if args.action == "list":
            value = _json(_request(client, "GET", base))
        else:
            response = _request(client, "DELETE", f"{base}/{args.node_id}")
            if args.as_json:
                return 0
            print(f"removed node {args.node_id}")
            return 0
        if args.as_json:
            _print_json(value)
        elif args.action == "list":
            _print_nodes(value, _server_dates(client))
        return 0
    if args.action == "list":
        params = {
            key: value
            for key, value in (
                ("status", args.status),
                ("limit", args.limit),
                ("sort_by", args.sort_by),
                ("sort_dir", args.sort_dir),
            )
            if value is not None
        }
        value = _json(_request(client, "GET", "/api/jobs", params=params))
        _print_json(value) if args.as_json else _print_jobs(value["items"])
        return 0
    if args.action == "show":
        value = _json(_request(client, "GET", f"/api/jobs/{args.job_id}", params={"events": 1}))
        _print_json(value) if args.as_json else _print_job(value, _server_dates(client))
        return 0
    if args.action == "watch":
        started = time.monotonic()
        previous = None
        while True:
            value = _json(_request(client, "GET", f"/api/jobs/{args.job_id}/state"))
            snapshot = (value.get("status"), value.get("progress"))
            if snapshot != previous:
                print(json.dumps(value, separators=(",", ":")) if args.as_json else _watch_line(value))
                previous = snapshot
            if value.get("status") in {"succeeded", "failed", "cancelled", "skipped"}:
                return 0
            if args.timeout is not None and time.monotonic() - started >= args.timeout:
                raise RuntimeError(f"timed out watching job {args.job_id} after {args.timeout} seconds")
            time.sleep(1)
    if args.action == "log":
        attempt = args.attempt
        if attempt is None:
            detail = _json(_request(client, "GET", f"/api/jobs/{args.job_id}", params={"events": 0}))
            attempts = detail.get("attempts", [])
            if not attempts:
                raise RuntimeError("job has no attempts")
            attempt = max(attempts, key=lambda item: item.get("attempt_number", -1))["id"]
        response = _request(
            client,
            "GET",
            f"/api/jobs/{args.job_id}/attempts/{attempt}/log",
            headers={} if args.complete else {"Range": TAIL},
        )
        sys.stdout.buffer.write(response.content)
        return 0
    if args.action == "bulk":
        action = "retry" if args.bulk_action == "requeue" else args.bulk_action
        body = {"action": action, "job_ids": args.job_id}
        value = _json(_request(client, "POST", "/api/jobs/bulk", json=body))
        if args.as_json:
            _print_json(value)
        elif action == "retry":
            print(f"Requeue requests: {len(value['accepted'])} accepted, {len(value['failed'])} rejected.")
            for job_id in value["accepted"]:
                print(f"Job {job_id}: requeue requested.")
            for failure in value["failed"]:
                print(f"Job {failure['id']} rejected: {failure['detail']}")
        else:
            print(f"succeeded: {value.get('succeeded', [])}. failed: {value.get('failed', [])}")
        return 0
    endpoint = {"cancel": "cancel", "requeue": "retry", "bump": "bump"}[args.action]
    body = {} if endpoint == "bump" else None
    value = _json(_request(client, "POST", f"/api/jobs/{args.job_id}/{endpoint}", json=body))
    if args.as_json:
        _print_json(value)
    elif endpoint == "retry":
        print(f"Job {value['id']}: requeue requested.")
    else:
        print(f"job {value.get('id', args.job_id)}: {value.get('status', 'updated')}")
    return 0


def main(argv: list[str] | None = None, client_factory: Callable[..., httpx.Client] = httpx.Client) -> int:
    args = _parser().parse_args(_normalize_json_flag(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "info":
            return _run_info()
        if args.command == "db":
            return _run_db(args)
        token = args.token
        if token is None and httpx.URL(args.url).host in {"127.0.0.1", "localhost", "::1"} and config_file().is_file():
            token = load_config(config_file()).api_key
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with client_factory(base_url=args.url.rstrip("/"), headers=headers, timeout=TIMEOUT) as client:
            return _execute(args, client)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
