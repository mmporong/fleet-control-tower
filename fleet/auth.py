"""Operator accounts, sessions and request checks for the control-room web servers.

- Roles: ``viewer`` (read only), ``operator`` (operational commands),
  ``supervisor`` (also simulation control and accounts). Upper systems (WMS)
  use API keys with the ``wms`` role, only for the WMS endpoints.
- Passwords: PBKDF2-HMAC-SHA256 with a random salt (stdlib ``hashlib``).
  Five wrong passwords lock the account for five minutes.
- Sessions: a random token in an HttpOnly, SameSite=Strict cookie; only its
  SHA-256 is stored. Absolute lifetime and an idle timeout.
- State-changing requests need the session's CSRF token in ``X-CSRF-Token``, a
  JSON body, and an Origin (when the browser sends one) equal to the Host.

The audit log records the authenticated account, never a name the client
claims.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from .store import Store

ROLES = ("viewer", "operator", "supervisor")
RANK = {"viewer": 0, "operator": 1, "supervisor": 2}
ITERATIONS = 200_000
LOCK_AFTER = 5
LOCK_S = 300.0
SESSION_S = 8 * 3600.0          # one shift
IDLE_S = 30 * 60.0
COOKIE = "fct_session"


class AuthError(ValueError):
    def __init__(self, msg: str, status: int = 401) -> None:
        super().__init__(msg)
        self.status = status


@dataclass
class Session:
    name: str
    role: str
    csrf: str


def _hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def at_least(role: str, needed: str) -> bool:
    """
    >>> at_least("supervisor", "operator"), at_least("viewer", "operator")
    (True, False)
    """
    return RANK.get(role, -1) >= RANK[needed]


class Auth:
    def __init__(self, store: Store, clock=time.time) -> None:
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------ accounts
    def has_users(self) -> bool:
        with self.store.lock:
            return self.store.db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0

    def create_user(self, name: str, password: str, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if not name or len(name) > 40 or not all(c.isalnum() or c in "._-" for c in name):
            raise ValueError("account name: 1-40 letters, digits or ._-")
        if len(password) < 10:
            raise ValueError("password: at least 10 characters")
        salt = secrets.token_bytes(16)
        with self.store.lock:
            self.store.db.execute(
                "INSERT OR REPLACE INTO users(name, role, salt, hash, iterations, failed, locked_until) "
                "VALUES (?, ?, ?, ?, ?, 0, 0)", (name, role, salt, _hash_password(password, salt, ITERATIONS),
                                                  ITERATIONS))

    def bootstrap(self, password: str | None = None) -> str | None:
        """First start: create the supervisor account ``admin``. Returns the generated password, if one was."""
        if self.has_users():
            return None
        generated = None
        if not password:
            password = generated = secrets.token_urlsafe(12)
        self.create_user("admin", password, "supervisor")
        return generated

    def users(self) -> list[dict]:
        with self.store.lock:
            rows = self.store.db.execute("SELECT name, role, locked_until FROM users ORDER BY name").fetchall()
        return [{"name": n, "role": r, "locked": lu > self.clock()} for n, r, lu in rows]

    # ------------------------------------------------------------ sessions
    def login(self, name: str, password: str) -> tuple[str, Session]:
        now = self.clock()
        with self.store.lock:
            row = self.store.db.execute("SELECT role, salt, hash, iterations, failed, locked_until FROM users "
                                        "WHERE name=?", (name,)).fetchone()
            if row is None:
                _hash_password(password, b"0" * 16, ITERATIONS)       # same cost: no user enumeration by timing
                raise AuthError("계정 또는 비밀번호가 맞지 않습니다")
            role, salt, digest, iterations, failed, locked_until = row
            if locked_until > now:
                raise AuthError(f"로그인 잠김: {int(locked_until - now)}초 뒤 다시 시도", 429)
            if not hmac.compare_digest(_hash_password(password, salt, iterations), digest):
                failed += 1
                lock = now + LOCK_S if failed >= LOCK_AFTER else 0.0
                self.store.db.execute("UPDATE users SET failed=?, locked_until=? WHERE name=?",
                                      (0 if lock else failed, lock, name))
                raise AuthError("계정 또는 비밀번호가 맞지 않습니다" +
                                (f" · {LOCK_AFTER}회 실패로 {int(LOCK_S / 60)}분 잠김" if lock else ""))
            self.store.db.execute("UPDATE users SET failed=0, locked_until=0 WHERE name=?", (name,))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            self.store.db.execute("INSERT INTO sessions(token_hash, name, csrf, created, last) VALUES (?, ?, ?, ?, ?)",
                                  (_digest(token), name, csrf, now, now))
        return token, Session(name, role, csrf)

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = self.clock()
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT s.name, u.role, s.csrf, s.created, s.last FROM sessions s JOIN users u ON u.name = s.name "
                "WHERE s.token_hash=?", (_digest(token),)).fetchone()
            if row is None:
                return None
            name, role, csrf, created, last = row
            if now - created > SESSION_S or now - last > IDLE_S:
                self.store.db.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))
                return None
            self.store.db.execute("UPDATE sessions SET last=? WHERE token_hash=?", (now, _digest(token)))
        return Session(name, role, csrf)

    def logout(self, token: str | None) -> None:
        if token:
            with self.store.lock:
                self.store.db.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))

    # ------------------------------------------------------------ API keys (upper systems)
    def create_api_key(self, name: str, role: str = "wms") -> str:
        key = "fct_" + secrets.token_urlsafe(32)
        with self.store.lock:
            self.store.db.execute("INSERT INTO api_keys(key_hash, name, role) VALUES (?, ?, ?)",
                                  (_digest(key), name, role))
        return key

    def api_key(self, key: str | None) -> Session | None:
        if not key:
            return None
        with self.store.lock:
            row = self.store.db.execute("SELECT name, role FROM api_keys WHERE key_hash=?", (_digest(key),)).fetchone()
        return Session(row[0], row[1], "") if row else None


def cookie_token(cookie_header: str | None) -> str | None:
    """
    >>> cookie_token("a=1; fct_session=abc; b=2")
    'abc'
    """
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return None


def session_cookie(token: str, secure: bool = False, clear: bool = False) -> str:
    attrs = [f"{COOKIE}={'' if clear else token}", "Path=/", "HttpOnly", "SameSite=Strict"]
    if secure:
        attrs.append("Secure")
    if clear:
        attrs.append("Max-Age=0")
    return "; ".join(attrs)


def check_state_change(headers, session: Session, host_header: str | None) -> None:
    """CSRF defences for a state-changing request from a browser session."""
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise AuthError("Content-Type은 application/json이어야 합니다", 415)
    origin = headers.get("Origin")
    if origin and origin != "null":
        host = origin.split("://", 1)[-1].rstrip("/")
        if host_header is None or host != host_header:
            raise AuthError("다른 출처(Origin)에서 온 요청은 거부합니다", 403)
    if not hmac.compare_digest(headers.get("X-CSRF-Token") or "", session.csrf):
        raise AuthError("CSRF 토큰이 없거나 맞지 않습니다", 403)
