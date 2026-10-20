from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from common.dates import Dates
from common.enums import ExecutionResult, JobStatus
from common.media.language import normalize_language
from sqlalchemy import BigInteger, Date, Integer, case, cast, column, func, literal, select, text, true
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH

from server.cache import CacheEntry, JsonCache
from server.event_rows import FILE_TRANSCODE_STATUS
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, ProbeRow, utcnow
from server.queue.store import codec_distribution, jobs_finished_since, processing_backlog, queue_depth

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.sql.elements import ColumnElement

    from server.events import EventBus

LOG = logging.getLogger(__name__)
STATS_INTERVAL_S = 300
STATS_RETENTION = timedelta(hours=24)
_STATUSES = tuple(
    status.value for status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED)
)
_PROCESSING_KINDS = ("transcode", "remux", "unclassified")
_DISTRIBUTIONS = (
    "video_codecs",
    "containers",
    "resolutions",
    "health_status",
    "transcode_status",
    "audio_codecs",
    "subtitle_codecs",
    "audio_languages",
    "subtitle_languages",
)


def _inventory() -> dict:
    return {"files": 0, "bytes": 0, **{key: {} for key in _DISTRIBUTIONS}}


def _scope() -> dict:
    return {
        "inventory": _inventory(),
        "jobs": [],
        "savings": {},
        "acquisition": {},
        "totals": {"files": 0, "saved_bytes": 0, "source_bytes": 0, "avg_reduction_pct": 0.0},
        "by_kind": {
            kind: {"savings": {}, "totals": {"files": 0, "saved_bytes": 0, "source_bytes": 0, "avg_reduction_pct": 0.0}}
            for kind in _PROCESSING_KINDS
        },
    }


def _processing_kind() -> ColumnElement[str]:
    # A stage's finished report records effective codec choices. The final plan
    # and attempt argv alone lose encodes followed by cleanup remux stages.
    stages = (
        select(JobEvent.payload["argv"].label("argv"))
        .where(
            JobEvent.type == "stage",
            JobEvent.job_id == Job.id,
            JobEvent.attempt_id == JobAttempt.id,
            JobEvent.payload["result"].as_string().is_not(None),
        )
        .correlate(Job, JobAttempt)
    )
    records = stages.union_all(
        select(JobAttempt.argv.label("argv"))
        .where(JobAttempt.result.is_not(None), ~stages.exists())
        .correlate(Job, JobAttempt)
    ).subquery()
    argv = records.c.argv
    tokens = (
        func.jsonb_array_elements_text(case((func.jsonb_typeof(argv) == "array", argv), else_=literal([], JSONB)))
        .table_valued("value", with_ordinality="position")
        .render_derived()
    )
    codec = argv[cast(tokens.c.position, Integer)].as_string()
    # renderer.render sets -c copy, then emits scoped overrides only for streams
    # actually encoded. Unused audio_encode targets therefore remain remuxes.
    rank = (
        select(
            func.coalesce(
                func.max(
                    case(
                        (tokens.c.value.op("~")(r"^-c(:[vas](:[0-9]+)?)?$") & (codec != "copy"), 2),
                        ((tokens.c.value == "-c") & (codec == "copy"), 1),
                        else_=0,
                    )
                ),
                0,
            )
        )
        .select_from(tokens)
        .correlate(records)
        .scalar_subquery()
    )
    return (
        select(
            case(
                (func.max(rank) == 2, "transcode"),
                (func.min(rank) == 1, "remux"),
                else_="unclassified",
            )
        )
        .select_from(records)
        .correlate(Job, JobAttempt)
        .scalar_subquery()
    )


def _savings_filters() -> list:
    latest = (
        select(func.max(JobAttempt.id))
        .where(JobAttempt.job_id == Job.id, JobAttempt.result == ExecutionResult.SUCCEEDED.value)
        .correlate(Job)
        .scalar_subquery()
    )
    return [
        Job.status == JobStatus.SUCCEEDED.value,
        Job.kind == "transcode",
        JobAttempt.source_size.is_not(None),
        JobAttempt.output_size.is_not(None),
        JobAttempt.result == ExecutionResult.SUCCEEDED.value,
        JobAttempt.id == latest,
    ]


