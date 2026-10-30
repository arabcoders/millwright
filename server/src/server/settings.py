from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from common.enums import ExecutionLocation
from common.workspace import DEFAULT_MIN_FREE_GB
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from server.kv import KeyValueStore
from server.models import KeyValue

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession


SETTINGS_KEY = "operational_settings"


class ScannerConfig(BaseModel):
    """Controls discovery without removing previously indexed files."""

    model_config = ConfigDict(extra="forbid")

    containers: list[str] = ["mkv", "mp4", "avi", "ts", "m4v", "mov", "wmv", "flv", "webm", "mpg", "mpeg"]
    ignore_dirs: list[str] = []


class PlaybackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_duration_s: float = Field(default=6.0, gt=0, allow_inf_nan=False)
    max_concurrent_segments: int = Field(default=2, ge=1)
    grant_ttl_s: int = Field(default=3600, ge=60)
    max_grants: int = Field(default=64, ge=1)


class OperationalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_healthchecks: int = Field(default=1, ge=1)
    node_registration_enabled: bool = True
    max_probes: int = Field(default=4, ge=1)
    max_mkv_stats: int = Field(default=1, ge=1)
    healthcheck_hwaccel: bool = True
    healthcheck_location: ExecutionLocation = ExecutionLocation.LOCAL
    mkv_stats_location: ExecutionLocation = ExecutionLocation.LOCAL
    min_free_gb: float = Field(default=DEFAULT_MIN_FREE_GB, ge=0, allow_inf_nan=False)
    job_retention_days: int = Field(default=30, ge=0)
    session_ttl_days: int = Field(default=7, ge=1)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    playback: PlaybackConfig = Field(default_factory=PlaybackConfig)


async def read_settings(session: AsyncSession) -> OperationalSettings:
    row = await session.scalar(select(KeyValue).where(KeyValue.key == SETTINGS_KEY))
    return OperationalSettings.model_validate({} if row is None else row.value)


class SettingsService:
    def __init__(self, store: KeyValueStore, *, apply: Callable[[OperationalSettings], None]) -> None:
        self.store = store
        self.current = OperationalSettings()
        self.apply = apply
        self.lock = asyncio.Lock()

    async def load(self) -> OperationalSettings:
        async with self.lock:
            self.current = OperationalSettings.model_validate(await self.store.get(SETTINGS_KEY, default={}))
            self.apply(self.current)
            return self.current

    async def update(self, changes: dict[str, Any]) -> OperationalSettings:
        async with self.lock:
            data = self.current.model_dump(mode="json")
            for key, value in changes.items():
                if key in {"scanner", "playback"} and isinstance(value, dict):
                    data[key] = {**data[key], **value}
                else:
                    data[key] = value
            settings = OperationalSettings.model_validate(data)
            await self.store.put(SETTINGS_KEY, settings.model_dump(mode="json"))
            self.current = settings
            self.apply(settings)
            return settings
