from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import import_module
from pathlib import PurePath
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from common.diagnostics import error_message, error_traceback, redact
from common.enums import JobStatus
from common.process import drain
from sqlalchemy import select, update

from server.external_notifications import notify_external
from server.models import (
    FinalizationJournal,
    Job,
    JobEvent,
    Library,
    MediaFile,
    NotificationDelivery,
    NotificationTarget,
    utcnow,
)
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apprise import Apprise
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SendNotification = Callable[[str, str, str, float], Awaitable[bool]]
LOG = logging.getLogger(__name__)
_MAX_ATTEMPTS = 3
_BATCH_SIZE = 20
_SEND_TIMEOUT_S = 60.0
_PROVIDER_CALL = ContextVar("notification_provider_call", default=False)
_PROVIDER_SECRETS = ContextVar[tuple[str, ...]]("notification_provider_secrets", default=())
_PROVIDER_DIAGNOSTIC = ContextVar[str | None]("notification_provider_diagnostic", default=None)
_CONFIG_DIAGNOSTIC = ContextVar[str | None]("notification_config_diagnostic", default=None)


async def record_publication_notifications(session: AsyncSession, job: Job, attempt_id: int) -> None:
    journal = await session.scalar(
        select(FinalizationJournal).where(
            FinalizationJournal.attempt_id == attempt_id,
            FinalizationJournal.state.in_(["completed", "discard_completed"]),
        )
    )
    if journal is None:
        return
    existing = await session.scalar(
        select(JobEvent.id).where(JobEvent.attempt_id == attempt_id, JobEvent.type == "external_notification")
    )
    if existing is not None:
        return
    stage = await session.scalar(
        select(JobEvent)
        .where(JobEvent.attempt_id == attempt_id, JobEvent.type == "stage")
        .order_by(JobEvent.id.desc())
        .limit(1)
    )
    if stage is None:
        return
    actions = stage.payload.get("publication_actions", [])
    unique = []
    for value in actions:
        destination = {key: field for key, field in value.items() if key not in {"plugin_id", "config_version"}}
        if value.get("kind") != "notify_external" or destination in unique:
            continue
        unique.append(destination)
        event = JobEvent(
            job_id=job.id,
            attempt_id=attempt_id,
            type="external_notification",
            payload={
                "action": value,
                "path": journal.destination_path,
                "original_filename": PurePath(journal.source_path).name,
            },
        )
        session.add(event)
        await session.flush()
        session.add(
            NotificationDelivery(
                job_id=job.id,
                job_event_id=event.id,
                target_name=f"external:{value['application']}",
                status="pending",
            )
        )


class _ProviderFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...] | None = None) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets is None and not _PROVIDER_CALL.get():
            return True
        secrets = self._secrets or _PROVIDER_SECRETS.get()
        message = redact(record.getMessage(), secrets=secrets)
        diagnostic = message
        if record.exc_info and record.exc_info[1] is not None:
            trace = error_traceback(record.exc_info[1], secrets=secrets)
            record.__dict__["traceback"] = trace
            record.__dict__["error"] = error_message(record.exc_info[1], secrets=secrets)
            diagnostic += "\n" + trace
        record.msg, record.args = message, ()
        if record.levelno >= logging.WARNING:
            _PROVIDER_DIAGNOSTIC.set(diagnostic)
        record.exc_info = None
        record.exc_text = None
        standard_fields = logging.LogRecord("", 0, "", 0, "", (), None).__dict__
        for key, value in list(record.__dict__.items()):
            if key not in standard_fields:
                record.__dict__[key] = redact(str(value), secrets=secrets)
        return True


# Apprise URL parsing uses a singleton logger. A caller-local filter avoids
# changing its level or handlers while another notification is using it.
for _name in ("apprise", "urllib3.connectionpool"):
    logging.getLogger(_name).addFilter(_ProviderFilter())


def _url_secrets(url: str) -> tuple[str, ...]:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return (url,)
    values = [url]
    if parsed.password:
        values.extend((parsed.password, unquote(parsed.password)))
    return tuple(values)


