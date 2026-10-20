from __future__ import annotations

import pytest
from common.secrets import resolve_secret
from tests.helpers import make_test_temp_dir


def test_literal_passthrough() -> None:
    assert resolve_secret("plain-secret") == "plain-secret"


def test_file_value_trimmed() -> None:
    secret = make_test_temp_dir("secrets") / "token"
    secret.write_text("  s3cret  \n", encoding="utf-8")
    assert resolve_secret(f"file:{secret}") == "s3cret"


def test_missing_file_named() -> None:
    missing = make_test_temp_dir("secrets") / "nope"
    with pytest.raises(ValueError):
        resolve_secret(f"file:{missing}")


def test_empty_file_named() -> None:
    empty = make_test_temp_dir("secrets") / "empty"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_secret(f"file:{empty}")


def test_relative_anchors_base() -> None:
    root = make_test_temp_dir("secrets-root")
    (root / "secrets").mkdir()
    (root / "secrets" / "token").write_text("s3cret\n", encoding="utf-8")
    assert resolve_secret("file:secrets/token", base_dir=root) == "s3cret"


def test_absolute_ignores_base() -> None:
    root = make_test_temp_dir("secrets-root")
    secret = make_test_temp_dir("secrets-other") / "token"
    secret.write_text("elsewhere\n", encoding="utf-8")
    assert resolve_secret(f"file:{secret}", base_dir=root) == "elsewhere"