async def build_snapshot(session: AsyncSession, events: EventBus, now: datetime) -> dict:
    dates = Dates(events.timezone)
    today = dates.local(now).date()
    first_day = today - timedelta(days=364)
    since = dates.midnight(first_day)
    until = dates.midnight(today + timedelta(days=1))
    libraries = (await session.execute(select(Library.id, Library.name).order_by(Library.name, Library.id))).all()
    scopes = {"all": _scope(), **{str(identifier): _scope() for identifier, _ in libraries}}

    def targets(identifier: int) -> tuple[dict, dict]:
        return scopes["all"], scopes.setdefault(str(identifier), _scope())

    rows = (
        await session.execute(
            select(MediaFile.library_id, func.count(), func.sum(MediaFile.size)).group_by(MediaFile.library_id)
        )
    ).all()
    for identifier, count, size in rows:
        for scope in targets(identifier):
            scope["inventory"]["files"] += int(count)
            scope["inventory"]["bytes"] += int(size or 0)

    acquired_day = cast(func.timezone(events.timezone.key, MediaFile.created_at), Date).label("day")
    # Initial dispatch preserves the input probe across stages. Finished attempts
    # can also retain source_size without a probe. Use one earliest retained input.
    input_size = func.coalesce(
        cast(JobAttempt.input_probe["size_bytes"].as_string(), BigInteger), JobAttempt.source_size
    )
    original = (
        select(input_size.label("size"))
        .select_from(Job)
        .join(JobAttempt, JobAttempt.job_id == Job.id)
        .where(
            Job.media_file_id == MediaFile.id,
            Job.library_id == MediaFile.library_id,
            Job.kind == "transcode",
            input_size.is_not(None),
        )
        .order_by(JobAttempt.created_at, JobAttempt.attempt_number, JobAttempt.id)
        .limit(1)
        .correlate(MediaFile)
        .lateral()
    )
    rows = (
        await session.execute(
            select(
                MediaFile.library_id,
                acquired_day,
                func.count(),
                func.sum(func.coalesce(original.c.size, MediaFile.size)),
                func.count().filter(original.c.size.is_(None)),
            )
            .select_from(MediaFile)
            .outerjoin(original, true())
            .where(MediaFile.created_at >= since, MediaFile.created_at < until)
            .group_by(MediaFile.library_id, "day")
        )
    ).all()
    for identifier, date, count, size, current_size_files in rows:
        for scope in targets(identifier):
            bucket = scope["acquisition"].setdefault(str(date), {"files": 0, "bytes": 0, "current_size_files": 0})
            bucket["files"] += int(count)
            bucket["bytes"] += int(size or 0)
            bucket["current_size_files"] += int(current_size_files)

    for key, attribute, filters in (
        ("video_codecs", MediaFile.video_codec, [MediaFile.medium == "video"]),
        ("containers", MediaFile.container, []),
        ("resolutions", MediaFile.resolution_bucket, [MediaFile.medium == "video"]),
        ("health_status", MediaFile.health_status, []),
        ("transcode_status", FILE_TRANSCODE_STATUS, []),
    ):
        value = func.coalesce(func.nullif(func.trim(attribute), ""), "unknown")
        rows = (
            await session.execute(
                select(MediaFile.library_id, value, func.count()).where(*filters).group_by(MediaFile.library_id, value)
            )
        ).all()
        for identifier, name, count in rows:
            for scope in targets(identifier):
                distribution = scope["inventory"][key]
                distribution[str(name)] = distribution.get(str(name), 0) + int(count)

    # Raw probe data remains text. Validate before casting so malformed probes do
    # not abort the inventory snapshot.
    raw = case(
        (func.pg_input_is_valid(ProbeRow.raw_json, "jsonb"), cast(ProbeRow.raw_json, JSONB)),
        else_=literal({}, JSONB),
    )
    # Strict array selection avoids reparsing the probe. Silent mode skips missing streams.
    streams = (
        func.jsonb_path_query(
            raw,
            cast('strict $.streams ? (@.type() == "array")[*]', JSONPATH),
            literal({}, JSONB),
            true(),
        )
        .table_valued(column("value", JSONB))
        .render_derived()
        .lateral()
    )
    kind = streams.c.value["codec_type"].as_string()
    codec = func.coalesce(func.nullif(func.lower(func.trim(streams.c.value["codec_name"].as_string())), ""), "unknown")
    language = streams.c.value["tags"]["language"].as_string()
    title = streams.c.value["tags"]["title"].as_string()
    rows = (
        await session.execute(
            select(MediaFile.library_id, kind, codec, language, title, func.count())
            .select_from(MediaFile)
            .join(ProbeRow, ProbeRow.id == MediaFile.probe_id)
            .join(streams, true())
            .where(kind.in_(("audio", "subtitle")))
            .group_by(MediaFile.library_id, kind, codec, language, title)
        )
    ).all()
    for identifier, stream_kind, name, language, title, count in rows:
        language = normalize_language(language, title=title) if language and language.strip() else "und"
        for scope in targets(identifier):
            for key, value in ((f"{stream_kind}_codecs", name), (f"{stream_kind}_languages", language)):
                distribution = scope["inventory"][key]
                distribution[value] = distribution.get(value, 0) + int(count)

    # PostgreSQL applies the configured IANA timezone, including DST, before grouping.
    day = cast(func.timezone(events.timezone.key, Job.finished_at), Date).label("day")
    duration = func.extract("epoch", Job.finished_at - Job.started_at)
    latest_attempt = (
        select(JobAttempt.id)
        .where(JobAttempt.job_id == Job.id)
        .order_by(JobAttempt.attempt_number.desc())
        .limit(1)
        .correlate(Job)
        .scalar_subquery()
    )
    processing_kind = _processing_kind()
    job_kind = case(
        # Flow skips without attempts are known not to have run, not missing records.
        ((Job.kind == "transcode") & (Job.status == JobStatus.SKIPPED.value) & JobAttempt.id.is_(None), "not_run"),
        (Job.kind == "transcode", processing_kind),
        else_=Job.kind,
    ).label("processing_kind")
    rows = (
        await session.execute(
            select(Job.library_id, day, job_kind, Job.status, func.count(), func.sum(duration), func.count(duration))
            .outerjoin(JobAttempt, JobAttempt.id == latest_attempt)
            .where(Job.finished_at >= since, Job.finished_at < until, Job.status.in_(_STATUSES))
            .group_by(Job.library_id, "day", "processing_kind", Job.status)
        )
    ).all()
    # Collapse the global view here so requests reduce only day/kind/status buckets,
    # regardless of the number of libraries in the catalog.
    global_jobs: dict[tuple[str, str, str], list] = {}
    for identifier, date, job_kind, status, count, seconds, measured in rows:
        row = {
            "date": str(date),
            "kind": job_kind,
            "status": status,
            "count": int(count),
            "duration_s": float(seconds or 0),
            "duration_count": int(measured),
        }
        scopes.setdefault(str(identifier), _scope())["jobs"].append(row)
        total = global_jobs.setdefault((str(date), job_kind, status), [0, 0.0, 0])
        total[0] += row["count"]
        total[1] += row["duration_s"]
        total[2] += row["duration_count"]
    scopes["all"]["jobs"] = [
        {
            "date": date,
            "kind": kind,
            "status": status,
            "count": count,
            "duration_s": seconds,
            "duration_count": measured,
        }
        for (date, kind, status), (count, seconds, measured) in global_jobs.items()
    ]

    saved = JobAttempt.source_size - JobAttempt.output_size
    rows = (
        await session.execute(
            select(Job.library_id, day, processing_kind.label("processing_kind"), func.count(), func.sum(saved))
            .select_from(Job)
            .join(JobAttempt, JobAttempt.job_id == Job.id)
            .where(*_savings_filters(), Job.finished_at >= since, Job.finished_at < until)
            .group_by(Job.library_id, "day", "processing_kind")
        )
    ).all()
    for identifier, date, kind, count, size in rows:
        for scope in targets(identifier):
            for target in (scope, scope["by_kind"][kind]):
                bucket = target["savings"].setdefault(str(date), {"files": 0, "saved_bytes": 0})
                bucket["files"] += int(count)
                bucket["saved_bytes"] += int(size or 0)
    rows = (
        await session.execute(
            select(
                Job.library_id,
                processing_kind.label("processing_kind"),
                func.count(),
                func.sum(saved),
                func.sum(JobAttempt.source_size),
            )
            .select_from(Job)
            .join(JobAttempt, JobAttempt.job_id == Job.id)
            .where(*_savings_filters())
            .group_by(Job.library_id, "processing_kind")
        )
    ).all()
    saved_by_library = {}
    for identifier, kind, count, size, source in rows:
        saved_by_library[str(identifier)] = saved_by_library.get(str(identifier), 0) + int(size or 0)
        for scope in targets(identifier):
            for target in (scope, scope["by_kind"][kind]):
                target["totals"]["files"] += int(count)
                target["totals"]["saved_bytes"] += int(size or 0)
                target["totals"]["source_bytes"] += int(source or 0)
    for scope in scopes.values():
        for target in (scope, *scope["by_kind"].values()):
            total = target["totals"]
            total["avg_reduction_pct"] = (
                round(100.0 * total["saved_bytes"] / total["source_bytes"], 1) if total["source_bytes"] else 0.0
            )
    library_rows = []
    for identifier, name in libraries:
        scope = scopes[str(identifier)]
        total = scope["totals"]
        library_rows.append(
            {
                "id": identifier,
                "name": name,
                "files": scope["inventory"]["files"],
                "bytes": scope["inventory"]["bytes"],
                "successful_transcodes": scope["by_kind"]["transcode"]["totals"]["files"],
                "successful_remuxes": scope["by_kind"]["remux"]["totals"]["files"],
                "successful_unclassified": scope["by_kind"]["unclassified"]["totals"]["files"],
                **{
                    f"{kind}_saved_bytes": scope["by_kind"][kind]["totals"]["saved_bytes"] for kind in _PROCESSING_KINDS
                },
                "source_bytes": total["source_bytes"],
                "saved_bytes": total["saved_bytes"],
                "avg_reduction_pct": total["avg_reduction_pct"],
            }
        )
    summary = {
        "queue_depth": await queue_depth(session),
        "backlog": await processing_backlog(
            session,
            healthcheck_slots=events.healthcheck_slots,
            healthchecks_on_nodes=events.healthchecks_on_nodes,
            probe_slots=events.probe_slots,
            probe_mean_s=events.probe_mean_s,
            mkv_stats_slots=events.mkv_stats_slots,
            mkv_stats_on_nodes=events.mkv_stats_on_nodes,
            mkv_stats_mean_s=events.mkv_stats_mean_s,
            scanner_paused=events.scanner_paused,
            timezone=events.timezone,
            now=now,
        ),
        "jobs": await jobs_finished_since(session, now - timedelta(hours=24)),
        "space_saved_by_library": saved_by_library,
        "codec_distribution": await codec_distribution(session),
    }
    return {"summary": summary, "date": today.isoformat(), "scopes": scopes, "libraries": library_rows}


