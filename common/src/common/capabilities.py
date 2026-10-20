from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from common.process import spawn_child, stop_child

if TYPE_CHECKING:
    from collections.abc import Iterable

LOG = logging.getLogger("common.capabilities")

_CMD_TIMEOUT_S = 15.0
_HW_TEST_TIMEOUT_S = 10.0
_HW_STDERR_TAIL = 200
_CAPABILITY_FLAGS = frozenset("DEVASFXB.")
_CODEC_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

# Suffix-matched against probed encoder names (hevc_nvenc -> nvenc).
HW_ENCODER_FAMILIES = frozenset({"nvenc", "qsv", "vaapi", "videotoolbox", "amf", "vulkan"})


class CapabilitiesError(Exception):
    pass


def low_priority_argv() -> tuple[str, ...]:
    nice, ionice = shutil.which("nice"), shutil.which("ionice")
    if nice is None or ionice is None:
        missing = ", ".join(name for name, binary in (("nice", nice), ("ionice", ionice)) if binary is None)
        message = f"Background media work requires {missing} on PATH for subprocess priority."
        raise CapabilitiesError(message)
    return nice, "-n", "10", ionice, "-c", "2", "-n", "7"


@dataclass(kw_only=True, frozen=True, slots=True)
class MkvTools:
    mkvmerge: str | None = None
    mkvextract: str | None = None
    mkvpropedit: str | None = None


def resolve_mkv_tools(*, mkvpropedit: str = "mkvpropedit") -> MkvTools:
    return MkvTools(
        mkvmerge=shutil.which("mkvmerge"),
        mkvextract=shutil.which("mkvextract"),
        mkvpropedit=shutil.which(mkvpropedit),
    )


@dataclass(kw_only=True)
class Capabilities:
    ffmpeg_version: str
    encoders: frozenset[str]
    video_encoders: frozenset[str]
    audio_encoders: frozenset[str]
    decoders: frozenset[str]
    audio_decoders: frozenset[str]
    hwaccels: frozenset[str]


async def _run(ffmpeg_bin: str | Path, *args: str, argv_prefix: tuple[str, ...] = ()) -> str:
    proc = await spawn_child(
        *argv_prefix,
        str(ffmpeg_bin),
        *args,
        force_on_cancel=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT_S)
    except TimeoutError:
        await stop_child(proc, force=True)
        msg = f"ffmpeg {args[0]} timed out after {_CMD_TIMEOUT_S}s"
        raise CapabilitiesError(msg) from None
    except Exception, asyncio.CancelledError:
        await stop_child(proc, force=True)
        raise
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        msg = f"ffmpeg {args[0]} exited with {proc.returncode}. {detail or 'No stderr output.'}"
        raise CapabilitiesError(msg)
    return stdout.decode(errors="replace")


def _section(text: str, header: str) -> list[str]:
    lines = text.splitlines()
    if header not in lines:
        return []
    return [line for line in lines[lines.index(header) + 1 :] if line.strip()]


def _names(text: str, header: str, flag: str | None = None) -> frozenset[str]:
    names = set[str]()
    for line in _section(text, header):
        parts = line.split()
        if len(parts) < 2:
            continue
        flags, name = parts[:2]
        if len(flags) != 6 or flags[0] not in "VAS" or not set(flags) <= _CAPABILITY_FLAGS:
            continue
        if flag is not None and flags[0] != flag:
            continue
        if _CODEC_NAME.fullmatch(name):
            names.add(name)
    return frozenset(names)


def _hwaccels(text: str) -> frozenset[str]:
    return frozenset(line.split()[0] for line in _section(text, "Hardware acceleration methods:"))


