from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from server.models import Library
from tests.helpers import database_engine


def test_update_advances_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    current = datetime(2026, 1, 1, tzinfo=UTC)
    later = datetime(2026, 1, 2, tzinfo=UTC)
    monkeypatch.setattr("server.models.datetime", SimpleNamespace(now=lambda _: current))
    with database_engine("model-timestamps") as engine:
        with Session(engine) as session:
            library = Library(name="one")
            session.add(library)
            session.commit()
            assert library.created_at == current and library.updated_at == current
            session.commit()
            assert library.created_at == current and library.updated_at == current

            current = later
            library.name = "two"
            session.commit()
            assert library.created_at == datetime(2026, 1, 1, tzinfo=UTC)
            assert library.updated_at == later

            current = datetime(2026, 1, 3, tzinfo=UTC)
            session.execute(update(Library).where(Library.id == library.id).values(name="three"))
            session.commit()
            refreshed = session.scalar(select(Library).where(Library.id == library.id))
            assert refreshed is not None
            assert refreshed.created_at == datetime(2026, 1, 1, tzinfo=UTC)
            assert refreshed.updated_at == current
