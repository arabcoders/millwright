from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter
from sqlalchemy import event as sql_event, select

from common.actions import Action
from server.backup import export_data
from server.flows.definition import FlowDefinition, PluginNode
from server.flows.engine import FlowContinuation
from server.flows.store import FlowVersionError, save_flow
from server.models import (
    Flow,
    Job,
    JobAttempt,
    JobEvent,
    MediaFile,
    NotificationDelivery,
    NotificationTarget,
    PluginState,
    utcnow,
)
from server.notifications import NotificationService
from server.plugins.host import PluginHost
from server.plugins.manifest import PluginManifest, load_manifest
from server.plugins.migrations import AppliedMigration, migrate_action, migrate_definition, migrate_event
from server.plugins.store_migrations import migrate_saved_plugins
from tests.helpers import api_client, create_test_app, database_context, graph_flow
from tests.test_external_notifications import notification
from tests.test_notification_delivery import make_job

PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def chain_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "id": "action.current",
            "kind": "action",
            "name": "Current action",
            "description": "Migration chain test plugin",
            "author": "Test",
            "url": "https://example.test/plugins",
            "version": "1.0.0",
            "config_version": 3,
            "migrations": [
                {
                    "from_version": 1,
                    "to_version": 2,
                    "changes": [
                        {"type": "rename_plugin", "from": "action.old", "to": "action.middle"},
                        {"type": "rename_field", "from": "server_url", "to": "url"},
                        {"type": "rename_action", "from": "old_hook", "to": "middle_hook"},
                        {"type": "rename_field", "scope": "action", "from": "old_option", "to": "option"},
                    ],
                },
                {
                    "from_version": 2,
                    "to_version": 3,
                    "changes": [
                        {"type": "rename_plugin", "from": "action.middle", "to": "action.current"},
                        {"type": "rename_field", "from": "url", "to": "endpoint"},
                        {"type": "rename_action", "from": "middle_hook", "to": "current_hook"},
                        {"type": "rename_field", "scope": "action", "from": "option", "to": "final_option"},
                    ],
                },
            ],
        }
    )


def old_notification() -> dict:
    payload = notification()
    payload["action"].update(
        kind="notify_media_manager",
        api_key="notify_media_manager media_manager_notification media-manager: refresh",
    )
    payload["path"] = "/library/notify_media_manager.mkv"
    payload["refresh"] = {"command_id": 29, "ids": [41]}
    return payload


def test_chained_migrations() -> None:
    manifest = chain_manifest()
    unrelated = {"kind": "old_hook", "type": "old_event", "server_url": "unchanged", "actions": ["old_hook"]}
    raw = graph_flow(("action.old", {"server_url": "https://example.test/old_hook", "user_data": unrelated}))
    raw["_ui"] = unrelated
    original = deepcopy(raw)
    applied: list[AppliedMigration] = []
    migrated = migrate_definition(raw, [manifest], applied=applied, record="flow example")
    node = migrated["nodes"][0]
    assert (node["plugin"], node["config_version"], node["inputs"]) == (
        "action.current",
        3,
        {"endpoint": "https://example.test/old_hook", "user_data": unrelated},
    )
    assert migrated["_ui"] == unrelated
    assert raw == original
    assert [(change.from_version, change.to_version) for change in applied] == [(1, 2), (2, 3)]
    repeated: list[AppliedMigration] = []
    assert migrate_definition(migrated, [manifest], applied=repeated) == migrated
    assert repeated == []

    action = {"kind": "old_hook", "old_option": "old_hook", "user_data": unrelated}
    original_action = deepcopy(action)
    result = migrate_action(action, [manifest])
    assert result == {
        "kind": "current_hook",
        "final_option": "old_hook",
        "user_data": unrelated,
        "plugin_id": "action.current",
        "config_version": 3,
    }
    assert action == original_action
    assert migrate_action(result, [manifest]) == result


