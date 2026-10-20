from __future__ import annotations

from tests.helpers import create_user_session

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from joserfc import jwk
from joserfc import jwt as jose_jwt
from joserfc.errors import JoseError
from server.api.app import create_app
from server.api.auth import AuthStack, OidcError, auth_router, _commit_and_revoke
from server.core.auth import (
    AuthCapacityError,
    PendingStates,
    Principal,
    WsTickets,
    authenticate_session,
    revoke_session,
    hash_password,
)
from server.core.config import AuthConfig, OidcConfig, ServerConfig
from server.core.db import create_engine_and_session
from server.models import SessionRow, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.helpers import database_context

ISSUER = "https://sso.example.test/realms/main"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/jwks"
REDIRECT_URI = "http://ui.example.test/callback"
UI_BASE = "http://testserver"


@pytest_asyncio.fixture
async def factory():
    async with database_context("auth", tables=("users", "sessions", "key_values")) as factory:
        async with factory() as db:
            db.add(User(username="operator", password_hash=hash_password("old-password")))
            await db.commit()
        yield factory


def make_config(**overrides) -> ServerConfig:
    oidc = OidcConfig(
        issuer=ISSUER,
        client_id="transcode",
        secret="shh",
        redirect_uri=REDIRECT_URI,
        username="operator",
    )
    return ServerConfig(auth=AuthConfig(oidc=oidc), **overrides)


@dataclass(kw_only=True)
class Provider:
    http: httpx.AsyncClient
    captured: dict


def make_provider(
    *,
    sign_key: jwk.RSAKey | jwk.OctKey | None = None,
    jwks_key: jwk.RSAKey | None = None,
    alg: str = "RS256",
    mutate: dict | None = None,
    failure: tuple[str, int] | None = None,
) -> Provider:
    key = jwk.RSAKey.generate_key(2048)
    captured: dict = {
        "nonce": None,
        "sign_key": sign_key if sign_key is not None else key,
        "jwks_key": jwks_key if jwks_key is not None else key,
        "alg": alg,
        "mutate": mutate or {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if failure is not None and url == failure[0]:
            return httpx.Response(failure[1], text="private provider response")
        if url == DISCOVERY:
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/auth",
                    "token_endpoint": TOKEN_ENDPOINT,
                    "jwks_uri": JWKS_URI,
                },
            )
        if url == TOKEN_ENDPOINT:
            claims = {
                "iss": ISSUER,
                "aud": "transcode",
                "sub": "user-1",
                "iat": 0,
                "exp": 4102444800,
                "nonce": captured["nonce"],
                "preferred_username": "sample-user",
                "email": "user@example.test",
            }
            claims.update(captured["mutate"])
            id_token = jose_jwt.encode({"alg": captured["alg"]}, claims, captured["sign_key"])
            return httpx.Response(200, json={"id_token": id_token})
        if url == JWKS_URI:
            public = captured["jwks_key"].as_dict(private=False)
            return httpx.Response(200, json={"keys": [public]})
        return httpx.Response(404, json={"error": f"unexpected provider request {url}"})

    return Provider(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), captured=captured)


