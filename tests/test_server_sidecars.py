import errno
import json
import logging
import os
from pathlib import Path

import pytest

from common.media.probe import ProbeData, ProbeStream
from server.flows.definition import FlowDefinition, FlowEdge, PluginNode, TerminalNode
from server.flows.dryrun import dry_run
from server.flows.sidecars import sidecar_metadata, sidecar_subtitles
from tests.helpers import plugin_host

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_discovers_subtitles(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.touch()
    for name in ("movie.vtt", "movie.srt", "movie.ass", "movie.ssa", "movie.txt"):
        (tmp_path / name).touch()
    (tmp_path / "directory.srt").mkdir()
    (tmp_path / "link.srt").symlink_to(tmp_path / "movie.srt")

    assert sidecar_subtitles(str(source)) == ["movie.ass", "movie.srt", "movie.ssa", "movie.vtt"]


def test_missing_parent_returns_empty(tmp_path: Path) -> None:
    assert sidecar_subtitles(str(tmp_path / "missing" / "movie.mkv")) == []


def test_nondirectory_parent_empty(tmp_path: Path) -> None:
    parent = tmp_path / "file"
    parent.touch()
    assert sidecar_subtitles(str(parent / "movie.mkv")) == []
    assert sidecar_subtitles(str(parent / "nested" / "movie.mkv")) == []


def test_symlink_parent_discovery(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "movie.srt").touch()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    assert sidecar_subtitles(str(link / "movie.mkv")) == ["movie.srt"]


@pytest.mark.parametrize("boundary", ["parent", "subtitle"])
@pytest.mark.parametrize("stat_error", [errno.EACCES, errno.EIO])
def test_sidecar_lookup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, stat_error: int
) -> None:
    source = tmp_path / "movie.mkv"
    subtitle = tmp_path / "movie.srt"
    subtitle.write_text("subtitle")
    target = tmp_path if boundary == "parent" else subtitle
    stat = os.stat

    def unavailable(path, *args, **kwargs):
        if path in (target, str(target)):
            raise OSError(stat_error, "subtitle storage unavailable")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", unavailable)
    with pytest.raises(OSError) as failure:
        sidecar_subtitles(str(source))
    assert failure.value.errno == stat_error


def test_disappearing_subtitle_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    subtitle = tmp_path / "movie.srt"
    subtitle.touch()
    stat = os.stat

    def disappeared(path, *args, **kwargs):
        if path in (subtitle, str(subtitle)):
            raise FileNotFoundError(errno.ENOENT, "subtitle disappeared")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", disappeared)
    assert sidecar_subtitles(str(source)) == []


@pytest.mark.asyncio
async def test_dryrun_burns_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_text("fake")
    (tmp_path / "movie.eng.srt").write_text("fake")
    (tmp_path / "movie.srt").write_text("fake")
    flow = FlowDefinition(
        entry="burn",
        nodes=[PluginNode(id="burn", plugin="transcode.burn_subtitles"), TerminalNode(id="done", outcome="done")],
        edges=[FlowEdge(src="burn", port="plan", dst="done")],
    )
    probe = ProbeData(
        file_path=str(source),
        container="matroska",
        duration_s=1,
        size_bytes=4,
        streams=[ProbeStream(index=0, codec="h264", codec_type="video")],
    )

    async with plugin_host([REPO_ROOT / "plugins"]) as host:
        result = await dry_run(flow, probe, host, str(source))

    assert result.flow.plan is not None
    assert result.flow.plan.burn_subtitle is not None
    assert result.flow.plan.burn_subtitle.filename == "movie.eng.srt"
    assert result.flow.steps[0].reason is not None
    assert result.render is not None
    vf = result.render.argv[result.render.argv.index("-filter:v:0") + 1]
    assert vf == f"subtitles=filename={source.parent / 'movie.eng.srt'}"


def metadata_flow(**inputs) -> FlowDefinition:
    return FlowDefinition(
        entry="metadata",
        nodes=[
            PluginNode(id="metadata", plugin="action.embed_metadata", inputs=inputs),
            PluginNode(id="remux", plugin="transcode.remux", inputs={"container": "mp4"}),
        ],
        edges=[FlowEdge(src="metadata", port="next", dst="remux")],
    )