@contextmanager
def _provider_call(url: str) -> Iterator[None]:
    tokens = (
        _PROVIDER_CALL.set(True),
        _PROVIDER_SECRETS.set(_url_secrets(url)),
        _PROVIDER_DIAGNOSTIC.set(None),
    )
    try:
        yield
    finally:
        _PROVIDER_DIAGNOSTIC.reset(tokens[2])
        _PROVIDER_SECRETS.reset(tokens[1])
        _PROVIDER_CALL.reset(tokens[0])


def _provider_reason(default: str) -> str:
    return _PROVIDER_DIAGNOSTIC.get() or default


def notification_client(url: str) -> Apprise | None:
    apprise = import_module("apprise")
    with _provider_call(url):
        client = apprise.Apprise(asset=apprise.AppriseAsset(async_mode=False))
        if not client.add(url) or len(client) != 1:
            reason = _provider_reason("provider rejected configuration")
            _CONFIG_DIAGNOSTIC.set(reason)
            LOG.warning("Invalid notification configuration", extra={"error": reason})
            return None
        logger = logging.getLogger(f"notification-provider-{id(client)}")
        logger.addFilter(_ProviderFilter(_url_secrets(url)))
        logger.parent = logging.getLogger("notification-provider")
        logger.propagate = True
        for plugin in client.servers:
            plugin.logger = logger
            # The service owns retries. Bound provider I/O even when a URL
            # requests unbounded socket waits or additional provider retries.
            plugin.retry = 0
            for name in ("socket_connect_timeout", "socket_read_timeout"):
                timeout = getattr(plugin, name)
                bounded = (
                    min(timeout, _SEND_TIMEOUT_S / 2) if math.isfinite(timeout) and timeout > 0 else _SEND_TIMEOUT_S / 2
                )
                setattr(plugin, name, bounded)
    LOG.debug("Validated notification configuration")
    return client