async def probe_capabilities(ffmpeg_bin: str | Path, *, argv_prefix: tuple[str, ...] = ()) -> Capabilities:
    version_out = await _run(ffmpeg_bin, "-version", argv_prefix=argv_prefix)
    hwaccel_out = await _run(ffmpeg_bin, "-hide_banner", "-hwaccels", argv_prefix=argv_prefix)
    encoder_out = await _run(ffmpeg_bin, "-hide_banner", "-encoders", argv_prefix=argv_prefix)
    decoder_out = await _run(ffmpeg_bin, "-hide_banner", "-decoders", argv_prefix=argv_prefix)
    first_line = next(iter(version_out.splitlines()), "")
    return Capabilities(
        ffmpeg_version=first_line,
        encoders=_names(encoder_out, "Encoders:"),
        video_encoders=_names(encoder_out, "Encoders:", "V"),
        audio_encoders=_names(encoder_out, "Encoders:", "A"),
        decoders=_names(decoder_out, "Decoders:"),
        audio_decoders=_names(decoder_out, "Decoders:", "A"),
        hwaccels=_hwaccels(hwaccel_out),
    )


async def _decode_sample(
    ffmpeg_bin: str | Path, encoder: str, argv_prefix: tuple[str, ...]
) -> tuple[bytes, str | None]:
    argv = [
        *argv_prefix,
        str(ffmpeg_bin),
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=size=128x128:rate=25",
        "-frames:v",
        "1",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-c:v",
        encoder,
    ]
    # QSV needs in-band parameter sets. NUT's global headers alone do not initialize its decoder.
    if encoder == "libx264":
        argv.extend(["-x264-params", "repeat-headers=1"])
    elif encoder == "libx265":
        argv.extend(["-x265-params", "pools=none:frame-threads=1:wpp=0:repeat-headers=1"])
    argv.extend(["-f", "nut", "pipe:1"])
    proc = await spawn_child(
        *argv,
        force_on_cancel=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), _HW_TEST_TIMEOUT_S)
    except TimeoutError:
        await stop_child(proc, force=True)
        return b"", f"Sample encoding timed out after {_HW_TEST_TIMEOUT_S:g}s."
    except Exception, asyncio.CancelledError:
        await stop_child(proc, force=True)
        raise
    if proc.returncode != 0:
        tail = stderr.decode(errors="replace").strip()[-2000:]
        return b"", f"Sample encoding exited {proc.returncode}: {tail or 'No stderr output.'}"
    return stdout, None


async def _test_decode(
    ffmpeg_bin: str | Path, backend: str, decoder: str, sample: bytes, argv_prefix: tuple[str, ...]
) -> str | None:
    proc = await spawn_child(
        *argv_prefix,
        str(ffmpeg_bin),
        "-v",
        "error",
        "-progress",
        "pipe:1",
        "-init_hw_device",
        f"{backend}=healthcheck",
        "-hwaccel",
        backend,
        "-hwaccel_device",
        "healthcheck",
        "-hwaccel_output_format",
        backend,
        "-threads",
        "1",
        "-c:v",
        decoder,
        "-i",
        "pipe:0",
        "-frames:v",
        "1",
        # Requiring GPU frames prevents native decoders from passing through a software fallback.
        "-vf",
        "hwdownload,format=nv12",
        "-filter_threads",
        "1",
        "-threads",
        "1",
        "-f",
        "null",
        "-",
        force_on_cancel=True,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(sample), _HW_TEST_TIMEOUT_S)
    except TimeoutError:
        await stop_child(proc, force=True)
        return f"Decoder test timed out after {_HW_TEST_TIMEOUT_S:g}s."
    except Exception, asyncio.CancelledError:
        await stop_child(proc, force=True)
        raise
    # libva prints initialization info even when FFmpeg requests error-only output.
    errors = [line for line in stderr.splitlines() if line.strip() and not line.startswith(b"libva info:")]
    if proc.returncode != 0 or errors:
        tail = stderr.decode(errors="replace").strip()[-2000:]
        return f"Decoder test exited {proc.returncode}: {tail or 'No stderr output.'}"
    if b"frame=1" not in stdout.splitlines():
        return "Decoder test produced no frames."
    return None


