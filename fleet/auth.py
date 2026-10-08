"""Operator accounts, sessions and request checks for the control-room web servers.

- Roles: ``viewer`` (read only), ``operator`` (operational commands),
  ``supervisor`` (also simulation control and accounts). Upper systems (WMS)
  use API keys with the ``wms`` role, only for the WMS endpoints.
- Passwords: PBKDF2-HMAC-SHA256 with a random salt (stdlib ``hashlib``),
  computed outside the store lock and at most two at a time, so a burst of
  logins cannot stall the control room. Five wrong passwords for one account
  from one address lock that pair for five minutes, and one address gets 20
  wrong passwords per five minutes in all. Unknown and known accounts answer
  alike, and nobody can lock an operator out from another address.
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
import threading
import time
from dataclasses import dataclass

from .store import Store

ROLES = ("viewer", "operator", "supervisor")
RANK = {"viewer": 0, "operator": 1, "supervisor": 2}
ITERATIONS = 200_000
LOCK_AFTER = 5
LOCK_S = 300.0
ADDRESS_FAILS = 20              # wrong passwords per address and LOCK_S, over all accounts
HASH_SLOTS = 2                  # password hashes computed at the same time
HASH_WAIT_S = 5.0
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
        self._slots = threading.BoundedSemaphore(HASH_SLOTS)
        self._fail_lock = threading.Lock()
        self._fails: dict[tuple[str, str], list[float]] = {}     # (account, address) -> failure times
        self._addr_fails: dict[str, list[float]] = {}

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
        digest = _hash_password(password, salt, ITERATIONS)
        with self.store.lock:
            self.store.db.execute(
                "INSERT OR REPLACE INTO users(name, role, salt, hash, iterations, failed, locked_until) "
                "VALUES (?, ?, ?, ?, ?, 0, 0)", (name, role, salt, digest, ITERATIONS))
            # a new password or role ends the sessions opened with the old one
            self.store.db.execute("DELETE FROM sessions WHERE name=?", (name,))

    def remove_user(self, name: str) -> bool:
        with self.store.lock:
            gone = self.store.db.execute("DELETE FROM users WHERE name=?", (name,)).rowcount > 0
            self.store.db.execute("DELETE FROM sessions WHERE name=?", (name,))
        return gone

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
            rows = self.store.db.execute("SELECT name, role FROM users ORDER BY name").fetchall()
        return [{"name": n, "role": r} for n, r in rows]

    # ------------------------------------------------------------ sessions
    def _recent(self, times: list[float], now: float) -> list[float]:
        return [t for t in times if now - t < LOCK_S]

    def _locked(self, name: str, address: str, now: float) -> float:
        """Seconds until this account may be tried again from this address (0: not locked)."""
        with self._fail_lock:
            pair = self._fails[(name, address)] = self._recent(self._fails.get((name, address), []), now)
            addr = self._addr_fails[address] = self._recent(self._addr_fails.get(address, []), now)
            waits = []
            if len(pair) >= LOCK_AFTER:
                waits.append(pair[-LOCK_AFTER] + LOCK_S - now)
            if len(addr) >= ADDRESS_FAILS:
                waits.append(addr[-ADDRESS_FAILS] + LOCK_S - now)
            return max(waits, default=0.0)

    def _failed(self, name: str, address: str, now: float) -> None:
        with self._fail_lock:
            self._fails.setdefault((name, address), []).append(now)
            self._addr_fails.setdefault(address, []).append(now)

    def login(self, name: str, password: str, address: str = "") -> tuple[str, Session]:
        now = self.clock()
        wait = self._locked(name, address, now)
        if wait > 0:
            raise AuthError(f"로그인 잠김: {int(wait) + 1}초 뒤 다시 시도", 429)
        with self.store.lock:
            row = self.store.db.execute("SELECT role, salt, hash, iterations FROM users WHERE name=?",
                                        (name,)).fetchone()
        # the hash runs outside the store lock (the control loop and every request use that lock) and only a
        # couple at a time; an unknown account costs the same, so timing does not tell which names exist
        if not self._slots.acquire(timeout=HASH_WAIT_S):
            raise AuthError("로그인 요청이 많습니다. 잠시 뒤 다시 시도하세요", 503)
        try:
            if row is None:
                _hash_password(password, b"0" * 16, ITERATIONS)
                ok, role = False, ""
            else:
                role, salt, digest, iterations = row
                ok = hmac.compare_digest(_hash_password(password, salt, iterations), digest)
        finally:
            self._slots.release()
        if not ok:
            self._failed(name, address, now)
            raise AuthError("계정 또는 비밀번호가 맞지 않습니다")
        with self._fail_lock:
            self._fails.pop((name, address), None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.store.lock:
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

    def revoke_api_keys(self, name: str) -> int:
        with self.store.lock:
            return self.store.db.execute("DELETE FROM api_keys WHERE name=?", (name,)).rowcount

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


LOOPBACK_NAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}


def is_loopback_host(host_header: str | None) -> bool:
    """Host header names this machine (development mode answers nothing else: no DNS rebinding).

    >>> is_loopback_host("127.0.0.1:8095"), is_loopback_host("[::1]:8095"), is_loopback_host("evil.example:8095")
    (True, True, False)
    """
    if not host_header:
        return False
    name = host_header.rsplit(":", 1)[0] if not host_header.endswith("]") else host_header
    return name in LOOPBACK_NAMES


def check_origin(headers, host_header: str | None) -> None:
    """A browser request that changes state: JSON body and, when the browser names its origin, this host.

    A form on another site can only send text/plain or form bodies, and every cross-site fetch carries an
    Origin, so these two checks keep other sites out even before the CSRF token is looked at.
    """
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise AuthError("Content-Type은 application/json이어야 합니다", 415)
    origin = headers.get("Origin")
    if origin and origin != "null":
        host = origin.split("://", 1)[-1].rstrip("/")
        if host_header is None or host != host_header:
            raise AuthError("다른 출처(Origin)에서 온 요청은 거부합니다", 403)


def check_state_change(headers, session: Session, host_header: str | None) -> None:
    """CSRF defences for a state-changing request from a browser session."""
    check_origin(headers, host_header)
    if not hmac.compare_digest(headers.get("X-CSRF-Token") or "", session.csrf):
        raise AuthError("CSRF 토큰이 없거나 맞지 않습니다", 403)