def savings(snapshot: dict, days: int, library_id: int | None = None) -> dict:
    scope = snapshot["scopes"].get(str(library_id), _scope()) if library_id is not None else snapshot["scopes"]["all"]
    first = datetime.fromisoformat(snapshot["date"]).date() - timedelta(days=days - 1)

    def project(target: dict) -> dict:
        series = []
        for offset in range(days):
            date = (first + timedelta(days=offset)).isoformat()
            series.append({"date": date, **target["savings"].get(date, {"files": 0, "saved_bytes": 0})})
        return {"series": series, "totals": {**target["totals"]}}

    return {
        "days": days,
        **project(scope),
        "by_kind": {kind: project(scope["by_kind"][kind]) for kind in _PROCESSING_KINDS},
    }


def overview(snapshot: dict, days: int, library_id: int | None = None) -> dict:
    scope = snapshot["scopes"].get(str(library_id), _scope()) if library_id is not None else snapshot["scopes"]["all"]
    first_day = datetime.fromisoformat(snapshot["date"]).date() - timedelta(days=days - 1)
    first = first_day.isoformat()
    observed: dict[str, dict[str, int]] = {}
    seconds = 0.0
    measured = 0
    for row in scope["jobs"]:
        if row["date"] < first:
            continue
        counts = observed.setdefault(row["kind"], dict.fromkeys(_STATUSES, 0))
        counts[row["status"]] += row["count"]
        seconds += row["duration_s"]
        measured += row["duration_count"]
    by_kind = dict(sorted(observed.items()))
    counts = {status: sum(values[status] for values in by_kind.values()) for status in _STATUSES}
    acquisition = []
    for offset in range(days):
        date = (first_day + timedelta(days=offset)).isoformat()
        acquisition.append(
            {"date": date, **scope["acquisition"].get(date, {"files": 0, "bytes": 0, "current_size_files": 0})}
        )
    return {
        "days": days,
        "library_id": library_id,
        "inventory": scope["inventory"],
        "jobs": {
            "completed": sum(counts.values()),
            **counts,
            "avg_duration_s": round(seconds / measured, 3) if measured else 0.0,
            "by_kind": by_kind,
        },
        "savings": savings(snapshot, days, library_id),
        "acquisition": {"series": acquisition},
        "libraries": snapshot["libraries"],
    }


