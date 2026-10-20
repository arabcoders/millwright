from __future__ import annotations

import asyncio
import copy
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.capabilities import MkvTools
from common.media.probe import ProbeData, probe_from_raw
from common.process import ChildNotReapedError
from node.repair import prepare_input

TOOLS = MkvTools(mkvmerge="fake-mkvmerge", mkvextract="fake-mkvextract", mkvpropedit="fake-mkvpropedit")
_VTT = b"WEBVTT\n\n00:01.250 --> 00:03.500\nHello\n"
_SRT = b"1\n00:00:01,250 --> 00:00:03,500\nHello\n"
_FONT = b"\x00\x01\x00\x00" + struct.pack(">4H", 1, 16, 0, 0) + struct.pack(">4sIII", b"head", 0, 28, 4) + b"font"


class FakeMkv:
    def __init__(self, source: Path, *, font: bool = False, vtt: bool = False) -> None:
        self.source = source
        self.calls: list[tuple[str, ...]] = []
        self.original: dict = {
            "container": {"properties": {"title": "Original", "timestamp_scale": 1000000, "segment_uid": "01" * 16}},
            "tracks": [
                self.track(9, 31, "video", "V_MPEG4/ISO/AVC", "Picture"),
                self.track(3, 52, "audio", "A_AAC", "Audio"),
            ],
            "attachments": [],
        }
        if vtt:
            self.original["tracks"].insert(1, self.track(20, 89, "subtitles", "S_TEXT/WEBVTT", "Subtitle"))
        if font:
            self.original["attachments"].append(
                {
                    "id": 7,
                    "file_name": "Example.ttf",
                    "content_type": "image/jpeg",
                    "description": "Font",
                    "size": len(_FONT),
                    "properties": {"uid": 71},
                }
            )
        self.normalized: dict = copy.deepcopy(self.original)
        self.font = _FONT
        self.converted = _SRT
        self.reordered = False
        self.tamper_font = False
        self.unreaped = False
        self.hang = False
        self.started = asyncio.Event()
        self.reaping = asyncio.Event()
        self.release = asyncio.Event()

    @staticmethod
    def track(identifier: int, uid: int, kind: str, codec: str, title: str) -> dict:
        return {
            "id": identifier,
            "type": kind,
            "properties": {
                "uid": uid,
                "number": identifier + 1,
                "codec_id": codec,
                "language": "eng",
                "track_name": title,
                "default_track": False,
                "forced_track": kind == "subtitles",
                "minimum_timestamp": 1250000000,
            },
        }

    def probe(self, metadata: dict) -> ProbeData:
        raw = {"format": {"duration": "10"}, "streams": []}
        for track in metadata["tracks"]:
            properties = track["properties"]
            codec = {"V_MPEG4/ISO/AVC": "h264", "A_AAC": "aac", "S_TEXT/UTF8": "subrip"}.get(properties["codec_id"], "")
            raw["streams"].append(
                {
                    "index": len(raw["streams"]),
                    "codec_type": {"video": "video", "audio": "audio", "subtitles": "subtitle"}[track["type"]],
                    "codec_name": codec,
                    "tags": {"language": properties["language"], "title": properties["track_name"]},
                    "disposition": {"default": properties["default_track"], "forced": properties["forced_track"]},
                }
            )
        for attachment in metadata["attachments"]:
            image = attachment["content_type"].startswith("image/")
            raw["streams"].append(
                {
                    "index": len(raw["streams"]),
                    "codec_type": "video" if image else "attachment",
                    "codec_name": "mjpeg" if image else "otf" if attachment["file_name"].endswith(".otf") else "ttf",
                    "tags": {"FILENAME": attachment["file_name"], "MIMETYPE": attachment["content_type"]},
                    "disposition": {"attached_pic": image},
                }
            )
        return probe_from_raw(self.source, self.source.stat().st_size, raw)

    async def spawn(self, *argv: str, **kwargs):
        self.calls.append(argv)
        stdout = b""
        tool = argv[0]
        if tool == "fake-mkvmerge" and "-J" in argv:
            original = Path(argv[-1]) == self.source
            stdout = json.dumps(self.original if original else self.normalized).encode()
        elif tool == "fake-mkvmerge":
            Path(argv[argv.index("-o") + 1]).write_bytes(b"normalized input")
            self.normalized = copy.deepcopy(self.original)
            for n, track in enumerate(self.normalized["tracks"]):
                track["properties"]["number"] = n + 1
                if track["properties"]["codec_id"] == "S_TEXT/WEBVTT":
                    track["properties"]["codec_id"] = "S_TEXT/UTF8"
                    track["properties"]["uid"] = 1000 + n
                    track["properties"]["language_ietf"] = "eng"
            if self.reordered:
                self.normalized["tracks"].reverse()
        elif tool == "fake-mkvextract":
            destination = Path(argv[-1].split(":", 1)[-1])
            mode = argv[2]
            original = Path(argv[1]) == self.source
            data = self.font if mode == "attachments" else _VTT if original else self.converted
            if mode == "attachments" and not original and self.tamper_font:
                data = self.font[:-4] + b"xxxx"
            destination.write_bytes(data)
        elif tool == "fake-ffmpeg":
            Path(argv[-1]).write_bytes(self.converted)
        elif tool == "fake-mkvpropedit":
            for n, argument in enumerate(argv):
                if argument == "--update-attachment":
                    uid = int(argv[n + 1][1:])
                    attachment = next(
                        item for item in self.normalized["attachments"] if item["properties"]["uid"] == uid
                    )
                    attachment["content_type"] = argv[n - 1]
        elif tool == "fake-ffprobe":
            stdout = json.dumps(self.probe(self.normalized).raw).encode()
        else:
            raise AssertionError(argv)
        owner = self

        class Child:
            returncode = None if tool == "fake-mkvpropedit" and (owner.hang or owner.unreaped) else 0

            async def communicate(self):
                if owner.unreaped and self.returncode is None:
                    raise OSError("pipe failed")
                if owner.hang and self.returncode is None:
                    owner.started.set()
                    await asyncio.Event().wait()
                return stdout, b""

            def send_signal(self, _signal):
                pass

            async def wait(self):
                if owner.unreaped and self.returncode is None:
                    raise TimeoutError
                if owner.hang and self.returncode is None:
                    owner.reaping.set()
                    await owner.release.wait()
                    self.returncode = -9
                return self.returncode

        return Child()


