from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlencode

import httpx
from common.diagnostics import error_traceback, redact
from common.process import drain
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from joserfc import jwk, jwt
from joserfc.errors import JoseError
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text
from starlette.responses import Response

from server.core.auth import (
    AuthCapacityError,
    LoginThrottle,
    PendingStates,
    Principal,
    WsTickets,
    authenticate_session,
    create_session,
    hash_password,
    revoke_session,
    verify_password,
)
from server.core.config import OidcConfig, ServerConfig
from server.models import SessionRow, User, utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.playback.service import PlaybackService

# RS/PS/ES families only. None and HS* never appear here, so jwt.decode rejects them.
_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
)

_STATE_COOKIE = "oidc_state"
SESSION_COOKIE = "mw_session"
LOG = logging.getLogger(__name__)


class OidcError(Exception):
    """Provider and ID-token failures return HTTP 400 from the callback."""


@dataclass(kw_only=True)
class AuthStack:
    config: ServerConfig
    sessionmaker: async_sessionmaker[AsyncSession]
    ws_tickets: WsTickets
    pending: PendingStates
    oidc: OidcService | None = None
    http_client: httpx.AsyncClient | None = None
    owns_client: bool = False
    logins: LoginThrottle = field(default_factory=LoginThrottle)

    async def cleanup(self) -> None:
        now = utcnow()
        self.pending.purge(now)
        self.ws_tickets.purge(now)
        async with self.sessionmaker() as db:
            await db.execute(delete(SessionRow).where(SessionRow.expires_at <= now))
            await db.commit()


class OidcService:
    def __init__(self, config: OidcConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client
        self._metadata: dict | None = None

    async def metadata(self) -> dict:
        if self._metadata is not None:
            return self._metadata
        resp = await self.client.get(f"{self.config.issuer}/.well-known/openid-configuration")
        if resp.status_code != 200:
            msg = f"discovery request failed with status {resp.status_code}"
            raise OidcError(msg)
        doc = resp.json()
        if doc.get("issuer") != self.config.issuer:
            msg = "provider issuer does not match configured issuer"
            raise OidcError(msg)
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not doc.get(key):
                msg = f"provider metadata missing {key}"
                raise OidcError(msg)
        self._metadata = doc
        return doc

    async def begin(self) -> tuple[str, str, str, str]:
        meta = await self.metadata()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).rstrip(b"=").decode()
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.config.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{meta['authorization_endpoint']}?{query}", state, nonce, verifier

    async def finish(self, code: str, verifier: str, nonce: str) -> None:
        meta = await self.metadata()
        resp = await self.client.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
                "code_verifier": verifier,
            },
            auth=(self.config.client_id, self.config.secret),
        )
        if resp.status_code != 200:
            msg = f"token exchange failed with status {resp.status_code}"
            raise OidcError(msg)
        id_token = resp.json().get("id_token")
        if not id_token:
            msg = "token response missing id_token"
            raise OidcError(msg)

        jwks_resp = await self.client.get(meta["jwks_uri"])
        if jwks_resp.status_code != 200:
            msg = f"jwks request failed with status {jwks_resp.status_code}"
            raise OidcError(msg)
        keys = jwks_resp.json().get("keys")
        if not keys:
            msg = "jwks response has no keys"
            raise OidcError(msg)
        keyset = jwk.KeySet([jwk.import_key(item) for item in keys])

        try:
            token = jwt.decode(id_token, keyset, algorithms=list(_ALGORITHMS))
            claims = token.claims
            registry = jwt.JWTClaimsRegistry(
                iss={"essential": True, "value": self.config.issuer},
                aud={"essential": True, "value": self.config.client_id},
                sub={"essential": True},
                exp={"essential": True},
                iat={"essential": True},
            )
            registry.validate(claims)
        except JoseError as exc:
            detail = redact(str(exc), secrets=(id_token, code, verifier, nonce, self.config.secret))
            msg = f"id_token rejected: {detail}"
            raise OidcError(msg) from exc

        # The claims validator accepts equality, but an ID token expires at exp.
        if claims["exp"] <= registry.now:
            msg = "id_token expired"
            raise OidcError(msg)
        aud = claims.get("aud")
        if ("azp" in claims or (isinstance(aud, list) and len(aud) > 1)) and claims.get("azp") != self.config.client_id:
            msg = "id_token azp does not match client_id"
            raise OidcError(msg)
        if claims.get("nonce") != nonce:
            msg = "id_token nonce does not match"
            raise OidcError(msg)