async def login(ui: httpx.AsyncClient) -> dict[str, str]:
    resp = await ui.get("/api/auth/oidc/login")
    assert resp.status_code == 200
    location = resp.json()["authorize_url"]
    assert location.startswith(f"{ISSUER}/auth?")
    query = {key: values[0] for key, values in parse_qs(urlparse(location).query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == "transcode"
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["scope"] == "openid profile email"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"]
    return query


def make_ui(app, base_url: str = UI_BASE) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=base_url, headers={"X-Millwright-Request": "1"}
    )


@pytest.mark.asyncio
async def test_session_roundtrip(factory) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    async with factory() as db:
        row, token = await create_user_session(db, "user-1", 7, now=t0)
        await db.commit()
        assert token.startswith("mw_")
        assert row.token_hash != token

        found = await authenticate_session(db, token, now=t0)
        assert found is not None and found.user_id == row.user_id
        assert await authenticate_session(db, "mw_bogus", now=t0) is None
        assert await authenticate_session(db, token, now=t0 + timedelta(days=6)) is not None
        assert await authenticate_session(db, token, now=t0 + timedelta(days=8)) is None

        assert await revoke_session(db, token) is True
        assert await revoke_session(db, token) is False
        assert await authenticate_session(db, token, now=t0) is None


def test_ws_ticket_single(monkeypatch) -> None:
    tickets = WsTickets(ttl_s=30.0)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("server.core.auth.utcnow", lambda: t0)
    principal = Principal(user_id=1, username="operator", session_hash="hash-1", session_expires_at=t0)
    ticket = tickets.issue(principal)
    assert ticket.startswith("wst_")
    assert tickets.consume(ticket, now=t0 + timedelta(seconds=10)) == principal
    assert tickets.consume(ticket, now=t0 + timedelta(seconds=10)) is None
    second = tickets.issue(principal)
    assert tickets.consume(second, now=t0 + timedelta(seconds=30)) is None


def test_pending_state_single(monkeypatch) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("server.core.auth.utcnow", lambda: t0)
    pending = PendingStates()
    pending.create("state-1", "verifier", "nonce")
    assert pending.consume("state-1") == ("verifier", "nonce")
    assert pending.consume("state-1") is None
    pending.create("state-2", "verifier-2", "nonce-2")
    assert pending.consume("state-2", now=t0 + timedelta(seconds=300)) is None


def test_pending_capacity_recovers(monkeypatch) -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now[0])
    pending = PendingStates(ttl_s=30, capacity=2)
    pending.create("first", "verifier-1", "nonce-1")
    now[0] += timedelta(seconds=10)
    pending.create("second", "verifier-2", "nonce-2")
    with pytest.raises(AuthCapacityError):
        pending.create("rejected", "verifier-3", "nonce-3")
    assert set(pending._states) == {"first", "second"}

    now[0] += timedelta(seconds=20)
    pending.create("third", "verifier-3", "nonce-3")
    assert set(pending._states) == {"second", "third"}
    assert pending.consume("first") is None
    assert pending.consume("second") == ("verifier-2", "nonce-2")
    assert pending.consume("second") is None
    pending.create("fourth", "verifier-4", "nonce-4")
    assert pending.consume("third") == ("verifier-3", "nonce-3")
    assert pending.consume("fourth") == ("verifier-4", "nonce-4")


def test_ticket_capacity_recovers(monkeypatch) -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now[0])
    tickets = WsTickets(ttl_s=30, capacity=2)
    principal = Principal(user_id=1, username="operator")
    first = tickets.issue(principal)
    now[0] += timedelta(seconds=10)
    second = tickets.issue(principal)
    with pytest.raises(AuthCapacityError):
        tickets.issue(principal)
    assert set(tickets._tickets) == {first, second}

    now[0] += timedelta(seconds=20)
    third = tickets.issue(principal)
    assert set(tickets._tickets) == {second, third}
    assert tickets.consume(first) is None
    assert tickets.consume(second) == principal
    assert tickets.consume(second) is None
    fourth = tickets.issue(principal)
    assert tickets.consume(third) == principal
    assert tickets.consume(fourth) == principal


@pytest.mark.parametrize("explicit_now", [False, True])
def test_transient_purge(monkeypatch, explicit_now) -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now[0])
    pending = PendingStates(ttl_s=30)
    tickets = WsTickets(ttl_s=30)
    principal = Principal(user_id=1, username="operator")
    pending.create("expired", "verifier-1", "nonce-1")
    expired = tickets.issue(principal)
    now[0] += timedelta(seconds=1)
    pending.create("valid", "verifier-2", "nonce-2")
    valid = tickets.issue(principal)
    checked = now[0] + timedelta(seconds=29)
    if explicit_now:
        pending.purge(checked)
        tickets.purge(checked)
    else:
        now[0] = checked
        pending.purge()
        tickets.purge()
    assert set(pending._states) == {"valid"}
    assert set(tickets._tickets) == {valid}
    assert pending.consume("expired", now=checked) is None
    assert tickets.consume(expired, now=checked) is None
    assert pending.consume("valid", now=checked) == ("verifier-2", "nonce-2")
    assert tickets.consume(valid, now=checked) == principal


