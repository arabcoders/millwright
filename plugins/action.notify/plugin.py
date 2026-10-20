from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Self

from common.actions import Notify
from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Inputs(BaseModel):
    enabled: bool = Field(
        default=True,
        description="Enable notifications from this flow step. When off, this step adds no notification actions.",
    )
    targets: list[str] = Field(default=[], description="Notification target identifiers. Required when enabled.")
    on: list[Literal["job.succeeded", "job.failed", "job.cancelled"]] = Field(
        default=[], description="Job events that trigger notifications. Required when enabled."
    )

    @model_validator(mode="after")
    def validate_enabled(self) -> Self:
        if self.enabled and (not self.targets or not self.on):
            raise ValueError("Enabled notifications require at least one target and one job event.")
        return self


INPUTS = Inputs


def decide(_ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    if not inputs.enabled:
        return {"type": "actions", "actions": []}
    return {"type": "actions", "actions": [Notify(targets=inputs.targets, on=inputs.on)]}