async def _authenticate_request(
    request: Request, config: ServerConfig, sessionmaker: async_sessionmaker[AsyncSession]
) -> Principal:
    header = request.headers.get("authorization", "")
    if header:
        token = header.removeprefix("Bearer ").strip()
        if header.startswith("Bearer ") and config.api_key and secrets.compare_digest(token, config.api_key):
            return Principal(user_id=None, username="API key")
        raise HTTPException(401, "Invalid API key.", headers={"WWW-Authenticate": "Bearer"})
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Sign in to continue.")
    async with sessionmaker() as db:
        row = await authenticate_session(db, token)
        if row is None:
            raise HTTPException(401, "Invalid or expired session.")
        user = await db.get(User, row.user_id)
        if user is None:
            raise HTTPException(401, "Invalid or expired session.")
        principal = Principal(
            user_id=user.id,
            username=user.username,
            session_hash=row.token_hash,
            session_expires_at=row.expires_at,
        )
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        check_browser_request(request)
    return principal


def check_browser_request(request: Request) -> None:
    if request.headers.get("x-millwright-request") != "1":
        raise HTTPException(403, "X-Millwright-Request: 1 is required")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site:
        # Nuxt's dev proxy rewrites Host, but preserves the browser's fetch metadata.
        if fetch_site.lower() not in {"same-origin", "none"}:
            raise HTTPException(403, "Cross-origin requests are not allowed.")
        return
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Request origin does not match the server URL.")


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^\S(?:.*\S)?$")
    password: str = Field(min_length=1, max_length=1024, repr=False)


class PasswordChangeBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024, repr=False)
    password: str = Field(min_length=1, max_length=1024, repr=False)