@pytest.mark.asyncio
async def test_auth_idle_cleanup(factory, monkeypatch) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    now = [t0 - timedelta(seconds=30)]
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now[0])
    monkeypatch.setattr("server.api.auth.utcnow", lambda: now[0])
    stack = AuthStack(
        config=ServerConfig(), sessionmaker=factory, pending=PendingStates(ttl_s=30), ws_tickets=WsTickets()
    )
    async with factory() as db:
        expired, expired_token = await create_user_session(db, "expired", 1, now=t0 - timedelta(days=2))
        boundary, boundary_token = await create_user_session(db, "boundary", 1, now=t0 - timedelta(days=1))
        active, active_token = await create_user_session(db, "active", 1, now=t0)
        await db.commit()

    principal = Principal(
        user_id=active.user_id,
        username="active",
        session_hash=active.token_hash,
        session_expires_at=active.expires_at,
    )
    watchers = {row.token_hash: stack.ws_tickets.watch_session(row.token_hash) for row in (expired, boundary, active)}
    try:
        stack.pending.create("expired", "verifier-1", "nonce-1")
        stack.ws_tickets.issue(principal)
        now[0] += timedelta(seconds=1)
        stack.pending.create("valid", "verifier-2", "nonce-2")
        valid_ticket = stack.ws_tickets.issue(principal)
        now[0] = t0
        await stack.cleanup()
        await stack.cleanup()
        assert set(stack.pending._states) == {"valid"}
        assert set(stack.ws_tickets._tickets) == {valid_ticket}
        assert set(stack.ws_tickets._sessions) == set(watchers)
        assert not any(event.is_set() for event in watchers.values())
        async with factory() as db:
            assert list((await db.scalars(select(SessionRow.id))).all()) == [active.id]
            assert await authenticate_session(db, expired_token) is None
            assert await authenticate_session(db, boundary_token) is None
            found = await authenticate_session(db, active_token)
            assert found is not None
            assert found.user_id == active.user_id
        assert stack.pending.consume("valid") == ("verifier-2", "nonce-2")
        assert stack.ws_tickets.consume(valid_ticket) == principal
        stack.ws_tickets.revoke_session(active.token_hash)
        assert watchers[active.token_hash].is_set()
        assert not watchers[expired.token_hash].is_set()
        assert not watchers[boundary.token_hash].is_set()
    finally:
        for token_hash, event in watchers.items():
            stack.ws_tickets.unwatch_session(token_hash, event)


@pytest.mark.asyncio
async def test_login_capacity_recovers(factory, monkeypatch) -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now[0])
    provider = make_provider()
    app = create_app(factory, config=make_config(), http_client=provider.http)
    app.state.auth.pending = PendingStates(capacity=2)
    async with make_ui(app) as ui, provider.http:
        first = await login(ui)
        now[0] += timedelta(seconds=1)
        second = await login(ui)
        rejected = await ui.get("/api/auth/oidc/login")
        assert rejected.status_code == 429
        assert "set-cookie" not in rejected.headers
        assert set(app.state.auth.pending._states) == {first["state"], second["state"]}

        now[0] += timedelta(seconds=299)
        third = await login(ui)
        assert set(app.state.auth.pending._states) == {second["state"], third["state"]}
        assert app.state.auth.pending.consume(first["state"]) is None
        second_pending = app.state.auth.pending.consume(second["state"])
        assert second_pending is not None and second_pending[1] == second["nonce"]
        provider.captured["nonce"] = third["nonce"]
        response = await ui.get("/api/auth/oidc/callback", params={"code": "abc", "state": third["state"]})
        assert response.status_code == 303


@pytest.mark.asyncio
async def test_ticket_api_capacity(factory, monkeypatch) -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now[0])
    app = create_app(factory, config=ServerConfig(api_key="test-key"))
    app.state.auth.ws_tickets = WsTickets(capacity=1)
    async with make_ui(app) as ui:
        ui.headers["Authorization"] = "Bearer test-key"
        first = await ui.post("/api/auth/ws-ticket")
        assert first.status_code == 200
        ticket = first.json()["ticket"]
        rejected = await ui.post("/api/auth/ws-ticket")
        assert rejected.status_code == 429
        principal = app.state.auth.ws_tickets.consume(ticket)
        assert principal is not None and principal.user_id is None
        second = await ui.post("/api/auth/ws-ticket")
        assert second.status_code == 200
        now[0] += timedelta(seconds=30)
        third = await ui.post("/api/auth/ws-ticket")
        assert third.status_code == 200
        assert set(app.state.auth.ws_tickets._tickets) == {third.json()["ticket"]}