class StatsService:
    def __init__(
        self,
        db: async_sessionmaker[AsyncSession],
        cache: JsonCache,
        events: EventBus,
        *,
        address: tuple[str, str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.cache = cache
        self.events = events
        # A restart with a different timezone must not reuse the previous timezone's buckets.
        # Do not reuse snapshots with current-size acquisition totals or missing fallback counts.
        self.address = (address[0], f"{address[1]}:{events.timezone}:media-original-acquisition")
        self.lock = asyncio.Lock()
        self.clock = clock if clock is not None else utcnow

    async def refresh(self) -> None:
        # Reset and restore hold this lock through mutation and invalidation.
        async with self.lock:
            try:
                now = self.clock()
                async with self.db() as session:
                    await session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY").execution_options(
                            read_only=True
                        )
                    )
                    snapshot = await build_snapshot(session, self.events, now)
                await self.cache.write(
                    *self.address,
                    entry=CacheEntry(
                        generated_at=now,
                        expires_at=now + STATS_RETENTION,
                        payload=snapshot,
                    ),
                )
                self.events.publish("stats", snapshot["summary"])
            except Exception:
                LOG.exception("Scheduled stats refresh failed.")

    async def read(self) -> dict | None:
        entry = await self.cache.read(*self.address)
        return entry.payload if entry is not None and isinstance(entry.payload, dict) else None

    async def invalidate(self) -> None:
        # The caller already owns the stats lock and has drained any refresh.
        await self.cache.delete(*self.address)
