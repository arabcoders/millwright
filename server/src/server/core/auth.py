from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from server.models import SessionRow, utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SESSION_PREFIX = "mw_"
TICKET_PREFIX = "wst_"


class AuthCapacityError(Exception):
    """A transient auth store is full of unexpired entries."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(
    db: AsyncSession,
    user_id: int,
    ttl_days: int,
    now: datetime | None = None,
) -> tuple[SessionRow, str]:
    token = SESSION_PREFIX + secrets.token_urlsafe(32)
    created = now if now is not None else utcnow()
    row = SessionRow(
        token_hash=hash_token(token),
        user_id=user_id,
        created_at=created,
        expires_at=created + timedelta(days=ttl_days),
    )
    db.add(row)
    await db.flush()
    return row, token


async def authenticate_session(db: AsyncSession, token: str, now: datetime | None = None) -> SessionRow | None:
    checked = now if now is not None else utcnow()
    stmt = select(SessionRow).where(
        SessionRow.token_hash == hash_token(token),
        SessionRow.expires_at > checked,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def revoke_session(db: AsyncSession, token: str) -> bool:
    identifier = await db.scalar(
        delete(SessionRow).where(SessionRow.token_hash == hash_token(token)).returning(SessionRow.id)
    )
    return identifier is not None


@dataclass(kw_only=True)
class _Ticket:
    principal: Principal
    expires_at: datetime


@dataclass(kw_only=True)
class _Pending:
    verifier: str
    nonce: str
    expires_at: datetime


class WsTickets:
    """One-time WebSocket tickets held in memory by a single server process."""

    def __init__(self, ttl_s: float = 30.0, *, capacity: int = 1024) -> None:
        self.ttl_s = ttl_s
        self.capacity = capacity
        self._tickets: dict[str, _Ticket] = {}
        self._sessions: dict[str, set[asyncio.Event]] = {}

    def issue(self, principal: Principal) -> str:
        now = utcnow()
        self.purge(now)
        if len(self._tickets) >= self.capacity:
            raise AuthCapacityError("Too many pending WebSocket tickets. Try again later.")
        ticket = TICKET_PREFIX + secrets.token_urlsafe(32)
        self._tickets[ticket] = _Ticket(principal=principal, expires_at=now + timedelta(seconds=self.ttl_s))
        return ticket

    def consume(self, ticket: str, now: datetime | None = None) -> Principal | None:
        checked = now if now is not None else utcnow()
        self.purge(checked)
        entry = self._tickets.pop(ticket, None)
        if entry is None or entry.expires_at <= checked:
            return None
        return entry.principal

    def watch_session(self, token_hash: str) -> asyncio.Event:
        revoked = asyncio.Event()
        self._sessions.setdefault(token_hash, set()).add(revoked)
        return revoked

    def unwatch_session(self, token_hash: str, revoked: asyncio.Event) -> None:
        watchers = self._sessions[token_hash]
        watchers.discard(revoked)
        if not watchers:
            del self._sessions[token_hash]

    def revoke_session(self, token_hash: str) -> None:
        for key in [key for key, entry in self._tickets.items() if entry.principal.session_hash == token_hash]:
            del self._tickets[key]
        for revoked in self._sessions.get(token_hash, ()):
            revoked.set()

    def purge(self, now: datetime | None = None) -> None:
        checked = now if now is not None else utcnow()
        for key in [key for key, entry in self._tickets.items() if entry.expires_at <= checked]:
            del self._tickets[key]


class PendingStates:
    """OIDC login states with PKCE verifiers, consumed once within their TTL."""

    def __init__(self, ttl_s: float = 300.0, *, capacity: int = 1024) -> None:
        self.ttl_s = ttl_s
        self.capacity = capacity
        self._states: dict[str, _Pending] = {}

    def create(self, state: str, verifier: str, nonce: str) -> None:
        now = utcnow()
        self.purge(now)
        if len(self._states) >= self.capacity:
            raise AuthCapacityError("Too many pending logins. Try again later.")
        self._states[state] = _Pending(verifier=verifier, nonce=nonce, expires_at=now + timedelta(seconds=self.ttl_s))

    def consume(self, state: str, now: datetime | None = None) -> tuple[str, str] | None:
        checked = now if now is not None else utcnow()
        self.purge(checked)
        entry = self._states.pop(state, None)
        if entry is None or entry.expires_at <= checked:
            return None
        return entry.verifier, entry.nonce

    def purge(self, now: datetime | None = None) -> None:
        checked = now if now is not None else utcnow()
        for key in [key for key, entry in self._states.items() if entry.expires_at <= checked]:
            del self._states[key]


@dataclass(kw_only=True)
class Principal:
    user_id: int | None
    username: str
    session_hash: str | None = None
    session_expires_at: datetime | None = None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    # OWASP's 2^14, r=8, p=5 scrypt option keeps each password operation near 16 MiB.
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=5)
    return f"scrypt$16384$8$5${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    _, n, r, p, salt, expected = encoded.split("$")
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return secrets.compare_digest(digest.hex(), expected)


class LoginThrottle:
    """Limit concurrent password hashing and login attempts per client IP."""

    def __init__(self) -> None:
        self.attempts: dict[str, deque[datetime]] = {}
        self.password_slots = asyncio.Semaphore(4)

    def check(self, address: str) -> None:
        now = utcnow()
        cutoff = now - timedelta(minutes=1)
        for key in list(self.attempts):
            attempts = self.attempts[key]
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                del self.attempts[key]
        attempts = self.attempts.get(address)
        if (attempts is not None and len(attempts) >= 10) or (attempts is None and len(self.attempts) >= 1024):
            raise AuthCapacityError("Too many login attempts. Try again in a minute.")
        self.attempts.setdefault(address, deque()).append(now)
