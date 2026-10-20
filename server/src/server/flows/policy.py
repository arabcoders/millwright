from __future__ import annotations

from typing import TYPE_CHECKING

from common.actions import Action, EmbedMetadata, Notify, NotifyExternal, PreserveTimestamps

if TYPE_CHECKING:
    from collections.abc import Sequence


def filter_actions(
    actions: Sequence[Action],
) -> tuple[list[Action], list[dict]]:
    """
    Dispatch file-writing actions within attempt workspaces.

    Notifications run on the server. Finalization preserves timestamps.
    Metadata requests are applied during planning, not dispatched to an executor.
    """
    dispatchable: list[Action] = []
    rejected: list[dict] = []
    for action in actions:
        if isinstance(action, (PreserveTimestamps, Notify, NotifyExternal, EmbedMetadata)):
            continue
        dispatchable.append(action)
    return dispatchable, rejected
