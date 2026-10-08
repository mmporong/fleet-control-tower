"""Durable state of the control room in one SQLite file.

What must survive a restart of the fleet manager:

- tasks (queued and active) and the WMS idempotency keys, so an upper system's
  retry after the restart neither loses nor duplicates an order;
- the WMS event outbox, read by the upper system with a cursor;
- per-vehicle order snapshots (route, released base, last order message), from
  which the manager rebuilds its reservations before it releases anything new;
- the audit log, chained: every entry carries an HMAC-SHA256 over the previous
  entry's MAC and its own content, keyed outside the database (environment
  variable FCT_AUDIT_KEY, or a key file beside the database created on first
  use), so an edited, inserted or deleted entry breaks the chain and the chain
  cannot be recomputed without the key. The newest entry's sequence number and
  MAC are also written to a head file beside the database, which catches
  entries cut off the end (``verify_audit``). Both files sit on the same disk:
  for evidence that survives a compromised host, ship the head to an external
  log (syslog, WORM storage);
- operator accounts and sessions (``fleet.auth``).

WAL mode, one connection shared under a lock (the HTTP threads and the control
loop both write). Standard library only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, state TEXT NOT NULL, body TEXT NOT NULL, updated REAL);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL, prev TEXT NOT NULL,
                                  hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idem (order_id TEXT PRIMARY KEY, body TEXT NOT NULL, task TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS robots (serial TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL);
CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, role TEXT NOT NULL, salt BLOB NOT NULL, hash BLOB NOT NULL,
                                  iterations INTEGER NOT NULL, failed INTEGER NOT NULL DEFAULT 0,
                                  locked_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, name TEXT NOT NULL, csrf TEXT NOT NULL,
                                     created REAL NOT NULL, last REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
GENESIS = "0" * 64


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chain_hash(prev: str, body: str) -> str:
    """Hash of one audit entry: SHA-256 over the previous hash and the canonical entry.

    >>> chain_hash(GENESIS, "{}")[:12]
    '5508d2b710e6'
    """
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def entry_mac(key: bytes, prev: str, body: str) -> str:
    """MAC of one audit entry: HMAC-SHA256 over the previous entry's MAC and the canonical entry.

    >>> entry_mac(b"k" * 32, GENESIS, "{}")[:12]
    'e01ce7d31481'
    """
    return hmac.new(key, (prev + body).encode("utf-8"), hashlib.sha256).hexdigest()


def _audit_key(path: str) -> bytes:
    """FCT_AUDIT_KEY (hex), else ``<db>.auditkey`` (created with mode 0600), else a key for this process."""
    env = os.environ.get("FCT_AUDIT_KEY")
    if env:
        return bytes.fromhex(env)
    if path == ":memory:":
        return secrets.token_bytes(32)
    key_file = Path(path + ".auditkey")
    if not key_file.exists():
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return bytes.fromhex(key_file.read_text().strip())


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._audit_key = _audit_key(self.path)
        self._head_mem: tuple[int, str] | None = None        # head record of an in-memory database
        with self.lock:
            if self.path != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            # entries written before the keyed chain (plain SHA-256) are checked the old way
            if self.get_meta("audit_mac_from") is None:
                row = self.db.execute("SELECT MAX(seq) FROM audit").fetchone()
                self.set_meta("audit_mac_from", str((row[0] or 0) + 1))
            self._mac_from = int(self.get_meta("audit_mac_from"))

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------ tasks
    def save_task(self, task_json: dict) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO tasks(id, state, body, updated) VALUES (?, ?, ?, ?)",
                            (task_json["id"], task_json["state"], _dump(task_json), time.time()))

    def open_tasks(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT body FROM tasks WHERE state IN ('QUEUED', 'ACTIVE') ORDER BY id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def task_by_ref(self, ref: str) -> str | None:
        """Id of a stored task created for the upper system's order ``ref`` (idempotency after a crash)."""
        with self.lock:
            row = self.db.execute("SELECT id FROM tasks WHERE json_extract(body, '$.meta.ref') = ? ORDER BY id "
                                  "LIMIT 1", (ref,)).fetchone()
        return row[0] if row else None

    def task(self, task_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # ------------------------------------------------------------ WMS outbox
    def append_event(self, ev: dict) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO events(seq, body) VALUES (?, ?)", (ev["seq"], _dump(ev)))

    def events_after(self, seq: int, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT body FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def last_event_seq(self) -> int:
        with self.lock:
            row = self.db.execute("SELECT MAX(seq) FROM events").fetchone()
        return row[0] or 0

    # ------------------------------------------------------------ idempotency keys
    def idem_get(self, order_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT body, task FROM idem WHERE order_id=?", (order_id,)).fetchone()
        return {"body": json.loads(row[0]), "task": row[1]} if row else None

    def idem_put(self, order_id: str, body: dict, task_id: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO idem(order_id, body, task) VALUES (?, ?, ?)",
                            (order_id, _dump(body), task_id))

    # ------------------------------------------------------------ vehicle snapshots
    def save_robot(self, serial: str, snapshot: dict) -> None:
        self.save_robots({serial: snapshot})

    def save_robots(self, snapshots: dict[str, dict]) -> None:
        """Several vehicle snapshots in one transaction (one moment of the reservation table)."""
        now = time.time()
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany("INSERT OR REPLACE INTO robots(serial, body, updated) VALUES (?, ?, ?)",
                                    [(s, _dump(snap), now) for s, snap in snapshots.items()])
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def robots(self) -> dict[str, dict]:
        with self.lock:
            rows = self.db.execute("SELECT serial, body FROM robots").fetchall()
        return {s: json.loads(b) for s, b in rows}

    # ------------------------------------------------------------ audit (hash chain)
    def append_audit(self, entry: dict) -> dict:
        with self.lock:
            row = self.db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS
            body = _dump(entry)
            digest = entry_mac(self._audit_key, prev, body)
            seq = self.db.execute("INSERT INTO audit(body, prev, hash) VALUES (?, ?, ?)",
                                  (body, prev, digest)).lastrowid
            self._write_head(seq, digest)
        return {**entry, "hash": digest[:12]}

    def _head_path(self) -> Path | None:
        return None if self.path == ":memory:" else Path(self.path + ".audithead")

    def _write_head(self, seq: int, digest: str) -> None:
        path = self._head_path()
        if path is None:
            self._head_mem = (seq, digest)
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(f"{seq} {digest}\n")
        os.replace(tmp, path)

    def _read_head(self) -> tuple[int, str] | None:
        path = self._head_path()
        if path is None:
            return self._head_mem
        if not path.exists():
            return None
        seq, digest = path.read_text().split()
        return int(seq), digest

    def audit_tail(self, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT body, hash FROM audit ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [{**json.loads(b), "hash": h[:12]} for b, h in reversed(rows)]

    def verify_audit(self) -> list[str]:
        """Problems in the hash chain; an empty list means no entry was removed, inserted or edited."""
        problems = []
        prev = GENESIS
        with self.lock:
            rows = self.db.execute("SELECT seq, body, prev, hash FROM audit ORDER BY seq").fetchall()
            head = self._read_head()
        for seq, body, stored_prev, digest in rows:
            if stored_prev != prev:
                problems.append(f"entry {seq}: chain broken before it (previous hash does not match)")
            expected = entry_mac(self._audit_key, stored_prev, body) if seq >= self._mac_from \
                else chain_hash(stored_prev, body)
            if not hmac.compare_digest(expected, digest):
                problems.append(f"entry {seq}: content does not match its MAC")
            prev = digest
        if head is not None:
            last = rows[-1] if rows else None
            if last is None or last[0] < head[0]:
                problems.append(f"entries up to {head[0]} were written, the log ends at {last[0] if last else 0}")
            elif last[0] == head[0] and not hmac.compare_digest(last[3], head[1]):
                problems.append(f"entry {head[0]}: differs from the recorded head")
        return problems