def _set_session(response: Response, request: Request, token: str, days: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=days * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


async def _commit_and_revoke(request: Request, db: AsyncSession, hashes: list[str]) -> None:
    async def finish() -> None:
        await db.commit()
        stack: AuthStack = request.app.state.auth
        playback: PlaybackService | None = getattr(request.app.state, "playback", None)
        for token_hash in hashes:
            stack.ws_tickets.revoke_session(token_hash)
            if playback is not None:
                playback.revoke_session(token_hash)

    # Revocation must follow a committed deletion even when the HTTP request is cancelled.
    committing = asyncio.create_task(finish())
    cancelled = False
    while not committing.done():
        try:
            await asyncio.shield(committing)
        except asyncio.CancelledError:
            cancelled = True
    committing.result()
    if cancelled:
        raise asyncio.CancelledError


async def current_principal(request: Request) -> Principal:
    stack: AuthStack = request.app.state.auth
    return await _authenticate_request(request, stack.config, stack.sessionmaker)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def auth_router(stack: AuthStack) -> APIRouter:
    config = stack.config
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    async def oidc_user(db: AsyncSession, *, lock: bool = False) -> User | None:
        if stack.oidc is None or not stack.oidc.config.username:
            return None
        query = select(User).where(User.username == stack.oidc.config.username)
        if lock:
            query = query.with_for_update()
        return await db.scalar(query)

    def api_key_only(principal: Principal) -> None:
        if principal.session_hash is not None:
            raise HTTPException(403, "This operation requires the instance API key.")

    @router.get("/status", summary="Get sign-in status")
    async def status(request: Request) -> dict:
        async with stack.sessionmaker() as db:
            setup_required = await db.scalar(select(User.id).limit(1)) is None
            oidc_available = await oidc_user(db) is not None
        try:
            principal = await _authenticate_request(request, config, stack.sessionmaker)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            principal = None
        return {
            "setup_required": setup_required,
            "authenticated": principal is not None,
            "user": {"id": principal.user_id, "username": principal.username}
            if principal is not None and principal.user_id is not None
            else None,
            "oidc_available": oidc_available,
        }

    @router.post("/setup", summary="Create the first local account")
    async def setup(request: Request, body: LoginBody) -> Response:
        check_browser_request(request)
        try:
            stack.logins.check(request.client.host if request.client else "unknown")
        except AuthCapacityError as exc:
            raise HTTPException(429, str(exc)) from exc
        async with stack.logins.password_slots:
            # Keep the password slot until the worker finishes, including request cancellation.
            password_hash = await drain(asyncio.create_task(asyncio.to_thread(hash_password, body.password)))
        async with stack.sessionmaker() as db:
            # No user row exists during initial setup, so concurrent registrations need a table lock.
            await db.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))
            if await db.scalar(select(User.id).limit(1)) is not None:
                raise HTTPException(409, "An account already exists. Sign in instead.")
            user = User(username=body.username, password_hash=password_hash)
            db.add(user)
            await db.flush()
            _, token = await create_session(db, user.id, request.app.state.settings.current.session_ttl_days)
            await db.commit()
        response = JSONResponse({"id": user.id, "username": user.username, "auth_method": "session"})
        _set_session(response, request, token, request.app.state.settings.current.session_ttl_days)
        return response

    @router.post("/login", summary="Sign in with a username and password")
    async def local_login(request: Request, body: LoginBody) -> Response:
        check_browser_request(request)
        try:
            stack.logins.check(request.client.host if request.client else "unknown")
        except AuthCapacityError as exc:
            raise HTTPException(429, str(exc)) from exc
        async with stack.sessionmaker() as db:
            user = await db.scalar(select(User).where(User.username == body.username))
            old_hash = user.password_hash if user is not None else None
        async with stack.logins.password_slots:
            if old_hash is None:
                await drain(asyncio.create_task(asyncio.to_thread(hash_password, body.password)))
                valid = False
            else:
                valid = await drain(asyncio.create_task(asyncio.to_thread(verify_password, body.password, old_hash)))
        if not valid or user is None:
            raise HTTPException(401, "Invalid username or password.")
        async with stack.sessionmaker() as db:
            # Reset can change the password during verification. Recheck it under the same user-row lock.
            current = await db.scalar(select(User).where(User.id == user.id).with_for_update())
            if current is None or current.password_hash != old_hash:
                raise HTTPException(401, "Invalid username or password.")
            _, token = await create_session(db, current.id, request.app.state.settings.current.session_ttl_days)
            await db.commit()
        response = JSONResponse({"id": current.id, "username": current.username, "auth_method": "session"})
        _set_session(response, request, token, request.app.state.settings.current.session_ttl_days)
        return response

    @router.post("/change-password", summary="Change your password")
    async def change_password(request: Request, body: PasswordChangeBody, principal: PrincipalDep) -> Response:
        if principal.user_id is None or principal.session_hash is None:
            raise HTTPException(403, "Sign in to change your password.")
        try:
            stack.logins.check(request.client.host if request.client else "unknown")
        except AuthCapacityError as exc:
            raise HTTPException(429, str(exc)) from exc
        async with stack.sessionmaker() as db:
            # Password changes, resets, and logout must finish in order for this account.
            user = await db.scalar(select(User).where(User.id == principal.user_id).with_for_update())
            if user is None:
                raise HTTPException(401, "Invalid or expired session.")
            async with stack.logins.password_slots:
                valid = await drain(
                    asyncio.create_task(asyncio.to_thread(verify_password, body.current_password, user.password_hash))
                )
                if not valid:
                    raise HTTPException(403, "Current password is incorrect.")
                password_hash = await drain(asyncio.create_task(asyncio.to_thread(hash_password, body.password)))
            if await authenticate_session(db, request.cookies[SESSION_COOKIE]) is None:
                raise HTTPException(401, "Invalid or expired session.")
            user.password_hash = password_hash
            hashes = list(
                await db.scalars(
                    delete(SessionRow).where(SessionRow.user_id == user.id).returning(SessionRow.token_hash)
                )
            )
            _, token = await create_session(db, user.id, request.app.state.settings.current.session_ttl_days)
            await _commit_and_revoke(request, db, hashes)
        response = JSONResponse({"id": user.id, "username": user.username, "auth_method": "session"})
        _set_session(response, request, token, request.app.state.settings.current.session_ttl_days)
        return response

    @router.post("/reset-password", status_code=204, summary="Reset an account password")
    async def reset_password(request: Request, body: LoginBody, principal: PrincipalDep) -> Response:
        api_key_only(principal)
        async with stack.logins.password_slots:
            password_hash = await drain(asyncio.create_task(asyncio.to_thread(hash_password, body.password)))
        async with stack.sessionmaker() as db:
            user = await db.scalar(select(User).where(User.username == body.username).with_for_update())
            if user is None:
                raise HTTPException(404, "Account not found.")
            user.password_hash = password_hash
            hashes = list(
                await db.scalars(
                    delete(SessionRow).where(SessionRow.user_id == user.id).returning(SessionRow.token_hash)
                )
            )
            await _commit_and_revoke(request, db, hashes)
        return Response(status_code=204)

    @router.get("/oidc/login", summary="Start an OIDC login")
    async def oidc_login() -> Response:
        if stack.oidc is None:
            return JSONResponse(status_code=404, content={"error": "OIDC is not configured."})
        if not stack.oidc.config.username:
            return JSONResponse(
                status_code=404, content={"error": "Set auth.oidc.username to an existing Millwright username."}
            )
        async with stack.sessionmaker() as db:
            mapped = await oidc_user(db)
        if mapped is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": f"Millwright account {stack.oidc.config.username!r} was not found. Check auth.oidc.username."
                },
            )
        redirect_url, state, nonce, verifier = await stack.oidc.begin()
        try:
            stack.pending.create(state, verifier, nonce)
        except AuthCapacityError as exc:
            return JSONResponse(status_code=429, content={"error": str(exc)})
        secure = config.auth.oidc is not None and config.auth.oidc.redirect_uri.startswith("https://")
        resp: Response = JSONResponse(content={"authorize_url": redirect_url})
        resp.set_cookie(_STATE_COOKIE, state, max_age=300, httponly=True, samesite="lax", secure=secure)
        return resp

    @router.get("/oidc/callback", summary="Finish an OIDC login")
    async def oidc_callback(
        request: Request,
        error: Annotated[str | None, Query()] = None,
        code: Annotated[str | None, Query()] = None,
        state: Annotated[str | None, Query()] = None,
    ) -> Response:
        try:
            response = await finish_callback(request, error, code, state)
        except Exception as exc:
            private = tuple(
                value
                for value in (code, state, stack.oidc.config.secret if stack.oidc is not None else None)
                if value is not None
            )
            LOG.error(
                "OIDC callback failed: %s",
                redact(str(exc), secrets=private),
                extra={"traceback": error_traceback(exc, secrets=private)},
            )
            response = JSONResponse(status_code=500, content={"error": "OIDC sign-in failed. Check the server logs."})
        response.delete_cookie(_STATE_COOKIE)
        return response

    async def finish_callback(request: Request, error: str | None, code: str | None, state: str | None) -> Response:
        if stack.oidc is None:
            msg = "OIDC is not configured."
            return JSONResponse(status_code=404, content={"error": msg})
        if error:
            msg = f"provider returned error: {error}"
            return JSONResponse(status_code=400, content={"error": msg})
        cookie_state = request.cookies.get(_STATE_COOKIE)
        if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
            return JSONResponse(status_code=400, content={"error": "state missing or mismatched"})
        pending = stack.pending.consume(state)
        if pending is None:
            return JSONResponse(status_code=400, content={"error": "unknown or expired state"})
        if not code:
            return JSONResponse(status_code=400, content={"error": "missing code"})
        verifier, nonce = pending
        try:
            await stack.oidc.finish(code, verifier, nonce)
        except (OidcError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            detail = redact(str(exc), secrets=(stack.oidc.config.secret, code, verifier, nonce, state))
            LOG.warning("OIDC callback failed: %s", detail)
            return JSONResponse(status_code=400, content={"error": detail})
        async with stack.sessionmaker() as db:
            user = await oidc_user(db, lock=True)
            if user is None:
                message = (
                    f"Millwright account {stack.oidc.config.username!r} was not found. Check auth.oidc.username."
                    if stack.oidc.config.username
                    else "Set auth.oidc.username to an existing Millwright username."
                )
                return JSONResponse(status_code=503, content={"error": message})
            _, token = await create_session(db, user.id, request.app.state.settings.current.session_ttl_days)
            await db.commit()
        target = config.auth.oidc.post_login_url if config.auth.oidc is not None else None
        response = RedirectResponse(target or "/", status_code=303)
        _set_session(response, request, token, request.app.state.settings.current.session_ttl_days)
        return response

    @router.get("/me", summary="Show the current session")
    async def me(principal: PrincipalDep) -> dict:
        return {
            "id": principal.user_id,
            "username": principal.username,
            "auth_method": "session" if principal.session_hash is not None else "api_key",
        }

    @router.post("/logout", status_code=204, summary="Revoke the current session")
    async def logout(request: Request, principal: PrincipalDep) -> Response:
        if principal.session_hash is None:
            raise HTTPException(400, "API keys cannot be logged out")
        token = request.cookies[SESSION_COOKIE]
        async with stack.sessionmaker() as db:
            await db.scalar(select(User.id).where(User.id == principal.user_id).with_for_update())
            await revoke_session(db, token)
            await _commit_and_revoke(request, db, [principal.session_hash])
        # A delayed logout response must not erase a newer login cookie shared across tabs.
        # The old cookie is unusable because its database session has been revoked.
        return Response(status_code=204)

    @router.post("/ws-ticket", summary="Issue an events WebSocket ticket")
    async def ws_ticket(principal: PrincipalDep) -> dict:
        try:
            async with stack.sessionmaker() as db:
                if principal.session_hash is not None:
                    active = await db.scalar(
                        select(SessionRow.id)
                        .where(SessionRow.token_hash == principal.session_hash, SessionRow.expires_at > utcnow())
                        .with_for_update(read=True)
                    )
                    if active is None:
                        raise HTTPException(401, "Invalid or expired session.")
                ticket = stack.ws_tickets.issue(principal)
        except AuthCapacityError as exc:
            raise HTTPException(429, str(exc)) from exc
        return {"ticket": ticket, "ttl_s": stack.ws_tickets.ttl_s}

    return router