async def verify_decoders(
    ffmpeg_bin: str | Path, capabilities: Capabilities, codecs: Iterable[str], *, argv_prefix: tuple[str, ...] = ()
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    verified: dict[str, tuple[str, str]] = {}
    diagnostics: list[str] = []
    sample_encoders = {"h264": "libx264", "hevc": "libx265", "av1": "libaom-av1", "vp8": "libvpx", "vp9": "libvpx-vp9"}
    for codec in sorted(set(codecs)):
        decoder_codec = {"mpeg1video": "mpeg1", "mpeg2video": "mpeg2"}.get(codec, codec)
        candidates = [
            (backend, decoder)
            for backend, decoder in (
                ("cuda", f"{decoder_codec}_cuvid"),
                ("qsv", f"{decoder_codec}_qsv"),
                ("vaapi", codec),
            )
            if backend in capabilities.hwaccels and decoder in capabilities.decoders
        ]
        if not candidates:
            diagnostics.append(f"No hardware decoder candidate for {codec}.")
            continue
        encoder = sample_encoders.get(codec, codec)
        if encoder not in capabilities.video_encoders:
            diagnostics.append(f"Cannot verify {codec} hardware decoding. Sample encoder {encoder} is unavailable.")
            continue
        # Encoding creates a known-good input. Only successful decoding confirms hardware support.
        sample, error = await _decode_sample(ffmpeg_bin, encoder, argv_prefix)
        if error is not None:
            diagnostics.append(f"Cannot verify {codec} hardware decoding. {error}")
            continue
        for backend, decoder in candidates:
            error = await _test_decode(ffmpeg_bin, backend, decoder, sample, argv_prefix)
            if error is None:
                verified[codec] = (backend, decoder)
                break
            diagnostics.append(f"Hardware decoder {decoder} ({backend}) is unavailable. {error}")
    return verified, diagnostics


def derive_worker_tags(encoders: Iterable[str]) -> set[str]:
    if any(encoder.endswith(tuple(HW_ENCODER_FAMILIES)) for encoder in encoders):
        return {"cpu", "gpu"}
    return {"cpu"}


def hw_encoders(encoders: Iterable[str]) -> list[str]:
    return sorted(name for name in encoders if name.endswith(tuple(HW_ENCODER_FAMILIES)))


async def _test_encode(ffmpeg_bin: str | Path, encoder: str) -> str | None:
    device = ["-init_hw_device", "vaapi=va", "-filter_hw_device", "va"] if encoder.endswith("_vaapi") else []
    upload = ["-vf", "format=nv12,hwupload"] if device else []
    argv = (
        str(ffmpeg_bin),
        "-hide_banner",
        *device,
        "-f",
        "lavfi",
        "-i",
        "testsrc2=duration=1:size=256x256:rate=10",
        "-frames:v",
        "10",
        *upload,
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    )
    try:
        # FFmpeg's interactive stdin can leave terminal modes changed when a probe is killed.
        proc = await spawn_child(
            *argv,
            force_on_cancel=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        return f"spawn {type(error).__name__}: {error}"
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_HW_TEST_TIMEOUT_S)
    except TimeoutError:
        await stop_child(proc, force=True)
        return f"timeout after {_HW_TEST_TIMEOUT_S}s"
    except Exception, asyncio.CancelledError:
        await stop_child(proc, force=True)
        raise
    if proc.returncode != 0:
        tail = (stderr or b"").decode(errors="replace").strip()[-_HW_STDERR_TAIL:]
        return f"Exit code {proc.returncode}. {tail or 'No stderr output.'}"
    return None


async def verify_encoders(ffmpeg_bin: str | Path, encoders: Iterable[str]) -> frozenset[str]:
    # Listed hardware encoders can fail because of drivers, session limits, or permissions.
    verified = set(encoders)
    dropped: list[str] = []
    for name in hw_encoders(verified):
        cause = await _test_encode(ffmpeg_bin, name)
        if cause is None:
            continue
        verified.discard(name)
        dropped.append(name)
        LOG.debug("Hardware encoder %s dropped", name, extra={"error": cause})
    verified_hw = hw_encoders(verified)
    LOG.info("hardware encoder verification: verified %s, dropped %s", verified_hw, sorted(dropped))
    return frozenset(verified)


def binary_identity(ffmpeg_bin: str | Path) -> tuple[str, int, int, int, int]:
    """Identify binary changes that invalidate cached capabilities."""
    resolved = shutil.which(str(ffmpeg_bin)) or str(ffmpeg_bin)
    info = Path(resolved).stat()
    return (resolved, info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