@pytest.mark.asyncio
async def test_oidc_happy_path(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now)
    provider = make_provider()
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        response = await ui.get("/api/auth/oidc/login")
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "oidc_state=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
        query = {key: values[0] for key, values in parse_qs(urlparse(response.json()["authorize_url"]).query).items()}
        provider.captured["nonce"] = query["nonce"]
        resp = await ui.get("/api/auth/oidc/callback", params={"code": "abc", "state": query["state"]})
        assert resp.status_code == 303 and resp.headers["location"] == "/"
        assert "oidc_state" not in ui.cookies
        token = ui.cookies["mw_session"]
        assert "HttpOnly" in resp.headers["set-cookie"] and "SameSite=lax" in resp.headers["set-cookie"]
        me = (await ui.get("/api/auth/me")).json()
        assert me["username"] == "operator" and me["auth_method"] == "session"
        assert (await ui.patch("/api/settings", json={"session_ttl_days": 1})).status_code == 200
        following = await callback_after_login(ui, provider)
        assert following.status_code == 303
        async with factory() as db:
            prior = await authenticate_session(db, token)
            current = await authenticate_session(db, ui.cookies["mw_session"])
            assert prior is not None and prior.expires_at == now + timedelta(days=7)
            assert current is not None and current.expires_at == now + timedelta(days=1)
            assert current.user_id == prior.user_id == me["id"]
        ticket = (await ui.post("/api/auth/ws-ticket")).json()
        assert ticket["ticket"].startswith("wst_")
        logout = await ui.post("/api/auth/logout")
        assert logout.status_code == 204 and "set-cookie" not in logout.headers
        assert (await ui.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_oidc_missing_username(factory) -> None:
    provider = make_provider(mutate={"preferred_username": None, "email": "unrelated@example.test"})
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        response = await callback_after_login(ui, provider)
        assert response.status_code == 303
        me = (await ui.get("/api/auth/me")).json()
        assert me["username"] == "operator" and me["auth_method"] == "session"
        async with factory() as db:
            assert list(await db.scalars(select(User.username))) == ["operator"]


@pytest.mark.asyncio
async def test_oidc_missing_mapping(factory) -> None:
    def unexpected_request(request):
        raise AssertionError(f"Unexpected provider request: {request.url}")

    config = make_config()
    assert config.auth.oidc is not None
    config.auth.oidc.username = "missing"
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as provider:
        app = create_app(factory, config=config, http_client=provider)
        async with make_ui(app) as ui:
            status = await ui.get("/api/auth/status")
            assert status.status_code == 200
            assert status.json()["oidc_available"] is False and status.json()["setup_required"] is False
            response = await ui.get("/api/auth/oidc/login")
            assert response.status_code == 404
            assert "'missing' was not found" in response.json()["error"]
            assert "auth.oidc.username" in response.json()["error"]
            config.auth.oidc.username = None
            response = await ui.get("/api/auth/oidc/login")
            assert response.status_code == 404
            assert response.json()["error"] == "Set auth.oidc.username to an existing Millwright username."
            config.auth.oidc.username = "operator"
            assert (await ui.get("/api/auth/status")).json()["oidc_available"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "status"),
    [
        (TOKEN_ENDPOINT, 400),
        (JWKS_URI, 503),
    ],
)
async def test_oidc_provider_status(factory, caplog, endpoint, status) -> None:
    provider = make_provider(failure=(endpoint, status))
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        response = await callback_after_login(ui, provider)
    assert response.status_code == 400
    messages = [
        record.getMessage() + getattr(record, "error", "")
        for record in caplog.records
        if record.name == "server.api.auth"
    ]
    assert len(messages) == 1
    assert "private provider response" not in response.text + "".join(messages)


