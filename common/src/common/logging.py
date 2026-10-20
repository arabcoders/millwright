from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

import structlog

from common.dates import Dates

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping

    from structlog.typing import Processor


THIRD_PARTY_LEVELS = (
    ("httpx", logging.WARNING),
    ("urllib3.connectionpool", logging.WARNING),
    ("apprise", logging.WARNING),
    ("httpcore", logging.INFO),
    ("psycopg", logging.INFO),
    ("asyncio", logging.INFO),
)


def _drop_color_message(_logger: object, _name: str, event_dict: MutableMapping[str, Any]) -> Mapping[str, Any]:
    # Uvicorn's console-only color_message carries ANSI escapes into structured output.
    event_dict.pop("color_message", None)
    return event_dict


_LEVEL_COLOR = {"debug": "\x1b[36m", "info": "\x1b[32m", "warning": "\x1b[33m", "error": "\x1b[31m"}
_RESET = "\x1b[0m"


def _console_renderer(dates: Dates, *, colors: bool) -> Processor:
    def render(_logger: object, _name: str, event_dict: MutableMapping[str, Any]) -> str:
        timestamp = dates.format(str(event_dict.pop("timestamp")), clock_only=True)
        level = str(event_dict.pop("level", "info"))
        name = str(event_dict.pop("logger", ""))
        event = str(event_dict.pop("event", ""))
        extras = "".join(f" {key}={value!r}" for key, value in sorted(event_dict.items()) if not key.startswith("_"))
        label = f"[{level.upper():<5}]"
        if colors:
            label = f"{_LEVEL_COLOR.get(level, '')}{label}{_RESET}"
        return f"{timestamp} {label} [{name}] {event}{extras}"

    return render


def setup_logging(*, timezone: str | Dates, dev: bool = False, level: int = logging.INFO) -> None:
    # Stdlib and structlog calls share formatting. Interactive stderr uses a local clock.
    # Piped output uses JSON with UTC timestamps, independent of the configured timezone.
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    renderer = (
        _console_renderer(Dates(timezone) if isinstance(timezone, str) else timezone, colors=sys.stderr.isatty())
        if dev or sys.stderr.isatty()
        else structlog.processors.JSONRenderer()
    )
    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                _drop_color_message,
                renderer,
            ],
            foreign_pre_chain=[structlog.stdlib.ExtraAdder(), *shared_processors],
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for name, dependency_level in THIRD_PARTY_LEVELS:
        logging.getLogger(name).setLevel(dependency_level)