@pytest.mark.asyncio
async def test_metadata_sources(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    (tmp_path / "movie.nfo").write_text(
        "<movie><title>NFO title</title><plot> </plot><premiered>2019-08-15</premiered>"
        "<genre>Horror</genre><genre>Thriller</genre></movie>"
    )
    (tmp_path / "movie.info.json").write_text(
        json.dumps(
            {
                "_type": "video",
                "title": "JSON title",
                "description": "Multiline\nUnicode العربية",
                "upload_date": "20201204",
                "channel": "Uploader",
                "categories": ["Entertainment"],
                "formats": [{"url": "https://example.test/not-forwarded"}],
            }
        )
    )
    (tmp_path / "tvshow.nfo").write_text("malformed unrelated NFO")
    (tmp_path / "other.info.json").write_text("malformed unrelated JSON")
    probe = ProbeData(file_path=str(source), container="mkv", duration_s=1, size_bytes=4, streams=[])
    async with plugin_host([REPO_ROOT / "plugins"]) as host:
        result = await dry_run(metadata_flow(), probe, host, str(source))
        reverse = await dry_run(metadata_flow(source_priority=["info_json", "nfo"]), probe, host, str(source))
    assert result.flow.status == "matched"
    assert result.flow.plan is not None and result.flow.plan.metadata is not None
    assert result.flow.plan.metadata.model_dump(exclude_none=True) == {
        "title": "NFO title",
        "description": "Multiline\nUnicode العربية",
        "date": "2019-08-15",
        "genre": "Horror; Thriller",
        "artist": "Uploader",
    }
    assert result.render is not None and "title=NFO title" in result.render.argv
    assert reverse.flow.plan is not None and reverse.flow.plan.metadata is not None
    assert reverse.flow.plan.metadata.title == "JSON title"
    assert reverse.flow.plan.metadata.date == "2020-12-04"
    assert not result.flow.actions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (".nfo", "<movie>"),
        (".nfo", "https://www.imdb.com/title/tt1234567/\n<movie>"),
        (".info.json", '{"title":7}'),
    ],
)
async def test_metadata_errors(tmp_path: Path, suffix: str, content: str) -> None:
    source = tmp_path / "movie.mkv"
    candidate = source.with_suffix(suffix)
    candidate.write_text(content)
    probe = ProbeData(file_path=str(source), container="mkv", duration_s=1, size_bytes=4, streams=[])
    requested = metadata_flow()
    unrelated = FlowDefinition(entry="remux", nodes=[PluginNode(id="remux", plugin="transcode.remux")])
    async with plugin_host([REPO_ROOT / "plugins"]) as host:
        result = await dry_run(requested, probe, host, str(source))
        skipped = await dry_run(unrelated, probe, host, str(source))
    assert result.flow.status == "error"
    assert str(candidate) in (result.flow.steps[0].error or "")
    assert "Fix it or remove it" in (result.flow.steps[0].error or "")
    assert skipped.flow.status == "skipped"


@pytest.mark.asyncio
async def test_link_nfo(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.with_suffix(".nfo").write_text("\n https://www.imdb.com/title/tt1234567/ \n")
    source.with_suffix(".info.json").write_text('{"title":"JSON title"}')
    probe = ProbeData(file_path=str(source), container="mkv", duration_s=1, size_bytes=4, streams=[])
    async with plugin_host([REPO_ROOT / "plugins"]) as host:
        result = await dry_run(metadata_flow(), probe, host, str(source))
    assert result.flow.status == "matched"
    assert result.flow.plan is not None and result.flow.plan.metadata is not None
    assert result.flow.plan.metadata.title == "JSON title"
    assert result.render is not None and "title=JSON title" in result.render.argv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "alternate", "error"),
    [
        (b'{"title":"Broken\xff"}', True, "can't decode byte"),
        (b'{"title":', True, "Expecting value"),
        (b'{"title":', False, "Expecting value"),
    ],
)
async def test_unusable_json(tmp_path: Path, caplog: pytest.LogCaptureFixture, content, alternate, error) -> None:
    source = tmp_path / "movie.mp4"
    candidate = source.with_suffix(".info.json")
    candidate.write_bytes(content)
    if alternate:
        source.with_suffix(".nfo").write_text("<movie><title>NFO title</title></movie>")
    probe = ProbeData(file_path=str(source), container="mp4", duration_s=1, size_bytes=4, streams=[])
    async with plugin_host([REPO_ROOT / "plugins"]) as host:
        with caplog.at_level(logging.WARNING, logger="server.flows.sidecars"):
            result = await dry_run(metadata_flow(when_no_media_changes="remux"), probe, host, str(source))
    if alternate:
        assert result.flow.status == "matched"
        assert result.flow.plan is not None and result.flow.plan.metadata is not None
        assert result.flow.plan.metadata.title == "NFO title"
        assert result.render is not None and "title=NFO title" in result.render.argv
    else:
        assert result.flow.status == "skipped" and result.flow.plan is None
    warnings = [record.getMessage() for record in caplog.records if record.name == "server.flows.sidecars"]
    assert any(str(candidate) in message and error in message for message in warnings)


def test_episode_metadata(tmp_path: Path) -> None:
    source = tmp_path / "episode.mkv"
    (tmp_path / "episode.nfo").write_text(
        "<episodedetails><title>Episode</title><plot>Plot</plot><season>03</season>"
        "<episode>2</episode><aired>2026-06-28</aired></episodedetails>"
    )
    (tmp_path / "tvshow.nfo").write_text("<tvshow><title>Must not inherit</title></tvshow>")
    (tmp_path / "episode.info.json").write_text('{"_type":"playlist","title":"Must not select","entries":[]}')
    result = sidecar_metadata(str(source), metadata_flow())
    assert not result.errors
    assert list(result.sources) == ["nfo"]
    assert result.sources["nfo"].model_dump(exclude_none=True) == {
        "title": "Episode",
        "description": "Plot",
        "date": "2026-06-28",
        "season_number": "3",
        "episode_sort": "2",
    }