@pytest.mark.asyncio
async def test_oidc_callback_redacts(factory, monkeypatch, caplog) -> None:
    provider = make_provider()
    app = create_app(factory, config=make_config(), http_client=provider.http)
    code = "private/code+value"
    captured: list[str] = []

    async def rejected(received_code: str, verifier: str, nonce: str):
        captured.extend((received_code, verifier, nonce, app.state.auth.oidc.config.secret))
        raise OidcError("callback rejected " + " ".join(captured + [quote(code, safe="")]))

    monkeypatch.setattr(app.state.auth.oidc, "finish", rejected)
    async with make_ui(app) as ui, provider.http:
        response = await callback_after_login(ui, provider, code=code)
    assert response.status_code == 400 and "callback rejected" in response.json()["error"]
    messages = [
        record.getMessage() + getattr(record, "error", "")
        for record in caplog.records
        if record.name == "server.api.auth"
    ]
    assert len(messages) == 1 and "[redacted]" in messages[0]
    assert all(value not in response.text + messages[0] for value in captured + [quote(code, safe="")])


@pytest.mark.asyncio
async def test_oidc_token_redacts(factory, monkeypatch, caplog) -> None:
    provider = make_provider()
    app = create_app(factory, config=make_config(), http_client=provider.http)
    captured: list[str] = []

    def rejected(id_token: str, *_args, **_kwargs):
        captured.append(id_token)
        raise JoseError(f"token rejected {id_token} {quote(id_token, safe='')}")

    monkeypatch.setattr("server.api.auth.jwt.decode", rejected)
    async with make_ui(app) as ui, provider.http:
        response = await callback_after_login(ui, provider)
    assert response.status_code == 400
    messages = [
        record.getMessage() + getattr(record, "error", "")
        for record in caplog.records
        if record.name == "server.api.auth"
    ]
    assert len(messages) == 1 and "[redacted]" in messages[0]
    assert captured and all(value not in response.text + messages[0] for value in captured)


