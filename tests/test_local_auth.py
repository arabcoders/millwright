from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import timedelta

import httpx
import pytest
from psycopg.errors import LockNotAvailable
from server.api.app import create_app
from server.core.auth import hash_password, verify_password
from server.core.auth import create_session
from server.core.config import ServerConfig
from server.models import SessionRow, User
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from tests.helpers import create_user_session
from tests.helpers import database_context

KEY = {"Authorization": "Bearer test-key"}
BROWSER = {"X-Millwright-Request": "1"}


def browser(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver", headers=BROWSER)


@pytest.mark.asyncio
async def test_status_database_failure() -> None:
    async with database_context("missing-auth-tables", tables=()) as factory:
        app = create_app(factory, config=ServerConfig(api_key="test-key"))
        async with browser(app) as client:
            response = await client.get("/api/auth/status")
            assert response.status_code == 500
            assert "setup_required" not in response.json()


@pytest.mark.asyncio
async def test_setup_concurrency(database_factory, monkeypatch) -> None:
    app = create_app(database_factory, config=ServerConfig(api_key="test-key"))
    issuing, release = asyncio.Event(), asyncio.Event()
    tasks = []

    async def delayed_session(db, user_id, ttl_days):
        issuing.set()
        await release.wait()
        return await create_session(db, user_id, ttl_days)

    monkeypatch.setattr("server.api.auth.create_session", delayed_session)
    async with browser(app) as first, browser(app) as second:
        status = (await first.get("/api/auth/status")).json()
        assert status == {"setup_required": True, "authenticated": False, "user": None, "oidc_available": False}
        body = {"username": "operator", "password": "secret"}
        assert (await first.post("/api/auth/setup", json=body, headers={"X-Millwright-Request": ""})).status_code == 403
        assert (await first.post("/api/auth/setup", json=body, headers={"Origin": "null"})).status_code == 403
        first.headers.update({"Origin": "http://frontend.test:8082", "Sec-Fetch-Site": "same-origin"})
        second.headers["Origin"] = "http://testserver"
        try:
            tasks.append(
                asyncio.create_task(first.post("/api/auth/setup", json={"username": "first", "password": "secret"}))
            )
            await asyncio.wait_for(issuing.wait(), 3)
            # Setup must exclude other inserts while the first account is uncommitted.
            async with database_factory() as db:
                with pytest.raises(OperationalError) as locked:
                    await db.execute(text("LOCK TABLE users IN ROW EXCLUSIVE MODE NOWAIT"))
                assert isinstance(locked.value.orig, LockNotAvailable)
            tasks.append(
                asyncio.create_task(second.post("/api/auth/setup", json={"username": "second", "password": "secret"}))
            )
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        assert sorted(response.status_code for response in responses) == [200, 409]
        successful = first if responses[0].status_code == 200 else second
        profile = (await successful.get("/api/auth/me")).json()
        assert profile["username"] in {"first", "second"} and profile["auth_method"] == "session"
        async with database_factory() as db:
            users = list(await db.scalars(select(User)))
            assert len(users) == 1 and users[0].id == profile["id"]
            assert verify_password("secret", users[0].password_hash)
            assert len(list(await db.scalars(select(SessionRow)))) == 1


@pytest.mark.asyncio
async def test_local_login_protection(database_factory) -> None:
    app = create_app(database_factory, config=ServerConfig(api_key="test-key"))
    async with database_factory() as db:
        await create_user_session(db, "operator", 1)
        await db.commit()
    body = {"username": "operator", "password": "test-password"}
    proxied = {"Origin": "http://frontend.test:8082", "Sec-Fetch-Site": "same-origin"}
    async with browser(app) as client:
        assert (
            await client.post("/api/auth/login", json=body, headers={**proxied, "X-Millwright-Request": ""})
        ).status_code == 403
        assert (
            await client.post("/api/auth/login", json=body, headers={"Origin": "https://elsewhere.test"})
        ).status_code == 403
        assert (await client.post("/api/auth/login", json=body, headers={"Origin": "null"})).status_code == 403
        for fetch_site in ("cross-site", "same-site"):
            assert (
                await client.post(
                    "/api/auth/login",
                    json=body,
                    headers={"Origin": "http://testserver", "Sec-Fetch-Site": fetch_site},
                )
            ).status_code == 403
        login = await client.post("/api/auth/login", json=body, headers=proxied)
        assert login.status_code == 200 and login.json()["auth_method"] == "session"
        assert "HttpOnly" in login.headers["set-cookie"] and "SameSite=lax" in login.headers["set-cookie"]
        assert (
            await client.patch("/api/settings", json={"session_ttl_days": 2}, headers={"X-Millwright-Request": ""})
        ).status_code == 403
        assert (
            await client.patch(
                "/api/settings", json={"session_ttl_days": 2}, headers={"Origin": "https://elsewhere.test"}
            )
        ).status_code == 403
        assert (await client.patch("/api/settings", json={"session_ttl_days": 2}, headers=proxied)).status_code == 200
        # Private bearer clients are not browser sessions and do not need browser headers.
        assert (
            await client.patch(
                "/api/settings",
                json={"session_ttl_days": 3},
                headers={**KEY, "X-Millwright-Request": "", "Origin": "null"},
            )
        ).status_code == 200
        assert (await client.post("/api/auth/logout", headers=KEY)).status_code == 400
        assert (await client.post("/api/auth/logout", headers={"Sec-Fetch-Site": "none"})).status_code == 204
        assert (await client.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_bad_login_throttling(database_factory, monkeypatch) -> None:
    app = create_app(database_factory, config=ServerConfig(api_key="test-key"))
    from server.models import utcnow

    now = utcnow()
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now)
    async with database_factory() as db:
        await create_user_session(db, "operator", 1)
        await db.commit()
    async with browser(app) as client:
        for index in range(10):
            response = await client.post(
                "/api/auth/login", json={"username": "missing" if index % 2 else "operator", "password": "wrong"}
            )
            assert response.status_code == 401 and response.json()["detail"] == "Invalid username or password."
        assert (
            await client.post("/api/auth/login", json={"username": "operator", "password": "test-password"})
        ).status_code == 429
        now += timedelta(minutes=1)
        assert (
            await client.post("/api/auth/login", json={"username": "operator", "password": "test-password"})
        ).status_code == 200


@pytest.mark.asyncio
async def test_password_change_sessions(database_factory) -> None:
    app = create_app(database_factory, config=ServerConfig(api_key="test-key"))
    async with database_factory() as db:
        other, other_token = await create_user_session(db, "other", 1)
        current, current_token = await create_user_session(db, "operator", 1)
        another, another_token = await create_user_session(db, "operator", 1)
        await db.commit()
    watched = {
        row.token_hash: app.state.auth.ws_tickets.watch_session(row.token_hash) for row in (current, another, other)
    }
    try:
        async with browser(app) as client:
            client.cookies.set("mw_session", current_token, domain="testserver.local")
            body = {"current_password": "test-password", "password": "new-password"}
            assert (await client.post("/api/auth/change-password", json=body, headers=KEY)).status_code == 403
            rejected = await client.post("/api/auth/change-password", json={**body, "current_password": "wrong"})
            assert rejected.status_code == 403
            assert (await client.get("/api/auth/me")).json()["username"] == "operator"
            async with database_factory() as db:
                user = await db.get(User, current.user_id)
                assert user is not None and verify_password("test-password", user.password_hash)
            changed = await client.post("/api/auth/change-password", json=body)
            assert changed.status_code == 200 and changed.json()["id"] == current.user_id
            assert client.cookies["mw_session"] != current_token
            assert (await client.get("/api/auth/me")).json()["username"] == "operator"
            for token in (current_token, another_token):
                assert (await client.get("/api/auth/me", headers={"Cookie": f"mw_session={token}"})).status_code == 401
            assert (await client.get("/api/auth/me", headers={"Cookie": f"mw_session={other_token}"})).json()[
                "username"
            ] == "other"
            assert watched[current.token_hash].is_set() and watched[another.token_hash].is_set()
            assert not watched[other.token_hash].is_set()
            assert (
                await client.post("/api/auth/login", json={"username": "operator", "password": "test-password"})
            ).status_code == 401
            assert (
                await client.post("/api/auth/login", json={"username": "operator", "password": "new-password"})
            ).status_code == 200
            async with database_factory() as db:
                other_user = await db.get(User, other.user_id)
                assert other_user is not None and verify_password("test-password", other_user.password_hash)
    finally:
        for token_hash, event in watched.items():
            app.state.auth.ws_tickets.unwatch_session(token_hash, event)


@pytest.mark.asyncio
async def test_password_reset_isolation(database_factory) -> None:
    app = create_app(database_factory, config=ServerConfig(api_key="test-key"))
    async with database_factory() as db:
        first, first_token = await create_user_session(db, "first", 1)
        another, another_token = await create_user_session(db, "first", 1)
        second, second_token = await create_user_session(db, "second", 1)
        await db.commit()
    first_revoked = app.state.auth.ws_tickets.watch_session(first.token_hash)
    another_revoked = app.state.auth.ws_tickets.watch_session(another.token_hash)
    second_revoked = app.state.auth.ws_tickets.watch_session(second.token_hash)
    try:
        async with browser(app) as client:
            client.cookies.set("mw_session", first_token, domain="testserver.local")
            body = {"username": "first", "password": "new-password"}
            assert (await client.post("/api/auth/reset-password", json=body)).status_code == 403
            assert (await client.post("/api/auth/reset-password", json=body, headers=KEY)).status_code == 204
            assert first_revoked.is_set() and another_revoked.is_set() and not second_revoked.is_set()
            assert (await client.get("/api/auth/me")).status_code == 401
            assert (
                await client.get("/api/auth/me", headers={"Cookie": f"mw_session={another_token}"})
            ).status_code == 401
            client.cookies.set("mw_session", second_token, domain="testserver.local")
            assert (await client.get("/api/auth/me")).json()["username"] == "second"
            assert (await client.get("/api/auth/me", headers=KEY)).json() == {
                "id": None,
                "username": "API key",
                "auth_method": "api_key",
            }
            assert (
                await client.post("/api/auth/login", json={"username": "first", "password": "test-password"})
            ).status_code == 401
            assert (await client.post("/api/auth/login", json=body)).status_code == 200
            assert (await client.get("/api/auth/me")).json()["username"] == "first"
    finally:
        app.state.auth.ws_tickets.unwatch_session(first.token_hash, first_revoked)
        app.state.auth.ws_tickets.unwatch_session(another.token_hash, another_revoked)
        app.state.auth.ws_tickets.unwatch_session(second.token_hash, second_revoked)


@pytest.mark.asyncio
async def test_reset_rejects_password(database_factory, monkeypatch) -> None:
    app = create_app(database_factory, config=ServerConfig(api_key="test-key"))
    async with database_factory() as db:
        await create_user_session(db, "operator", 1)
        await db.commit()
    verified = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def delayed_verify(password, encoded):
        result = verify_password(password, encoded)
        loop.call_soon_threadsafe(verified.set)
        if not release.wait(5):
            raise RuntimeError("Password verification was not released.")
        return result

    monkeypatch.setattr("server.api.auth.verify_password", delayed_verify)
    task = None
    async with browser(app) as old_login, browser(app) as recovery:
        try:
            task = asyncio.create_task(
                old_login.post("/api/auth/login", json={"username": "operator", "password": "test-password"})
            )
            await asyncio.wait_for(verified.wait(), 3)
            assert (
                await recovery.post(
                    "/api/auth/reset-password", headers=KEY, json={"username": "operator", "password": "new-password"}
                )
            ).status_code == 204
            release.set()
            response = await asyncio.wait_for(task, 3)
            assert response.status_code == 401 and "set-cookie" not in response.headers
            async with database_factory() as db:
                assert list(await db.scalars(select(SessionRow))) == []
        finally:
            release.set()
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_login_reset_order(database_factory, monkeypatch) -> None:
    app = create_app(database_factory, config=ServerConfig(api_key="test-key"))
    async with database_factory() as db:
        original, _ = await create_user_session(db, "operator", 1)
        await db.commit()
    issuing, release = asyncio.Event(), asyncio.Event()

    async def delayed_session(db, user_id, ttl_days):
        issuing.set()
        await release.wait()
        return await create_session(db, user_id, ttl_days)

    monkeypatch.setattr("server.api.auth.create_session", delayed_session)
    tasks = []
    async with browser(app) as login, browser(app) as recovery:
        try:
            tasks.append(
                asyncio.create_task(
                    login.post("/api/auth/login", json={"username": "operator", "password": "test-password"})
                )
            )
            await asyncio.wait_for(issuing.wait(), 3)
            # Login must retain the user lock until its new session commits.
            async with database_factory() as db:
                with pytest.raises(OperationalError) as locked:
                    await db.scalar(select(User.id).where(User.id == original.user_id).with_for_update(nowait=True))
                assert isinstance(locked.value.orig, LockNotAvailable)
            tasks.append(
                asyncio.create_task(
                    recovery.post(
                        "/api/auth/reset-password",
                        headers=KEY,
                        json={"username": "operator", "password": "new-password"},
                    )
                )
            )
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert [response.status_code for response in responses] == [200, 204]
            assert (await login.get("/api/auth/me")).status_code == 401
            async with database_factory() as db:
                assert list(await db.scalars(select(SessionRow))) == []
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_logout_preserves_relogin(database_factory, monkeypatch) -> None:
    app = create_app(database_factory, config=ServerConfig(api_key="test-key"))
    async with database_factory() as db:
        row, old_token = await create_user_session(db, "operator", 1)
        await db.commit()
    deleted = asyncio.Event()
    release = asyncio.Event()
    original = app.state.auth.ws_tickets.revoke_session

    def revoked(token_hash):
        original(token_hash)
        deleted.set()

    monkeypatch.setattr(app.state.auth.ws_tickets, "revoke_session", revoked)

    class DelayedLogout:
        async def __call__(self, scope, receive, send):
            async def delayed_send(message):
                if scope["path"] == "/api/auth/logout" and message["type"] == "http.response.start":
                    await release.wait()
                await send(message)

            await app(scope, receive, delayed_send)

    task = None
    async with browser(DelayedLogout()) as client:
        client.cookies.set("mw_session", old_token, domain="testserver.local")
        try:
            task = asyncio.create_task(client.post("/api/auth/logout"))
            await asyncio.wait_for(deleted.wait(), 3)
            login = await client.post("/api/auth/login", json={"username": "operator", "password": "test-password"})
            assert login.status_code == 200
            new_token = client.cookies["mw_session"]
            release.set()
            logout = await asyncio.wait_for(task, 3)
            assert logout.status_code == 204 and "set-cookie" not in logout.headers
            assert client.cookies["mw_session"] == new_token
            assert (await client.get("/api/auth/me")).status_code == 200
            async with database_factory() as db:
                assert await db.get(SessionRow, row.id) is None
        finally:
            release.set()
            if task is not None:
                await asyncio.wait_for(task, 3)
