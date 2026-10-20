from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from common.process import spawn_child, stop_child

LOG = logging.getLogger("node.capabilities")

_CMD_TIMEOUT_S = 15.0
CPUINFO = Path("/proc/cpuinfo")
MEMINFO = Path("/proc/meminfo")
_GPU_QUERY = ("nvidia-smi", "--query-gpu=name", "--format=csv,noheader")
_GPU_USAGE_QUERY = ("nvidia-smi", "--query-gpu=index,name,utilization.gpu", "--format=csv,noheader,nounits")
_INTEL_GPU_QUERY = ("intel_gpu_top", "-J", "-s", "100")


@dataclass(kw_only=True)
class SystemFacts:
    cpu_model: str = ""
    cpu_cores: int = 0
    memory_gb: float = 0.0
    gpus: list[str] = field(default_factory=list)


def _cpu_facts(path: Path) -> tuple[str, int]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "", 0
    model = ""
    cores = 0
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "model name" and not model:
            model = value.strip()
        elif key == "processor":
            cores += 1
    return model, cores


def _memory_gb(path: Path) -> float:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0.0
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            return round(int(line.split()[1]) / 2**20, 1)
    return 0.0


async def _gpu_names(drm_root: Path = Path("/sys/class/drm")) -> list[str]:
    names: list[str] = []
    seen: set[tuple[str, str]] = set()
    for vendor_path in sorted(drm_root.glob("card*/device/vendor")):
        try:
            vendor = vendor_path.read_text(encoding="ascii").strip().lower()
        except OSError:
            continue
        if vendor not in {"0x1002", "0x8086", "0x10de"}:
            continue
        device_path = vendor_path.parent
        try:
            device_id = (device_path / "device").read_text(encoding="ascii").strip().lower()
        except OSError:
            device_id = ""
        identity = (vendor, str(device_path.resolve()))
        if identity in seen:
            continue
        seen.add(identity)
        if vendor == "0x1002":
            names.append(f"AMD GPU{f' ({device_id})' if device_id else ''}")
        elif vendor == "0x8086":
            names.append(f"Intel GPU{f' ({device_id})' if device_id else ''}")
    nvidia_present = "0x10de" in {vendor for vendor, _ in seen}
    if shutil.which(_GPU_QUERY[0]) is None:
        return names + (["NVIDIA GPU"] if nvidia_present else [])
    try:
        proc = await spawn_child(
            *_GPU_QUERY, force_on_cancel=True, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError:
        return names + (["NVIDIA GPU"] if nvidia_present else [])
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT_S)
    except TimeoutError:
        await stop_child(proc, force=True)
        return names + (["NVIDIA GPU"] if nvidia_present else [])
    except Exception, asyncio.CancelledError:
        await stop_child(proc, force=True)
        raise
    if proc.returncode != 0:
        return names + (["NVIDIA GPU"] if nvidia_present else [])
    nvidia_names = [name.strip() for name in stdout.decode(errors="replace").splitlines() if name.strip()]
    return names + (nvidia_names or (["NVIDIA GPU"] if nvidia_present else []))


