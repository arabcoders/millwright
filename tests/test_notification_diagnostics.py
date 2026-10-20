from __future__ import annotations

import logging

from server.notifications import _ProviderFilter, _url_secrets


def test_provider_filter_sanitizes_exception() -> None:
    marker = "diagnostic secret"
    encoded = "diagnostic%20secret"
    url = f"json://user:{encoded}@example.test/notify?format=json"
    record = logging.LogRecord(
        "provider", logging.WARNING, __file__, 1, "opaque %s %s /notify?format=json", (marker, encoded), None
    )
    try:
        raise RuntimeError(f"request failed /{marker}")
    except RuntimeError:
        record.exc_info = __import__("sys").exc_info()
    record.secret_url = url
    from server.notifications import _PROVIDER_CALL, _PROVIDER_SECRETS

    call = _PROVIDER_CALL.set(True)
    secrets = _PROVIDER_SECRETS.set(_url_secrets(url))
    try:
        assert _ProviderFilter().filter(record)
    finally:
        _PROVIDER_SECRETS.reset(secrets)
        _PROVIDER_CALL.reset(call)
    assert marker not in record.getMessage()
    assert encoded not in record.getMessage()
    assert "/notify?format=json" in record.getMessage()
    assert record.exc_info is None
    assert marker not in record.secret_url
    assert encoded not in record.secret_url
