from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from server.scheduler.schedule import active, parse_schedule

T = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)  # Thursday

GRID = {"type": "grid", "cells": [[False] * 24 for _ in range(7)]}
GRID["cells"][3][12] = True


def test_grid_selection() -> None:
    schedule = parse_schedule(GRID)
    assert active(schedule, T) is True
    assert active(schedule, T.replace(hour=13)) is False
    assert active(schedule, T.replace(day=2)) is False  # Friday
    assert active(None, T) is True


def test_grid_shape_rejected() -> None:
    six_rows = {"type": "grid", "cells": [[False] * 24] * 6}
    wide_row = {"type": "grid", "cells": [[False] * 25] * 7}
    short_row = {"type": "grid", "cells": [[False] * 23] * 7}
    for bad in (six_rows, wide_row, short_row):
        with pytest.raises(ValidationError):
            parse_schedule(bad)


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_schedule({"type": "mystery"})


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        active(parse_schedule(GRID), T.replace(tzinfo=None))


def test_grid_timezone() -> None:
    grid = {"type": "grid", "cells": [[False] * 24 for _ in range(7)]}
    grid["cells"][3][19] = True
    schedule = parse_schedule(grid)
    assert active(schedule, datetime(2026, 1, 2, 0, tzinfo=UTC), ZoneInfo("America/New_York")) is True