async def resource_sample(
    previous_cpu: tuple[int, int] | None = None,
    drm_root: Path = Path("/sys/class/drm"),
) -> tuple[float | None, float | None, list[dict[str, str | float | None]], tuple[int, int] | None]:
    try:
        line = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0]
        values = [int(value) for value in line.split()[1:]]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        current = (total, idle)
        cpu = (
            None
            if previous_cpu is None or total == previous_cpu[0]
            else 100 * (1 - (idle - previous_cpu[1]) / (total - previous_cpu[0]))
        )
        cpu = max(0.0, min(100.0, cpu)) if cpu is not None else None
    except OSError, IndexError, ValueError:
        cpu = None
        current = None
    try:
        memory = {
            line.split()[0].rstrip(":"): int(line.split()[1])
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
            if len(line.split()) >= 2
        }
        memory_percent = max(0.0, min(100.0, 100 * (1 - memory["MemAvailable"] / memory["MemTotal"])))
    except OSError, KeyError, ValueError, ZeroDivisionError:
        memory_percent = None
    vendors: list[tuple[str, Path]] = []
    for vendor_path in sorted(drm_root.glob("card*/device/vendor")):
        with contextlib.suppress(OSError):
            vendors.append((vendor_path.read_text(encoding="ascii").strip().lower(), vendor_path.parent))
    readings: list[dict[str, str | float | None]] = []
    intel_cards: list[str] = []
    nvidia_cards: list[str] = []
    for vendor, device_path in vendors:
        device_id = ""
        if vendor in {"0x1002", "0x8086"}:
            with contextlib.suppress(OSError):
                device_id = (device_path / "device").read_text(encoding="ascii").strip().lower()
        card = device_path.parent.name
        label = f"{device_id}, {card}" if device_id else card
        if vendor == "0x1002":
            busy_path = device_path / "gpu_busy_percent"
            percent: float | None = None
            with contextlib.suppress(OSError, ValueError):
                percent = float(busy_path.read_text(encoding="ascii").strip())
            readings.append({"name": f"AMD GPU ({label})", "percent": percent})
        elif vendor == "0x8086":
            intel_cards.append(label)
        elif vendor == "0x10de":
            nvidia_cards.append(card)
    if len(intel_cards) == 1 and shutil.which(_INTEL_GPU_QUERY[0]) is not None:
        intel_usage = await _intel_gpu_usage()
        readings.append({"name": f"Intel GPU ({intel_cards[0]})", "percent": intel_usage})
    else:
        readings.extend({"name": f"Intel GPU ({card})", "percent": None} for card in intel_cards)
    if shutil.which(_GPU_USAGE_QUERY[0]) is not None:
        nvidia_readings = await _nvidia_gpu_usage()
        readings.extend(nvidia_readings or ({"name": f"NVIDIA GPU ({card})", "percent": None} for card in nvidia_cards))
    else:
        readings.extend({"name": f"NVIDIA GPU ({card})", "percent": None} for card in nvidia_cards)
    for reading in readings:
        if reading["percent"] is not None:
            reading["percent"] = max(0.0, min(100.0, float(reading["percent"])))
    return cpu, memory_percent, readings, current


async def _nvidia_gpu_usage() -> list[dict[str, str | float | None]]:
    proc = None
    try:
        proc = await spawn_child(
            *_GPU_USAGE_QUERY, force_on_cancel=True, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        if proc.returncode == 0:
            readings = []
            for line in stdout.decode(errors="replace").splitlines():
                fields = [field.strip() for field in line.split(",", 2)]
                if len(fields) == 3:
                    percent = None if fields[2].upper() == "N/A" else float(fields[2])
                    readings.append({"name": f"NVIDIA GPU {fields[0]} ({fields[1]})", "percent": percent})
            return readings
    except OSError, TimeoutError, ValueError:
        if proc is not None:
            await stop_child(proc, force=True)
    except Exception, asyncio.CancelledError:
        if proc is not None:
            await stop_child(proc, force=True)
        raise
    return []


def _busy_values(value: object) -> list[float]:
    if isinstance(value, dict):
        result = []
        for key, child in value.items():
            if "busy" in key.lower() and isinstance(child, (int, float)):
                result.append(float(child))
            result.extend(_busy_values(child))
        return result
    if isinstance(value, list):
        return [item for child in value for item in _busy_values(child)]
    return []


async def _intel_gpu_usage() -> float | None:
    proc = None
    try:
        proc = await spawn_child(
            *_INTEL_GPU_QUERY, force_on_cancel=True, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        if proc.stdout is None:
            return None
        output = ""
        async with asyncio.timeout(2.5):
            while line := await proc.stdout.readline():
                output += line.decode(errors="replace")
                candidate = output.lstrip("[\n ")
                try:
                    sample, _ = json.JSONDecoder().raw_decode(candidate)
                except ValueError:
                    continue
                busy = _busy_values(sample)
                return max(busy) if busy else None
    except OSError, TimeoutError:
        pass
    finally:
        if proc is not None:
            await stop_child(proc, force=True)
    return None


async def system_facts(
    cpuinfo: Path = CPUINFO, meminfo: Path = MEMINFO, drm_root: Path = Path("/sys/class/drm")
) -> SystemFacts:
    model, cores = _cpu_facts(cpuinfo)
    gpus = await _gpu_names() if drm_root == Path("/sys/class/drm") else await _gpu_names(drm_root=drm_root)
    return SystemFacts(cpu_model=model, cpu_cores=cores, memory_gb=_memory_gb(meminfo), gpus=gpus)
