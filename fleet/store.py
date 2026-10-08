"""Durable state of the control room in one SQLite file.

What must survive a restart of the fleet manager:

- tasks (queued and active) and the WMS idempotency keys, so an upper system's
  retry after the restart neither loses nor duplicates an order;
- the WMS event outbox, read by the upper system with a cursor;
- per-vehicle order snapshots (route, released base, last order message), from
  which the manager rebuilds its reservations before it releases anything new;
- the audit log, hash-chained: every entry carries the SHA-256 of the previous
  one, so a deleted or edited entry breaks the chain (``verify_audit``);
- operator accounts and sessions (``fleet.auth``).

WAL mode, one connection shared under a lock (the HTTP threads and the control
loop both write). Standard library only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
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


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        with self.lock:
            if self.path != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)

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
            self.db.execute("INSERT INTO idem(order_id, body, task) VALUES (?, ?, ?)", (order_id, _dump(body), task_id))

    # ------------------------------------------------------------ vehicle snapshots
    def save_robot(self, serial: str, snapshot: dict) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO robots(serial, body, updated) VALUES (?, ?, ?)",
                            (serial, _dump(snapshot), time.time()))

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
            digest = chain_hash(prev, body)
            self.db.execute("INSERT INTO audit(body, prev, hash) VALUES (?, ?, ?)", (body, prev, digest))
        return {**entry, "hash": digest[:12]}

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
        for seq, body, stored_prev, digest in rows:
            if stored_prev != prev:
                problems.append(f"entry {seq}: chain broken before it (previous hash does not match)")
            if chain_hash(stored_prev, body) != digest:
                problems.append(f"entry {seq}: content does not match its hash")
            prev = digest
        return problems
