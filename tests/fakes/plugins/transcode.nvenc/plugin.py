from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.plan import FFmpegPlan, VerificationSpec, VideoTarget

if TYPE_CHECKING:
    from common.ctx import PluginContext


def decide(_ctx: PluginContext, _inputs: dict[str, Any]) -> dict[str, Any]:
    plan = FFmpegPlan(container="mkv", video=VideoTarget(encoder="hevc_nvenc"), expected=VerificationSpec())
    return {"type": "plan", "plan": plan}