@pytest.mark.parametrize("problem", ["conflict", "gap", "ambiguous", "unknown"])
def test_invalid_migrations_fail(problem: str) -> None:
    data = chain_manifest().model_dump(mode="json")
    raw = graph_flow(("action.old", {"server_url": "original"}))
    original = deepcopy(raw)
    if problem == "conflict":
        raw["nodes"][0]["inputs"]["url"] = "different"
        original = deepcopy(raw)
    elif problem == "gap":
        data["migrations"] = data["migrations"][:1]
    elif problem == "unknown":
        data["migrations"][0]["changes"][0]["type"] = "replace_everything"
    with pytest.raises(ValueError):
        manifest = PluginManifest.model_validate(data)
        manifests = [manifest]
        if problem == "ambiguous":
            second = {**data, "id": "action.other"}
            manifests.append(PluginManifest.model_validate(second))
        migrate_definition(raw, manifests)
    assert raw == original


def test_ambiguous_actions_fail() -> None:
    first = chain_manifest()
    second = PluginManifest.model_validate({**first.model_dump(mode="json"), "id": "action.other"})
    action = {"kind": "old_hook", "old_option": "value"}
    with pytest.raises(ValueError):
        migrate_action(action, [first, second])
    assert action == {"kind": "old_hook", "old_option": "value"}


def test_notification_event_migration() -> None:
    manifest = load_manifest(PLUGINS / "action.notify_external")
    payload = old_notification()
    original = deepcopy(payload)
    kind, migrated, targets = migrate_event("media_manager_notification", payload, ["media-manager:radarr"], [manifest])
    assert kind == "external_notification"
    assert targets == ["external:radarr"]
    assert migrated["progress"] == original["refresh"]
    assert "refresh" not in migrated
    assert migrated["action"] == {
        **original["action"],
        "kind": "notify_external",
        "plugin_id": "action.notify_external",
        "config_version": 2,
    }
    assert migrated["path"] == original["path"]
    assert payload == original
    assert migrate_event(kind, migrated, targets, [manifest]) == (kind, migrated, targets)
    unrelated = {"refresh": original["refresh"], "type": "media_manager_notification", "inputs": original}
    assert migrate_event("job.succeeded", unrelated, ["media-manager:radarr"], [manifest]) == (
        "job.succeeded",
        unrelated,
        ["media-manager:radarr"],
    )


