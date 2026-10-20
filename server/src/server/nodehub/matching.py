from __future__ import annotations

from typing import TYPE_CHECKING

from common.plan.renderer import audio_targets

if TYPE_CHECKING:
    from common.media.probe import ProbeData
    from common.plan.models import FFmpegPlan
    from common.protocol import NodeCapabilities


def tags_match(required: list[str], node_tags: list[str]) -> bool:
    if not required:
        return True
    available = set(node_tags)
    return any(tag in available for tag in required)


def can_execute(
    capabilities: NodeCapabilities,
    workspace_ready: bool,
    plan: FFmpegPlan | None,
    probe: ProbeData,
    required_tags: list[str] | None = None,
    required_encoders: list[str] | None = None,
) -> tuple[bool, str]:
    if not workspace_ready:
        return False, "workspace is not ready"
    if plan is None:
        return False, "transcode plan is required"
    try:
        selected = plan.select_encoder(capabilities.encoders)
    except ValueError as exc:
        return False, str(exc)
    if selected.video is not None and selected.video.encoder not in capabilities.encoders:
        return False, f"encoder {selected.video.encoder} unavailable"
    for target in audio_targets(selected, probe).values():
        if target.codec != "copy" and target.codec not in capabilities.encoders:
            return False, f"encoder {target.codec} unavailable"
    return requirements_met(capabilities, required_tags, required_encoders)


def requirements_met(
    capabilities: NodeCapabilities,
    required_tags: list[str] | None,
    required_encoders: list[str] | None,
) -> tuple[bool, str]:
    missing = next((encoder for encoder in required_encoders or [] if encoder not in capabilities.encoders), None)
    if missing is not None:
        return False, f"missing encoder {missing}"
    if not tags_match(required_tags or [], capabilities.worker_tags):
        return False, f"no worker tag matches {required_tags}"
    return True, ""