@pytest.mark.asyncio
async def test_post_login_handoff(factory) -> None:
    provider = make_provider()
    config = make_config()
    assert config.auth.oidc is not None
    config.auth.oidc.post_login_url = "https://transcode.example.com/"
    app = create_app(factory, config=config, http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        resp = await callback_after_login(ui, provider)
        assert resp.status_code == 303
        assert resp.headers["location"] == "https://transcode.example.com/"
        assert "mw_session=" in resp.headers["set-cookie"]
        me = await ui.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["username"] == "operator"


async def callback_after_login(ui: httpx.AsyncClient, provider: Provider, **params) -> httpx.Response:
    query = await login(ui)
    if "nonce" not in params:
        provider.captured["nonce"] = query["nonce"]
    merged = {"code": "abc", "state": query["state"], **params}
    return await ui.get("/api/auth/oidc/callback", params=merged)


@pytest.mark.asyncio
async def test_tampered_signature_rejected(factory) -> None:
    forged = jwk.RSAKey.generate_key(2048)
    provider = make_provider(sign_key=forged)
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        resp = await callback_after_login(ui, provider)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_hs256_token_rejected(factory) -> None:
    provider = make_provider(sign_key=jwk.OctKey.generate_key(), alg="HS256")
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        resp = await callback_after_login(ui, provider)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_nonce_mismatch_rejected(factory) -> None:
    provider = make_provider(mutate={"nonce": "bogus"})
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        resp = await callback_after_login(ui, provider)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_state_mismatch_rejected(factory) -> None:
    provider = make_provider()
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        resp = await callback_after_login(ui, provider, state="forged-state")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_replayed_callback_rejected(factory) -> None:
    provider = make_provider()
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        query = await login(ui)
        provider.captured["nonce"] = query["nonce"]
        first = await ui.get("/api/auth/oidc/callback", params={"code": "abc", "state": query["state"]})
        replay = await ui.get("/api/auth/oidc/callback", params={"code": "abc", "state": query["state"]})
    assert first.status_code == 303
    assert replay.status_code == 400


@pytest.mark.asyncio
async def test_missing_code_rejected(factory) -> None:
    provider = make_provider()
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        query = await login(ui)
        resp = await ui.get("/api/auth/oidc/callback", params={"state": query["state"]})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_authentication_required(factory) -> None:
    app = create_app(factory)
    async with make_ui(app) as ui:
        assert (await ui.get("/api/auth/me")).status_code == 401
        assert (await ui.get("/api/system")).status_code == 401
        assert (await ui.post("/api/auth/logout")).status_code == 401
        assert (await ui.get("/api/auth/oidc/login")).status_code == 404


@pytest.mark.asyncio
async def test_api_key_with_oidc(factory) -> None:
    app = create_app(factory, config=make_config(api_key="server-cli-key", node_enrollment_token="node-key"))
    async with make_ui(app) as ui:
        assert (await ui.get("/api/system")).status_code == 401
        assert (await ui.get("/api/system", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await ui.get("/api/system", headers={"Authorization": "Bearer node-key"})).status_code == 401
        headers = {"Authorization": "Bearer server-cli-key"}
        assert (await ui.get("/api/system", headers=headers)).status_code == 200
        assert (await ui.post("/api/auth/logout", headers=headers)).status_code == 400
        assert (await ui.get("/api/system", headers=headers)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", ["transcode", ["transcode"]])
async def test_oidc_wrong_party(factory, audience) -> None:
    provider = make_provider(mutate={"aud": audience, "azp": "another-client"})
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        response = await callback_after_login(ui, provider)
    assert response.status_code == 400
    async with factory() as db:
        assert (await db.scalars(select(SessionRow))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims,status",
    [
        ({"iss": "https://another.example.test"}, 400),
        ({"aud": "another-client"}, 400),
        ({"aud": ["transcode", "another-client"]}, 400),
        ({"aud": ["transcode", "another-client"], "azp": "another-client"}, 400),
        ({"sub": None}, 400),
        ({"sub": ""}, 400),
        ({"sub": 123}, 400),
        ({"exp": None}, 400),
        ({"iat": None}, 400),
        ({"iat": 1_800_000_001}, 400),
        ({"nbf": 1_800_000_001}, 400),
        ({"aud": ["transcode", "another-client"], "azp": "transcode"}, 303),
        ({"aud": "transcode", "azp": "transcode"}, 303),
        ({"aud": ["transcode"], "azp": "transcode"}, 303),
        ({"aud": "transcode"}, 303),
        ({"aud": ["transcode"]}, 303),
        ({"aud": "transcode", "azp": None}, 400),
        ({"aud": ["transcode"], "azp": None}, 400),
        ({"aud": "transcode", "azp": ["transcode"]}, 400),
    ],
)
async def test_oidc_claim_validation(factory, monkeypatch, claims, status) -> None:
    monkeypatch.setattr("joserfc._rfc7519.claims._generate_now", lambda: 1_800_000_000)
    provider = make_provider(mutate=claims)
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        response = await callback_after_login(ui, provider)
    assert response.status_code == status
    async with factory() as db:
        assert len((await db.scalars(select(SessionRow))).all()) == int(status == 303)


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,status", [(-1, 400), (0, 400), (1, 303)])
async def test_oidc_expiry_edges(factory, monkeypatch, offset, status) -> None:
    now = 1_800_000_000
    monkeypatch.setattr("joserfc._rfc7519.claims._generate_now", lambda: now)
    provider = make_provider(mutate={"iat": now - 60, "exp": now + offset})
    app = create_app(factory, config=make_config(), http_client=provider.http)
    async with make_ui(app) as ui, provider.http:
        response = await callback_after_login(ui, provider)
    assert response.status_code == status
    async with factory() as db:
        assert len((await db.scalars(select(SessionRow))).all()) == int(status == 303)


@pytest.mark.asyncio
async def test_auth_persisted_restart(monkeypatch) -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now[0])
    async with database_context("auth-restart", tables=("users", "sessions")) as factory:
        async with factory() as db:
            row, token = await create_user_session(db, "operator", 1)
            _, revoked_token = await create_user_session(db, "revoked", 1)
            await revoke_session(db, revoked_token)
            await db.commit()
        old_tickets = WsTickets()
        ticket = old_tickets.issue(
            Principal(
                user_id=row.user_id, username="operator", session_hash=row.token_hash, session_expires_at=row.expires_at
            )
        )
        old_pending = PendingStates()
        old_pending.create("old-state", "old-verifier", "old-nonce")
        engine, reopened = create_engine_and_session(factory.kw["bind"].url)
        try:
            stack = AuthStack(
                config=ServerConfig(api_key="test-key"),
                sessionmaker=reopened,
                ws_tickets=WsTickets(),
                pending=PendingStates(),
            )
            app = FastAPI()
            app.state.auth = stack
            app.include_router(auth_router(stack))
            assert stack.ws_tickets.consume(ticket) is None
            assert stack.pending.consume("old-state") is None
            async with make_ui(app) as ui:
                ui.cookies.set("mw_session", token, domain="testserver.local")
                headers = {}
                me = await ui.get("/api/auth/me", headers=headers)
                assert me.status_code == 200 and me.json()["username"] == "operator"
                assert (
                    await ui.get("/api/auth/me", headers={"Cookie": f"mw_session={revoked_token}"})
                ).status_code == 401
                async with reopened() as db:
                    stored = (await db.scalars(select(SessionRow))).one()
                    assert stored.token_hash == row.token_hash and stored.token_hash != token
                    assert stored.expires_at.replace(tzinfo=UTC) == row.expires_at
                now[0] = row.expires_at
                assert (await ui.get("/api/auth/me", headers=headers)).status_code == 401
        finally:
            await asyncio.wait_for(engine.dispose(), timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,failure", [("commit", "cancel"), ("close", "cancel"), ("close", "error")])
@pytest.mark.parametrize("operation", ["logout", "reset-password", "change-password"])
async def test_revocation_finishes(factory, monkeypatch, phase, failure, operation) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now)
    stack = AuthStack(
        config=ServerConfig(api_key="test-key"),
        sessionmaker=factory,
        ws_tickets=WsTickets(),
        pending=PendingStates(),
    )
    app = FastAPI()
    app.state.auth = stack
    app.state.settings = SimpleNamespace(current=SimpleNamespace(session_ttl_days=1))
    app.include_router(auth_router(stack))
    async with factory() as db:
        row, token = await create_user_session(db, "operator", 1)
        other, other_token = await create_user_session(db, "other", 1)
        await db.commit()
    principal = Principal(
        user_id=row.user_id, username="operator", session_hash=row.token_hash, session_expires_at=row.expires_at
    )
    ticket = stack.ws_tickets.issue(principal)
    revoked = stack.ws_tickets.watch_session(row.token_hash)
    other_revoked = stack.ws_tickets.watch_session(other.token_hash)
    interrupted = asyncio.Event()
    release = asyncio.Event()
    original_exit = AsyncSession.__aexit__
    original_commit = AsyncSession.commit

    async def mark_revoke(request, db, hashes):
        db.info["logout"] = True
        await _commit_and_revoke(request, db, hashes)

    async def interrupt():
        # The commit has completed. Interrupt result delivery or session closure.
        interrupted.set()
        await release.wait()
        if failure == "error":
            raise RuntimeError("session close failed")

    async def commit_session(db):
        await original_commit(db)
        if db.info.get("logout") and phase == "commit":
            await interrupt()

    async def close_session(db, *args):
        await original_exit(db, *args)
        if db.info.get("logout") and phase == "close":
            await interrupt()

    monkeypatch.setattr("server.api.auth._commit_and_revoke", mark_revoke)
    monkeypatch.setattr(AsyncSession, "commit", commit_session)
    monkeypatch.setattr(AsyncSession, "__aexit__", close_session)
    task = None
    try:
        async with make_ui(app) as ui:
            payload = None
            if operation == "reset-password":
                payload = {"username": "operator", "password": "new-password"}
            elif operation == "change-password":
                payload = {"current_password": "old-password", "password": "new-password"}
            task = asyncio.create_task(
                ui.post(
                    f"/api/auth/{operation}",
                    headers={"Authorization": "Bearer test-key"}
                    if operation == "reset-password"
                    else {"Cookie": f"mw_session={token}"},
                    json=payload,
                )
            )
            await asyncio.wait_for(interrupted.wait(), timeout=1)
            if failure == "cancel":
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)
            else:
                release.set()
                with pytest.raises(RuntimeError):
                    await asyncio.wait_for(task, timeout=1)
            async with factory() as db:
                assert await authenticate_session(db, token) is None
                assert await authenticate_session(db, other_token) is not None
            assert revoked.is_set()
            assert not other_revoked.is_set()
            assert stack.ws_tickets.consume(ticket) is None
    finally:
        release.set()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await asyncio.wait_for(task, timeout=1)
        stack.ws_tickets.unwatch_session(row.token_hash, revoked)
        stack.ws_tickets.unwatch_session(other.token_hash, other_revoked)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["commit", "rollback"])
