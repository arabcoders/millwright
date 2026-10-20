from __future__ import annotations

from common.diagnostics import error_message, error_traceback, redact


def test_url_redaction() -> None:
    exc = RuntimeError("connecting to ws://user:pw@example.test:8000/?token=issued-private-token")
    message = error_message(exc, secrets=("pw", "issued-private-token"))
    assert "user:pw" not in message
    assert "ws://user:[redacted]@example.test:8000/?token=[redacted]" in message


def test_diagnostic_urls_preserved() -> None:
    text = "ws://server:8000/api/hub?retry=1 file:///media/movie.mkv custom://server/path"
    assert redact(text) == text
    assert error_message(RuntimeError(text)) == f"RuntimeError: {text}"


def test_cycles_and_urls() -> None:
    exc = ValueError("wss://user:pw@example.test:bad/path")
    exc.__cause__ = exc
    message = error_message(exc, secrets=("pw",))
    assert "pw" not in message


def test_encoded_secret() -> None:
    text = redact("token=secret%20value", secrets=(value for value in ("secret value",)))
    assert "secret%20value" not in text
    assert "secret value" not in text


def test_traceback_redaction() -> None:
    try:
        raise ValueError("secret")
    except ValueError as exc:
        text = error_traceback(exc, secrets=("secret",))
    assert "secret" not in text
