from __future__ import annotations

import asyncio

import pytest
from common.actions import ActionOutcome
from common.journal import append_message
from common.protocol import ActionReport
from sqlalchemy import select

from server.models import JobAttempt, JobEvent
from tests.test_hub import Env, post_action_case, wait_for
from tests.test_hub import env as env


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 2])
async def test_journal_action_results(env: Env, count: int) -> None:
    async with post_action_case(env) as case:
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        results = [ActionOutcome(kind="extract_subtitles", ok=True) for _ in range(count)]
        message = ActionReport(job_id=case.jid, attempt_id=case.offer["attempt_id"], results=results)
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            append_message(env.workspace, case.offer["log_path"], message.model_dump(mode="json"))
            await case.hub.read_journals()
            await asyncio.wait_for(dispatch, 2)
            await case.hub.read_journals()
            async with env.factory() as session:
                attempt = await session.get(JobAttempt, case.offer["attempt_id"])
                assert attempt is not None
                assert attempt.action_results == [result.model_dump(mode="json") for result in results]
                completions = list(
                    await session.scalars(
                        select(JobEvent).where(JobEvent.attempt_id == attempt.id, JobEvent.type == "actions_finished")
                    )
                )
                assert len(completions) == 1
        finally:
            if not dispatch.done():
                dispatch.cancel()
                await case.hub._handle_report(case.hub.entries["node-a"].node_db_id, message)
                await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)