@pytest.mark.parametrize("operation", ["logout", "reset-password", "change-password"])
async def test_repeated_revocation_cancellation(factory, monkeypatch, outcome, operation) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now)
    stack = AuthStack(
        config=ServerConfig(api_key="test-key"),
        sessionmaker=factory,
        ws_tickets=WsTickets(),
        pending=PendingStates(),
    )
    app = FastAPI()
    app.state.auth = stack
    app.state.settings = SimpleNamespace(current=SimpleNamespace(session_ttl_days=1))
    app.include_router(auth_router(stack))
    async with factory() as db:
        row, token = await create_user_session(db, "operator", 1)
        other, other_token = await create_user_session(db, "other", 1)
        await db.commit()
    principal = Principal(
        user_id=row.user_id, username="operator", session_hash=row.token_hash, session_expires_at=row.expires_at
    )
    other_principal = Principal(
        user_id=other.user_id, username="operator", session_hash=other.token_hash, session_expires_at=other.expires_at
    )
    ticket = stack.ws_tickets.issue(principal)
    other_ticket = stack.ws_tickets.issue(other_principal)
    revoked = stack.ws_tickets.watch_session(row.token_hash)
    other_revoked = stack.ws_tickets.watch_session(other.token_hash)
    entered, release, exited = (asyncio.Event() for _ in range(3))
    observed = [asyncio.Event(), asyncio.Event()]
    commit_finished = asyncio.Event()
    shields = 0
    original_commit = AsyncSession.commit
    original_exit = AsyncSession.__aexit__
    original_shield = asyncio.shield

    async def mark_revoke(request, db, hashes):
        db.info["logout"] = True
        await _commit_and_revoke(request, db, hashes)

    async def commit_session(db):
        if not db.info.get("logout"):
            return await original_commit(db)
        try:
            if outcome == "commit":
                await original_commit(db)
            entered.set()
            await release.wait()
            if outcome == "rollback":
                raise RuntimeError("commit failed")
        finally:
            commit_finished.set()

    async def close_session(db, *args):
        if db.info.get("logout"):
            exited.set()
        await original_exit(db, *args)

    def observe_shield(awaitable):
        nonlocal shields
        # Observe the request catching cancellation, regardless of commit's internal tasks.
        if (
            task is not None
            and asyncio.current_task() is task
            and task.cancelling() > shields
            and shields < len(observed)
        ):
            observed[shields].set()
            shields += 1
        return original_shield(awaitable)

    monkeypatch.setattr("server.api.auth._commit_and_revoke", mark_revoke)
    monkeypatch.setattr(AsyncSession, "commit", commit_session)
    monkeypatch.setattr(AsyncSession, "__aexit__", close_session)
    monkeypatch.setattr("server.api.auth.asyncio.shield", observe_shield)
    task = None
    try:
        async with make_ui(app) as ui:
            payload = None
            if operation == "reset-password":
                payload = {"username": "operator", "password": "new-password"}
            elif operation == "change-password":
                payload = {"current_password": "old-password", "password": "new-password"}
            task = asyncio.create_task(
                ui.post(
                    f"/api/auth/{operation}",
                    headers={"Authorization": "Bearer test-key"}
                    if operation == "reset-password"
                    else {"Cookie": f"mw_session={token}"},
                    json=payload,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            for event in observed:
                assert task.cancel()
                await asyncio.wait_for(event.wait(), timeout=1)
                assert not task.done() and not exited.is_set()
                assert not commit_finished.is_set()
                assert not revoked.is_set() and not other_revoked.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError if outcome == "commit" else RuntimeError):
                await asyncio.wait_for(task, timeout=1)
            assert exited.is_set() and commit_finished.is_set()
            async with factory() as db:
                active = await authenticate_session(db, token)
                assert (active is None) == (outcome == "commit")
                assert await authenticate_session(db, other_token) is not None
            assert revoked.is_set() == (outcome == "commit")
            assert not other_revoked.is_set()
            assert stack.ws_tickets.consume(ticket) == (None if outcome == "commit" else principal)
            assert stack.ws_tickets.consume(other_ticket) == other_principal
    finally:
        release.set()
        if task is not None:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
        if entered.is_set():
            await asyncio.wait_for(commit_finished.wait(), timeout=1)
        stack.ws_tickets.unwatch_session(row.token_hash, revoked)
        stack.ws_tickets.unwatch_session(other.token_hash, other_revoked)