def _size(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{int(value)} B"


def _message(
    *, job_id: int, kind: str | None, event_type: str, payload: dict, filename: str, library_name: str
) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("invalid event payload")
    if event_type == JobStatus.SUCCEEDED.value:
        body = f"{filename} in {library_name} transcoded successfully (job #{job_id})"
        source, output = payload.get("source_size"), payload.get("output_size")
        if source is not None and output is not None:
            if type(source) is not int or type(output) is not int or source < 0 or output < 0:
                raise ValueError("invalid event sizes")
            change = output - source
            direction = "saved" if change < 0 else "added"
            body += f". {_size(source)} -> {_size(output)} ({_size(abs(change))} {direction})."
        return body, f"Transcode succeeded: {filename}"
    if event_type == JobStatus.FAILED.value:
        reason = " ".join(str(payload.get("error") or "unknown error").split())[:160]
        label = "Health check" if kind == "healthcheck" else "Transcode"
        return f"{filename} in {library_name} failed (job #{job_id}): {reason}", f"{label} failed: {filename}"
    return f"{filename} in {library_name} was cancelled (job #{job_id})", f"Transcode cancelled: {filename}"


def _apprise_send_sync(url: str, body: str, title: str) -> bool:
    client = notification_client(url)
    if client is None:
        raise RuntimeError(_CONFIG_DIAGNOSTIC.get() or "Invalid notification configuration")
    with _provider_call(url):
        sent = bool(client.notify(body=body, title=title))
        reason = _provider_reason("provider returned failure")
    if not sent:
        raise RuntimeError(reason)
    return sent


async def _apprise_send(url: str, body: str, title: str, _timeout_s: float) -> bool:
    return await wait_workspace_io(lambda: _apprise_send_sync(url, body, title))


def _apprise_test_sync(url: str, body: str, title: str) -> tuple[bool, str | None]:
    client = notification_client(url)
    if client is None:
        return False, _CONFIG_DIAGNOSTIC.get() or "invalid notification configuration"
    with _provider_call(url):
        sent = bool(client.notify(body=body, title=title))
        reason = _provider_reason("provider returned failure")
    if not sent:
        LOG.warning("Notification provider rejected test", extra={"error": reason})
    return sent, None if sent else reason


async def _apprise_test(url: str, body: str, title: str, _timeout_s: float) -> tuple[bool, str | None]:
    return await wait_workspace_io(lambda: _apprise_test_sync(url, body, title))


class NotificationService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        *,
        send: SendNotification = _apprise_send,
        interval_s: float = 5.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._send = send
        self._interval = interval_s
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._close_task is not None and not self._close_task.done():
            return
        if self._task is None or self._task.done():
            self._close_task = None
            self._task = asyncio.create_task(self._run(), name="notification-delivery")

    def wake(self) -> None:
        self._wake.set()

    async def aclose(self) -> None:
        if self._close_task is None:
            if self._task is None:
                return
            self._task.cancel()
            self._close_task = asyncio.create_task(self._close(self._task))
        closing = self._close_task
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        closing.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self, task: asyncio.Task) -> None:
        await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.deliver_pending()
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
                self._wake.clear()
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Notification delivery pass failed", extra={"error": error_message(exc)})
                await asyncio.sleep(self._interval)

    async def deliver_pending(self) -> int:
        async with self._sessionmaker() as session:
            rows = list(
                (
                    await session.execute(
                        select(NotificationDelivery)
                        .where(NotificationDelivery.status == "pending")
                        .order_by(NotificationDelivery.id)
                        .limit(_BATCH_SIZE)
                    )
                ).scalars()
            )
            delivery_ids = [row.id for row in rows]
        for delivery_id in delivery_ids:
            await self._deliver(delivery_id)
        return len(delivery_ids)

    async def _send_confirmed(self, url: str, body: str, title: str, external: tuple[int, dict] | None = None) -> bool:
        if external is None:
            operation = self._send(url, body, title, _SEND_TIMEOUT_S)
        else:
            event_id, payload = external

            async def save_progress(progress: dict) -> None:
                # A separate event write commits progress without releasing this
                # delivery's row lock while the remote command is still running.
                async with self._sessionmaker() as session:
                    await session.execute(
                        update(JobEvent)
                        .where(JobEvent.id == event_id)
                        .values(payload={**payload, "progress": progress})
                    )
                    await session.commit()

            operation = notify_external(payload, save_progress)
        sending = asyncio.ensure_future(operation)
        try:
            return await asyncio.shield(sending)
        except asyncio.CancelledError:
            sending.cancel()
            # Only the first cancellation reaches an injected sender. Repeated
            # cancelling the caller must not interrupt the worker's cancellation cleanup.
            while not sending.done():
                try:
                    await asyncio.shield(sending)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not sending.cancelled():
                sending.exception()
            # A late result after cancellation is ambiguous, not confirmation.
            raise

    async def _deliver(self, delivery_id: int) -> None:
        async with self._sessionmaker() as session:
            delivery = await session.scalar(
                select(NotificationDelivery)
                .where(NotificationDelivery.id == delivery_id, NotificationDelivery.status == "pending")
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if delivery is None or delivery.status != "pending":
                return
            job_id, target_name = delivery.job_id, delivery.target_name
            attempts = delivery.attempts + 1
            identity = (
                NotificationDelivery.id == delivery.id,
                NotificationDelivery.job_id == job_id,
                NotificationDelivery.job_event_id == delivery.job_event_id,
                NotificationDelivery.target_name == target_name,
                NotificationDelivery.created_at == delivery.created_at,
                NotificationDelivery.status == "pending",
            )
            claimed = await session.scalar(
                update(NotificationDelivery)
                .where(*identity, NotificationDelivery.attempts == attempts - 1)
                .values(attempts=attempts, updated_at=utcnow())
                .returning(NotificationDelivery.id)
            )
            if claimed is None:
                return
            delivery_update = update(NotificationDelivery).where(*identity, NotificationDelivery.attempts == attempts)
            event = await session.get(JobEvent, delivery.job_event_id)
            external_payload = (
                (event.id, event.payload) if event is not None and event.type == "external_notification" else None
            )
            target = (
                await session.scalar(select(NotificationTarget).where(NotificationTarget.name == delivery.target_name))
                if external_payload is None
                else None
            )
            job = await session.get(Job, delivery.job_id)
            media = await session.get(MediaFile, job.media_file_id) if job is not None else None
            library = await session.get(Library, job.library_id) if job is not None else None
            url = target.url if target is not None else None
            event_type = event.type if event is not None else "finished"
            filename = PurePath(media.path).name if media is not None else "unknown file"
            library_name = library.name if library is not None else "unknown library"
            payload = event.payload if event is not None else {}
            try:
                body, title = (
                    ("", "")
                    if external_payload is not None
                    else _message(
                        job_id=job_id,
                        kind=job.kind if job is not None else None,
                        event_type=event_type,
                        payload=payload,
                        filename=filename,
                        library_name=library_name,
                    )
                )
            except (TypeError, ValueError, OverflowError) as exc:
                reason = f"Cannot build notification: {error_message(exc)}"
                await session.execute(delivery_update.values(status="failed", error=reason, updated_at=utcnow()))
                await session.commit()
                LOG.warning(
                    "Cannot notify %s about job %s for %s",
                    target_name,
                    job_id,
                    filename,
                    extra={"error": reason},
                )
                return
            # Hold only this delivery row through provider I/O. Another writer
            # skips it instead of claiming the same pending attempt mid-send.
            try:
                outcome = await self._send_delivery(url, body, title, target_name, job_id, filename, external_payload)
            except asyncio.CancelledError:
                await drain(asyncio.create_task(session.commit()))
                raise
            if outcome is None:
                await session.commit()
                return
            ok, reason = outcome
            if not ok:
                LOG.warning(
                    "Cannot notify %s about job %s for %s (attempt %s of %s)",
                    target_name,
                    job_id,
                    filename,
                    attempts,
                    _MAX_ATTEMPTS,
                    extra={"error": reason},
                )
            await session.execute(
                delivery_update.values(
                    updated_at=utcnow(),
                    status="sent" if ok else "failed" if attempts >= _MAX_ATTEMPTS else "pending",
                    error=None if ok else reason,
                )
            )
            await session.commit()

    async def _send_delivery(
        self,
        url: str | None,
        body: str,
        title: str,
        target_name: str,
        job_id: int,
        filename: str,
        external: tuple[int, dict] | None = None,
    ) -> tuple[bool, str] | None:
        ok = False
        reason = f"Notification target {target_name!r} no longer exists"
        if url is not None or external is not None:
            sending = asyncio.create_task(self._send_confirmed(url or "", body[:500], title, external))
            try:
                ok = await asyncio.wait_for(sending, timeout=_SEND_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError) as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                if not sending.cancelling():
                    sending.cancel()
                while not sending.done():
                    try:
                        await asyncio.shield(sending)
                    except asyncio.CancelledError:
                        task = asyncio.current_task()
                        if not sending.done() or (task is not None and task.cancelling()):
                            cancelled = True
                    except Exception:
                        break
                if not sending.cancelled():
                    sending.exception()
                if cancelled:
                    raise asyncio.CancelledError from None
                # A provider may have delivered after its caller's deadline.
                # Leave the persisted attempt pending for at-least-once retry.
                LOG.warning(
                    "Notification to %s about job %s for %s timed out after %s seconds",
                    target_name,
                    job_id,
                    filename,
                    _SEND_TIMEOUT_S,
                )
                return None
            except Exception as exc:
                secrets = (external[1]["action"]["api_key"],) if external is not None else _url_secrets(url or "")
                reason = error_message(exc, secrets=secrets)
            else:
                if not ok:
                    reason = "Notification provider returned failure without a diagnostic"
        return ok, reason
