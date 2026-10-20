from __future__ import annotations

import logging

from common.logging import setup_logging as setup_common_logging

from server.playback.responses import PlaybackStreamClosed


class _WebSocketAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn writes WebSocket access records through its error logger.
        if record.levelno != logging.INFO:
            return True
        message = str(record.msg)
        return not (
            message.startswith('%s - "WebSocket %s"')
            or message in {"connection open", "connection closed", "connection rejected (%d %s)"}
        )


class _PlaybackStreamFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info is not None and isinstance(record.exc_info[1], PlaybackStreamClosed):
            # Uvicorn must still catch this exception and close the truncated transport.
            record.msg = "Playback response aborted: %s"
            record.args = (str(record.exc_info[1]),)
            record.levelno = logging.INFO
            record.levelname = "INFO"
            record.exc_info = None
            record.exc_text = None
            return record.levelno >= logging.getLogger(record.name).getEffectiveLevel()
        return True


def configure_access_logging(*, enabled: bool) -> None:
    logger = logging.getLogger("uvicorn.error")
    for log_filter in logger.filters[:]:
        if isinstance(log_filter, _WebSocketAccessFilter):
            logger.removeFilter(log_filter)
    if not enabled:
        logger.addFilter(_WebSocketAccessFilter())


def setup_logging(*, timezone: str, dev: bool = False, level: int = logging.INFO) -> None:
    uvicorn = logging.getLogger("uvicorn.error")
    if not any(isinstance(log_filter, _PlaybackStreamFilter) for log_filter in uvicorn.filters):
        uvicorn.addFilter(_PlaybackStreamFilter())
    setup_common_logging(timezone=timezone, dev=dev, level=level)