def context(fake: FakeMkv):
    return prepare_input(
        fake.source,
        fake.probe(fake.original),
        fake.source.parent,
        tools=TOOLS,
        workspace_dir=fake.source.parent,
        ffmpeg_bin="fake-ffmpeg",
        ffprobe_bin="fake-ffprobe",
        log=lambda _text: None,
    )


@pytest.mark.asyncio
async def test_unchanged_input(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original")
    fake = FakeMkv(source)
    async with prepare_input(
        source,
        fake.probe(fake.original),
        tmp_path,
        tools=MkvTools(),
        workspace_dir=tmp_path,
        ffmpeg_bin="missing",
        ffprobe_bin="missing",
        log=lambda _text: None,
    ) as prepared:
        assert prepared.path == source
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["ttf", "otf"])
async def test_font_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extension: str) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original bytes")
    fake = FakeMkv(source, font=True)
    fake.original["attachments"][0]["file_name"] = f"Example.{extension}"
    fake.normalized = copy.deepcopy(fake.original)
    if extension == "otf":
        fake.font = b"OTTO" + _FONT[4:]
    monkeypatch.setattr("node.process.create_tool", fake.spawn)
    async with context(fake) as prepared:
        assert prepared.path != source
        assert prepared.path.read_bytes() == b"original bytes"
        expected_mime = "application/x-truetype-font" if extension == "ttf" else "application/vnd.ms-opentype"
        assert prepared.probe.streams[-1].mimetype == expected_mime
        assert source.read_bytes() == b"original bytes"
        output = prepared.path
    assert not output.exists()
    assert not any("-o" in call for call in fake.calls)
    assert any("=71" in call for call in fake.calls)


