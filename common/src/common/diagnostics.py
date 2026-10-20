from __future__ import annotations

import traceback
from typing import TYPE_CHECKING
from urllib.parse import quote, quote_plus

from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterable


def redact(text: str, *, secrets: Iterable[str] = ()) -> str:
    result = text
    values = {value for value in secrets if value}
    # Replace encoded forms too: URL errors often echo the complete request URL.
    values |= {encoded for value in values for encoded in (quote(value, safe=""), quote_plus(value))}
    for value in sorted(values, key=len, reverse=True):
        result = result.replace(value, "[redacted]")
    return result


def _causes(exc: BaseException) -> Iterable[BaseException]:
    visited: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        yield current
        current = current.__cause__ or (current.__context__ if not current.__suppress_context__ else None)


def error_message(exc: BaseException, *, secrets: Iterable[str] = ()) -> str:
    secret_values = tuple(secrets)
    parts: list[str] = []
    for current in _causes(exc):
        if isinstance(current, ValidationError):
            errors = current.errors(include_input=False, include_context=False, include_url=False)
            descriptions = []
            for error in errors:
                location = ".".join(str(part) for part in error["loc"]) or "value"
                descriptions.append(f"{location}: {error['msg']}")
            text = ". ".join(descriptions)
        else:
            text = str(current)
        text = redact(text, secrets=secret_values)
        if not text and isinstance(current, TimeoutError):
            text = "operation timed out"
        piece = f"{type(current).__name__}: {text}" if text else type(current).__name__
        if piece not in parts:
            parts.append(piece)
    return ". Caused by ".join(parts)


def error_traceback(exc: BaseException, *, secrets: Iterable[str] = ()) -> str:
    """Keep diagnostic frame locations without payloads, locals, or source literals."""
    frames = [
        f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
        for current in _causes(exc)
        for frame in traceback.extract_tb(current.__traceback__)
    ]
    secret_values = tuple(secrets)
    return redact("Traceback:\n" + "".join(frames), secrets=secret_values) + error_message(exc, secrets=secret_values)
