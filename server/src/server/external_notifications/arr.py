from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Literal

import httpx
from common.process import drain
from pydantic import BaseModel, Field, TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from common.actions import NotifyExternal

_IDENTIFIER = re.compile(r"\{(imdb|tmdb)-([^{}]+)\}", re.IGNORECASE)


class _Movie(BaseModel):
    id: int = Field(gt=0)
    imdb_id: str = Field(default="", alias="imdbId")
    tmdb_id: int = Field(default=0, alias="tmdbId")


class _Series(BaseModel):
    id: int = Field(gt=0)


class _Episode(BaseModel):
    id: int = Field(gt=0)
    series_id: int = Field(gt=0, alias="seriesId")


class _Parse(BaseModel):
    movie: _Movie | None = None
    series: _Series | None = None
    episodes: list[_Episode] = []


class _Command(BaseModel):
    id: int = Field(gt=0)
    status: Literal["queued", "started", "completed", "failed", "aborted", "cancelled", "orphaned"]
    message: str | None = None


class _Progress(BaseModel):
    command_id: int = Field(gt=0)
    ids: list[int] = Field(min_length=1)


async def notify_arr(
    action: NotifyExternal, payload: dict, save_progress: Callable[[dict], Awaitable[None]] | None = None
) -> bool:
    path, filename = payload["path"], payload["original_filename"]
    async with httpx.AsyncClient(
        base_url=str(action.url).rstrip("/") + "/api/v3/",
        headers={"X-Api-Key": action.api_key},
        timeout=15,
    ) as client:
        saved = _Progress.model_validate(payload["progress"]) if "progress" in payload else None
        if saved is not None:
            ids = saved.ids
            response = await client.get(f"command/{saved.command_id}")
            response.raise_for_status()
            progress = _Command.model_validate(response.json())
        elif action.application == "radarr":
            identifiers = {kind.lower(): value for kind, value in _IDENTIFIER.findall(path + "/" + filename)}
            if identifiers:
                params = {"tmdbId": identifiers["tmdb"]} if "tmdb" in identifiers else {}
                response = await client.get("movie", params=params)
                response.raise_for_status()
                movies = TypeAdapter(list[_Movie]).validate_python(response.json())
                matches = [
                    movie
                    for movie in movies
                    if ("imdb" not in identifiers or movie.imdb_id == identifiers["imdb"])
                    and ("tmdb" not in identifiers or str(movie.tmdb_id) == identifiers["tmdb"])
                ]
                if len(matches) != 1:
                    raise ValueError("Radarr lookup did not identify one managed movie.")
                movie = matches[0]
            else:
                response = await client.get("parse", params={"title": filename})
                response.raise_for_status()
                parsed = _Parse.model_validate(response.json())
                if parsed.movie is None:
                    raise ValueError("Radarr parser did not identify a managed movie.")
                movie = parsed.movie
            command = {"name": "RefreshMovie", "movieIds": [movie.id]}
            ids = [movie.id]
        else:
            response = await client.get("parse", params={"title": filename, "path": path})
            response.raise_for_status()
            parsed = _Parse.model_validate(response.json())
            if (
                parsed.series is None
                or not parsed.episodes
                or any(episode.series_id != parsed.series.id for episode in parsed.episodes)
            ):
                raise ValueError("Sonarr parser did not identify a managed series and associated episodes.")
            command = {"name": "RefreshSeries", "seriesIds": [parsed.series.id]}
            ids = sorted({episode.id for episode in parsed.episodes})
        if saved is None:
            response = await client.post("command", json=command)
            response.raise_for_status()
            progress = _Command.model_validate(response.json())
            if save_progress is not None:
                # Preserve accepted long-running commands before waiting. Cancellation
                # must not lose their identity and start another refresh on retry.
                await drain(asyncio.ensure_future(save_progress({"command_id": progress.id, "ids": ids})))
        while progress.status in {"queued", "started"}:
            await asyncio.sleep(1)
            response = await client.get(f"command/{progress.id}")
            response.raise_for_status()
            progress = _Command.model_validate(response.json())
        if progress.status != "completed":
            detail = f" {progress.message}" if progress.message else ""
            message = f"{action.application.title()} refresh command {progress.id} {progress.status}.{detail}"
            raise ValueError(message)
        if action.unmonitor_after_refresh:
            monitor_path = "movie/editor" if action.application == "radarr" else "episode/monitor"
            monitor_body = {"movieIds" if action.application == "radarr" else "episodeIds": ids, "monitored": False}
            response = await client.put(monitor_path, json=monitor_body)
            response.raise_for_status()
    return True