@pytest.mark.asyncio
async def test_webvtt_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original bytes")
    fake = FakeMkv(source, vtt=True, font=True)
    monkeypatch.setattr("node.process.create_tool", fake.spawn)
    async with context(fake) as prepared:
        assert [stream.codec_type for stream in prepared.probe.streams] == ["video", "subtitle", "audio", "attachment"]
        assert prepared.probe.subtitle[0].codec == "subrip"
        assert prepared.probe.subtitle[0].title == "Subtitle"
        assert prepared.probe.subtitle[0].disposition_forced
    merge = next(call for call in fake.calls if "-o" in call)
    assert merge[merge.index("--track-order") + 1] == "0:9,1:0,0:3"
    assert "--no-subtitles" in merge
    assert source.read_bytes() == b"original bytes"
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["font", "font_digest", "order", "unknown"])
async def test_invalid_repair_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original bytes")
    fake = FakeMkv(source, font=failure.startswith("font"), vtt=not failure.startswith("font"))
    if failure == "font":
        fake.font = b"JPEG is not a font"
    elif failure == "font_digest":
        fake.tamper_font = True
    elif failure == "order":
        fake.reordered = True
    else:
        fake.original["tracks"][1]["properties"]["codec_id"] = "S_UNKNOWN"
    monkeypatch.setattr("node.process.create_tool", fake.spawn)
    with pytest.raises(ValueError):
        async with context(fake):
            pytest.fail("An invalid repair was accepted.")
    assert source.read_bytes() == b"original bytes"
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.asyncio
async def test_missing_tools_visible(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original")
    fake = FakeMkv(source, font=True)
    with pytest.raises(ValueError):
        async with prepare_input(
            source,
            fake.probe(fake.original),
            tmp_path,
            tools=MkvTools(),
            workspace_dir=tmp_path,
            ffmpeg_bin="missing",
            ffprobe_bin="missing",
            log=lambda _text: None,
        ):
            pytest.fail("Missing tools were ignored.")
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.asyncio
async def test_unreaped_retains_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original bytes")
    fake = FakeMkv(source, font=True)
    fake.unreaped = True
    monkeypatch.setattr("node.process.create_tool", fake.spawn)
    with pytest.raises(ChildNotReapedError):
        async with context(fake):
            pytest.fail("An unreaped child was completed.")
    assert (tmp_path / "repair-input.mkv").exists()
    assert source.read_bytes() == b"original bytes"


@pytest.mark.asyncio
async def test_cancellation_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original bytes")
    fake = FakeMkv(source, font=True)
    fake.hang = True
    monkeypatch.setattr("node.process.create_tool", fake.spawn)

    async def preparing():
        async with context(fake):
            pytest.fail("A cancelled repair completed.")

    task = asyncio.create_task(preparing())
    try:
        await asyncio.wait_for(fake.started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(fake.reaping.wait(), 2)
        task.cancel()
        assert (tmp_path / "repair-input.mkv").exists()
        assert not task.done()
        fake.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert list(tmp_path.iterdir()) == [source]
    finally:
        fake.release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_occupied_output_retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original")
    occupied = tmp_path / "repair-input.mkv"
    occupied.write_bytes(b"recovery files")
    fake = FakeMkv(source, font=True)
    monkeypatch.setattr("node.process.create_tool", fake.spawn)
    with pytest.raises(FileExistsError):
        async with context(fake):
            pytest.fail("Existing repair output was overwritten.")
    assert occupied.read_bytes() == b"recovery files"
    assert source.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_repair_space_reserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original")
    fake = FakeMkv(source, vtt=True)
    monkeypatch.setattr("node.process.create_tool", fake.spawn)
    monkeypatch.setattr("node.repair.shutil.disk_usage", lambda _path: SimpleNamespace(free=20))
    with pytest.raises(ValueError):
        async with context(fake):
            pytest.fail("Normalization did not reserve space for subtitle repair files.")
    assert len(fake.calls) == 1
    assert list(tmp_path.iterdir()) == [source]