@pytest.mark.asyncio
async def test_legacy_flow_http() -> None:
    action = old_notification()["action"]
    inputs = {key: value for key, value in action.items() if key != "kind"}
    raw = graph_flow(("action.notify_media_manager", inputs), "transcode.remux")
    raw["actions"] = [action]
    raw["_ui"] = {
        "notes": {"kind": "notify_media_manager", "type": "media_manager_notification", "refresh": "unchanged"}
    }
    host = PluginHost([PLUGINS])
    try:
        async with database_context("plugin-flow-http") as factory:
            app = create_test_app(factory, plugin_host=host)
            async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                normalized = await client.post("/api/flows/normalize", json=raw)
                assert normalized.status_code == 200, normalized.text
                canonical = normalized.json()
                assert canonical["nodes"][0]["plugin"] == "action.notify_external"
                assert canonical["nodes"][0]["config_version"] == 2
                assert canonical["nodes"][0]["inputs"] == inputs
                assert canonical["actions"] == [
                    {**action, "kind": "notify_external", "plugin_id": "action.notify_external", "config_version": 2}
                ]
                assert canonical["_ui"] == raw["_ui"]
                async with factory() as session:
                    assert list(await session.scalars(select(Flow.id))) == []
                saved = await client.put("/api/flows/legacy", json={"definition": raw})
                assert saved.status_code == 200, saved.text
                assert saved.json()["definition"] == canonical
            async with factory() as session:
                flow = (await session.scalars(select(Flow))).one()
                assert flow.name == "legacy"
                assert json.loads(flow.definition) == canonical
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_reload_migrates_flows(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="server.plugins.store_migrations")
    payload = old_notification()
    raw = graph_flow(
        ("action.notify_media_manager", {key: value for key, value in payload["action"].items() if key != "kind"}),
        "transcode.remux",
    )
    raw["actions"] = [payload["action"]]
    host = PluginHost([PLUGINS], disabled=["action.notify_media_manager"])
    try:
        async with database_context("plugin-flow-migration") as factory:
            async with factory() as session:
                session.add(Flow(name="saved", version=7, definition=json.dumps(raw)))
                session.add(PluginState(plugin_id="action.notify_media_manager", enabled=False))
                await session.commit()

            async with factory() as session:
                backup = await export_data(session)
                assert backup["flows"][0]["definition"] == raw

            async def changed() -> None:
                await migrate_saved_plugins(host, factory)

            host.on_change = changed
            await asyncio.wait_for(host.restart("action.notify_external"), 5)
            messages = [
                record.getMessage() for record in caplog.records if record.name == "server.plugins.store_migrations"
            ]
            assert any(
                "action.notify_external migration 1 -> 2 to flow 'saved' step 'n0'" in message
                and "rename_plugin: action.notify_media_manager -> action.notify_external" in message
                for message in messages
            )
            assert payload["action"]["api_key"] not in "\n".join(messages)
            async with factory() as session:
                flow = (await session.scalars(select(Flow))).one()
                assert flow.version == 8
                saved = json.loads(flow.definition)
                assert saved["nodes"][1] == raw["nodes"][1]
                assert set(saved) == set(raw)
                migrated = FlowDefinition.model_validate_json(flow.definition)
                assert isinstance(migrated.nodes[0], PluginNode)
                assert (migrated.nodes[0].plugin, migrated.nodes[0].config_version) == ("action.notify_external", 2)
                assert migrated.actions[0].kind == "notify_external"
                assert (await session.execute(select(PluginState.plugin_id, PluginState.enabled))).one() == (
                    "action.notify_external",
                    False,
                )
                with pytest.raises(FlowVersionError):
                    await save_flow(session, "saved", migrated, expected_version=7)
                await session.rollback()
            assert (
                next(info.state for info in host.plugins() if info.manifest.id == "action.notify_external")
                == "disabled"
            )
            await asyncio.wait_for(host.restart("action.notify_external"), 5)
            assert [
                record.getMessage() for record in caplog.records if record.name == "server.plugins.store_migrations"
            ] == messages
            async with factory() as session:
                flow = (await session.scalars(select(Flow))).one()
                assert flow.version == 8
                assert FlowDefinition.model_validate_json(flow.definition) == migrated
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_persisted_command_resumes(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="server.plugins.store_migrations")
    payload = old_notification()
    action = payload["action"]
    definition = graph_flow(
        ("action.notify_media_manager", {key: value for key, value in action.items() if key != "kind"})
    )
    definition["actions"] = [action]
    continuation = {"flow": definition, "node": "n0"}
    snapshot = {"name": "notify_media_manager", "definition": definition}
    user_data = {"kind": "notify_media_manager", "type": "media_manager_notification", "refresh": "unchanged"}
    previous = {
        "pre_actions": [action],
        "post_actions": [action],
        "continuation": continuation,
        "flow_snapshot": snapshot,
    }
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            assert request.url.path.endswith("/command/29")
            return httpx.Response(200, json={"id": 29, "status": "completed"})
        assert request.method == "PUT"
        assert json.loads(request.content) == {"movieIds": [41], "monitored": False}
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "server.external_notifications.arr.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    host = PluginHost([PLUGINS])
    try:
        async with database_context("plugin-job-migration") as factory:
            async with factory() as session:
                job_id = await make_job(session, notify=False)
                job = await session.get(Job, job_id)
                assert job is not None
                job.status, job.attempts = "pending", 2
                job.pre_actions = job.post_actions = [action]
                job.continuation, job.flow_snapshot = continuation, snapshot
                job.facets = user_data
                attempt = JobAttempt(
                    job_id=job_id,
                    attempt_number=2,
                    status="running",
                    action_results=[{"kind": "notify_media_manager", "ok": False, "error": "old diagnostic"}],
                )
                session.add(attempt)
                old_attempt = JobAttempt(
                    job_id=job_id,
                    attempt_number=1,
                    status="failed",
                    action_results=[{"kind": "notify_media_manager", "ok": False}],
                )
                session.add(old_attempt)
                await session.flush()
                old_stage = JobEvent(
                    job_id=job_id,
                    attempt_id=old_attempt.id,
                    type="stage",
                    payload={"actions": [action], "continuation": continuation},
                )
                old_request = JobEvent(job_id=job_id, type="requested", payload={"previous": previous})
                session.add_all([old_stage, old_request])
                await session.flush()
                event = JobEvent(
                    job_id=job_id,
                    attempt_id=attempt.id,
                    type="media_manager_notification",
                    payload={**payload, "progress": {"command_id": 99, "ids": [99]}},
                )
                stage = JobEvent(
                    job_id=job_id,
                    attempt_id=attempt.id,
                    type="stage",
                    payload={
                        "actions": [action],
                        "publication_actions": [action],
                        "post_actions": [action],
                        "action_results": [{"kind": "notify_media_manager", "ok": True}],
                        "continuation": continuation,
                    },
                )
                retry = JobEvent(
                    job_id=job_id,
                    type="requested",
                    payload={"operation": "retry", "previous": previous, "flow_snapshot": snapshot},
                )
                unrelated = JobEvent(job_id=job_id, type="job.succeeded", payload={"refresh": user_data})
                session.add_all([event, stage, retry, unrelated])
                await session.flush()
                delivery = NotificationDelivery(
                    job_id=job_id,
                    job_event_id=event.id,
                    target_name="media-manager:radarr",
                    status="pending",
                    attempts=1,
                    error="command interrupted",
                )
                other = NotificationDelivery(
                    job_id=job_id,
                    job_event_id=unrelated.id,
                    target_name="media-manager:radarr",
                    status="sent",
                    attempts=1,
                )
                session.add_all(
                    [delivery, other, NotificationTarget(name="media-manager:radarr", url="json://example.test")]
                )
                await session.commit()
                event_id, stage_id, retry_id = event.id, stage.id, retry.id
                delivery_id, other_id, attempt_id = delivery.id, other.id, attempt.id
                old_attempt_id, old_stage_id, old_request_id = old_attempt.id, old_stage.id, old_request.id

            with pytest.raises(ValueError, match="conflicting"):
                await migrate_saved_plugins(host, factory)
            assert not [record for record in caplog.records if record.name == "server.plugins.store_migrations"]
            async with factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None and job.pre_actions == job.post_actions == [action]
                assert (job.status, job.attempts) == ("pending", 2)
                delivery = await session.get(NotificationDelivery, delivery_id)
                assert delivery is not None and (delivery.target_name, delivery.status, delivery.attempts) == (
                    "media-manager:radarr",
                    "pending",
                    1,
                )
                event = await session.get(JobEvent, event_id)
                assert event is not None and event.type == "media_manager_notification"
                assert event.payload == {**payload, "progress": {"command_id": 99, "ids": [99]}}
                event.payload = payload
                await session.commit()
            await migrate_saved_plugins(host, factory)
            messages = [
                record.getMessage() for record in caplog.records if record.name == "server.plugins.store_migrations"
            ]
            assert any(
                f"job {job_id} event {event_id}" in message
                and "action.notify_external migration 1 -> 2" in message
                and "rename_field (event): refresh -> progress" in message
                and "rename_target_prefix: media-manager:radarr -> external:radarr" in message
                for message in messages
            )
            assert action["api_key"] not in "\n".join(messages)
            async with factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None and (job.status, job.attempts, job.facets) == ("pending", 2, user_data)
                assert job.pre_actions is not None and job.post_actions is not None
                TypeAdapter(list[Action]).validate_python(job.pre_actions + job.post_actions)
                assert all(item["kind"] == "notify_external" for item in job.pre_actions + job.post_actions)
                assert FlowContinuation.model_validate(job.continuation).flow.actions[0].kind == "notify_external"
                assert (
                    job.flow_snapshot is not None
                    and job.flow_snapshot["definition"]["actions"][0]["kind"] == "notify_external"
                )
                migrated_event = await session.get(JobEvent, event_id)
                assert migrated_event is not None and migrated_event.type == "external_notification"
                assert migrated_event.payload["progress"] == payload["refresh"]
                assert migrated_event.payload["action"]["api_key"] == action["api_key"]
                migrated_stage = await session.get(JobEvent, stage_id)
                assert migrated_stage is not None
                assert migrated_stage.payload["actions"][0]["kind"] == "notify_external"
                assert migrated_stage.payload["publication_actions"][0]["kind"] == "notify_external"
                assert migrated_stage.payload["post_actions"][0]["kind"] == "notify_external"
                assert migrated_stage.payload["action_results"] == [{"kind": "notify_external", "ok": True}]
                migrated_retry = await session.get(JobEvent, retry_id)
                assert (
                    migrated_retry is not None
                    and migrated_retry.payload["previous"]["pre_actions"][0]["kind"] == "notify_external"
                )
                assert (
                    migrated_retry.payload["flow_snapshot"]["definition"]["nodes"][0]["plugin"]
                    == "action.notify_external"
                )
                attempt = await session.get(JobAttempt, attempt_id)
                assert attempt is not None and attempt.status == "running" and attempt.action_results is not None
                assert attempt.action_results[0]["kind"] == "notify_external"
                assert attempt.action_results[0]["error"] == "old diagnostic"
                old_attempt = await session.get(JobAttempt, old_attempt_id)
                assert old_attempt is not None
                assert old_attempt.action_results == [{"kind": "notify_media_manager", "ok": False}]
                old_stage = await session.get(JobEvent, old_stage_id)
                assert old_stage is not None and old_stage.payload == {
                    "actions": [action],
                    "continuation": continuation,
                }
                old_request = await session.get(JobEvent, old_request_id)
                assert old_request is not None and old_request.payload == {"previous": previous}
                delivery = await session.get(NotificationDelivery, delivery_id)
                assert delivery is not None
                assert (delivery.target_name, delivery.status, delivery.attempts, delivery.error) == (
                    "external:radarr",
                    "pending",
                    1,
                    "command interrupted",
                )
                other = await session.get(NotificationDelivery, other_id)
                assert other is not None and (other.target_name, other.status, other.attempts) == (
                    "media-manager:radarr",
                    "sent",
                    1,
                )
                assert (await session.scalars(select(NotificationTarget.name))).one() == "media-manager:radarr"
            await migrate_saved_plugins(host, factory)
            assert [
                record.getMessage() for record in caplog.records if record.name == "server.plugins.store_migrations"
            ] == messages
            assert await NotificationService(factory).deliver_pending() == 1
            assert await NotificationService(factory).deliver_pending() == 0
            async with factory() as session:
                delivery = await session.get(NotificationDelivery, delivery_id)
                assert delivery is not None and (delivery.status, delivery.attempts) == ("sent", 2)
                job = await session.get(Job, job_id)
                assert job is not None and (job.status, job.attempts) == ("pending", 2)
            assert requests == [("GET", "/base/api/v3/command/29"), ("PUT", "/base/api/v3/movie/editor")]
    finally:
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", ["none", "notification", "post_actions", "deferred_actions"])
async def test_finished_work_scope(remaining: str) -> None:
    payload = old_notification()
    action = payload["action"]
    definition = graph_flow(
        ("action.notify_media_manager", {key: value for key, value in action.items() if key != "kind"})
    )
    snapshot = {"definition": definition}
    stage_payload = {
        "post_actions": [action],
        "publication_actions": [action],
        "action_results": [{"kind": "notify_media_manager", "ok": True}],
        "continuation": {"flow": definition, "node": "n0"},
    }
    sent_payload = {**payload, "progress": {"command_id": 99}}
    host = PluginHost([PLUGINS])
    try:
        async with database_context("plugin-finished-migration") as factory:
            async with factory() as session:
                job_id = await make_job(session, notify=False)
                job = await session.get(Job, job_id)
                assert job is not None
                job.status, job.attempts = "succeeded", 2
                job.pre_actions = job.post_actions = [action]
                job.flow_snapshot = snapshot
                job.continuation = stage_payload["continuation"]
                attempt = JobAttempt(
                    job_id=job_id,
                    attempt_number=2,
                    status="succeeded",
                    action_results=stage_payload["action_results"],
                )
                session.add(attempt)
                await session.flush()
                stage = JobEvent(job_id=job_id, attempt_id=attempt.id, type="stage", payload=stage_payload)
                actions = JobEvent(
                    job_id=job_id,
                    attempt_id=attempt.id,
                    type="actions_pending" if remaining == "post_actions" else "actions_finished",
                    payload={"actions": [action]},
                )
                sent = JobEvent(
                    job_id=job_id, attempt_id=attempt.id, type="media_manager_notification", payload=sent_payload
                )
                session.add_all([stage, sent])
                if remaining != "deferred_actions":
                    session.add(actions)
                await session.flush()
                sent_delivery = NotificationDelivery(
                    job_id=job_id, job_event_id=sent.id, target_name="media-manager:radarr", status="sent", attempts=1
                )
                session.add(sent_delivery)
                pending_id = None
                if remaining == "notification":
                    pending = JobEvent(
                        job_id=job_id, attempt_id=attempt.id, type="media_manager_notification", payload=payload
                    )
                    session.add(pending)
                    await session.flush()
                    pending_id = pending.id
                    session.add(
                        NotificationDelivery(
                            job_id=job_id, job_event_id=pending.id, target_name="media-manager:radarr", status="pending"
                        )
                    )
                await session.commit()
                attempt_id, stage_id, actions_id, sent_id = attempt.id, stage.id, actions.id, sent.id
                delivery_id, updated_at = sent_delivery.id, job.updated_at

            async with factory() as locked:
                if remaining == "none":
                    # A completed job must not even be locked or loaded by the migration pass.
                    await locked.scalar(select(Job).where(Job.id == job_id).with_for_update())
                await asyncio.wait_for(migrate_saved_plugins(host, factory), 5)

            async with factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                assert (job.status, job.attempts, job.pre_actions, job.flow_snapshot, job.continuation) == (
                    "succeeded",
                    2,
                    [action],
                    snapshot,
                    stage_payload["continuation"],
                )
                if remaining == "deferred_actions":
                    assert job.post_actions is not None and job.post_actions[0]["kind"] == "notify_external"
                else:
                    assert job.post_actions == [action] and job.updated_at == updated_at
                attempt = await session.get(JobAttempt, attempt_id)
                assert attempt is not None and attempt.action_results == stage_payload["action_results"]
                stage = await session.get(JobEvent, stage_id)
                assert stage is not None
                if remaining == "deferred_actions":
                    assert stage.payload["post_actions"][0]["kind"] == "notify_external"
                    assert {key: value for key, value in stage.payload.items() if key != "post_actions"} == {
                        key: value for key, value in stage_payload.items() if key != "post_actions"
                    }
                else:
                    assert stage.payload == stage_payload
                if remaining == "post_actions":
                    actions = await session.get(JobEvent, actions_id)
                    assert actions is not None and actions.payload["actions"][0]["kind"] == "notify_external"
                sent = await session.get(JobEvent, sent_id)
                assert sent is not None and (sent.type, sent.payload) == ("media_manager_notification", sent_payload)
                sent_delivery = await session.get(NotificationDelivery, delivery_id)
                assert sent_delivery is not None and (sent_delivery.target_name, sent_delivery.status) == (
                    "media-manager:radarr",
                    "sent",
                )
                if pending_id is not None:
                    pending = await session.get(JobEvent, pending_id)
                    assert pending is not None and pending.type == "external_notification"
                    assert pending.payload["action"]["kind"] == "notify_external"
                    assert pending.payload["progress"] == payload["refresh"]
                    delivery = await session.scalar(
                        select(NotificationDelivery).where(NotificationDelivery.job_event_id == pending_id)
                    )
                    assert delivery is not None and (delivery.target_name, delivery.status) == (
                        "external:radarr",
                        "pending",
                    )
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_retry_migrates_execution() -> None:
    action = old_notification()["action"]
    host = PluginHost([PLUGINS])
    try:
        async with database_context("plugin-retry-migration") as factory:
            async with factory() as session:
                job_id = await make_job(session, notify=False)
                job = await session.get(Job, job_id)
                assert job is not None
                job.status = "failed"
                job.pre_actions = job.post_actions = [action]
                history = JobEvent(job_id=job_id, type="stage", payload={"actions": [action]})
                session.add(history)
                await session.commit()
                history_id = history.id
            app = create_test_app(factory, plugin_host=host)
            result = await app.state.jobs.retry([job_id])
            assert result == {"accepted": [job_id], "failed": []}
            async with factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None and job.status == "pending"
                assert job.pre_actions is not None and job.post_actions is not None
                parsed = TypeAdapter(list[Action]).validate_python(job.pre_actions + job.post_actions)
                assert all(item.kind == "notify_external" for item in parsed)
                history = await session.get(JobEvent, history_id)
                assert history is not None and history.payload == {"actions": [action]}
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_migrations_read_batches(tmp_path: Path) -> None:
    action = old_notification()["action"]
    host = PluginHost([PLUGINS])
    try:
        async with database_context("plugin-migration-batches") as factory:
            async with factory() as session:
                first_id = await make_job(session, notify=False)
                first = await session.get(Job, first_id)
                assert first is not None
                first.post_actions = [action]
                media = [
                    MediaFile(
                        library_id=first.library_id,
                        path=str(tmp_path / f"media-{index}.mkv"),
                        size=100,
                        mtime_ns=0,
                        container="mkv",
                        medium="movie",
                        last_scanned_at=utcnow(),
                    )
                    for index in range(100)
                ]
                session.add_all(media)
                await session.flush()
                session.add_all(
                    Job(
                        library_id=first.library_id,
                        media_file_id=item.id,
                        status="queued",
                        post_actions=[action],
                    )
                    for item in media
                )
                await session.commit()
            statements = 0
            engine = factory.kw["bind"].sync_engine

            def count(*_args) -> None:
                nonlocal statements
                statements += 1

            sql_event.listen(engine, "before_cursor_execute", count)
            try:
                await migrate_saved_plugins(host, factory)
            finally:
                sql_event.remove(engine, "before_cursor_execute", count)
            # The production stall came from several database round trips per job.
            assert statements < 30
            async with factory() as session:
                jobs = list(await session.scalars(select(Job).order_by(Job.id)))
                assert len(jobs) == 101
                assert all(job.post_actions and job.post_actions[0]["kind"] == "notify_external" for job in jobs)
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_unrelated_records_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="server.plugins.store_migrations")
    raw = graph_flow("transcode.remux")
    action = {"kind": "preserve_timestamps", "plugin_id": "action.keep_dates"}
    raw["actions"] = [action]
    raw["_ui"] = {"notes": "action.notify_media_manager"}
    serialized = json.dumps(raw, indent=2)
    continuation = {"flow": raw, "node": "n0"}
    snapshot = {"name": "unrelated", "definition": raw}
    payload = {"continuation": continuation, "publication_actions": [action]}
    host = PluginHost([PLUGINS])
    try:
        async with database_context("plugin-noop-migration") as factory:
            async with factory() as session:
                flow = Flow(name="unrelated", version=4, definition=serialized)
                session.add(flow)
                job_id = await make_job(session, notify=False)
                job = await session.get(Job, job_id)
                assert job is not None
                job.pre_actions = job.post_actions = [action]
                job.continuation = continuation
                job.flow_snapshot = snapshot
                stage = JobEvent(job_id=job_id, type="stage", payload=payload)
                session.add(stage)
                await session.commit()
                flow_id, stage_id = flow.id, stage.id
                flow_updated, job_updated = flow.updated_at, job.updated_at

            for _ in range(2):
                await migrate_saved_plugins(host, factory)
                async with factory() as session:
                    flow = await session.get(Flow, flow_id)
                    assert flow is not None
                    assert (flow.definition, flow.version, flow.updated_at) == (serialized, 4, flow_updated)
                    job = await session.get(Job, job_id)
                    assert job is not None
                    assert job.pre_actions == job.post_actions == [action]
                    assert job.continuation == continuation and job.flow_snapshot == snapshot
                    assert job.updated_at == job_updated
                    stage = await session.get(JobEvent, stage_id)
                    assert stage is not None and stage.payload == payload
            assert not [record for record in caplog.records if record.name == "server.plugins.store_migrations"]
    finally:
        await host.aclose()
