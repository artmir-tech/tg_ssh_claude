"""SQLite registry: sessions (Telegram topic <-> Claude session) and turns (per-session queue).

The service is a single asyncio process, so one connection is enough. Every write commits
immediately, which is what makes a crash/restart lose nothing but in-flight Claude runs.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# Session statuses (see README for the state machine).
NEW, IDLE, QUEUED, RUNNING = "NEW", "IDLE", "QUEUED", "RUNNING"
WAITING, WAITING_APPROVAL = "WAITING", "WAITING_APPROVAL"
ERROR, STOPPED, ARCHIVED = "ERROR", "STOPPED", "ARCHIVED"
ACTIVE_STATUSES = (RUNNING, WAITING, WAITING_APPROVAL)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id           INTEGER NOT NULL,
    topic_id          INTEGER NOT NULL,
    claude_session_id TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    title_source      TEXT NOT NULL DEFAULT 'user',   -- user | auto | placeholder
    title_synced      INTEGER NOT NULL DEFAULT 0,     -- title written to the Claude session
    cwd               TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'NEW',
    started           INTEGER NOT NULL DEFAULT 0,     -- Claude transcript exists
    archived          INTEGER NOT NULL DEFAULT 0,
    current_pid       INTEGER,
    last_error        TEXT,
    tags              TEXT NOT NULL DEFAULT '',
    model             TEXT,
    permission_mode   TEXT NOT NULL DEFAULT 'default',
    grants            TEXT NOT NULL DEFAULT '[]',     -- "allow for this session" decisions
    pending_files     TEXT NOT NULL DEFAULT '[]',     -- uploaded files waiting for a prompt
    origin            TEXT NOT NULL DEFAULT 'telegram',  -- telegram | import | fork
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    last_activity_at  REAL NOT NULL,
    run_started_at    REAL,
    UNIQUE (chat_id, topic_id)
);
CREATE TABLE IF NOT EXISTS turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions(id),
    chat_id       INTEGER,
    message_id    INTEGER,          -- the user's Telegram message (NULL for Retry/Continue)
    prompt        TEXT NOT NULL,
    status        TEXT NOT NULL,    -- queued | running | done | error | stopped | interrupted | cancelled
    status_msg_id INTEGER,          -- bot message showing queue/progress for this turn
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    error         TEXT,
    details       TEXT,
    cost_usd      REAL,
    duration_ms   INTEGER,
    UNIQUE (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS turns_queue ON turns(status, id);
CREATE TABLE IF NOT EXISTS topic_names (
    chat_id  INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    name     TEXT NOT NULL,
    PRIMARY KEY (chat_id, topic_id)
);
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY,
    at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Session:
    id: int
    chat_id: int
    topic_id: int
    claude_session_id: str
    title: str
    title_source: str
    title_synced: int
    cwd: str
    status: str
    started: int
    archived: int
    current_pid: int | None
    last_error: str | None
    tags: str
    model: str | None
    permission_mode: str
    grants: str
    pending_files: str
    origin: str
    created_at: float
    updated_at: float
    last_activity_at: float
    run_started_at: float | None
    control_msg_id: int | None = None   # pinned "control panel" message in the topic
    send_files: int = 1                 # files Claude writes are sent to the topic automatically

    @property
    def grant_list(self) -> list[dict[str, Any]]:
        return json.loads(self.grants or "[]")

    @property
    def pending_file_list(self) -> list[str]:
        return json.loads(self.pending_files or "[]")


@dataclass
class Turn:
    id: int
    session_id: int
    chat_id: int | None
    message_id: int | None
    prompt: str
    status: str
    status_msg_id: int | None
    created_at: float
    started_at: float | None
    finished_at: float | None
    error: str | None
    details: str | None
    cost_usd: float | None
    duration_ms: int | None


def _build(cls, row: sqlite3.Row | None):
    if row is None:
        return None
    names = {f.name for f in fields(cls)}
    return cls(**{k: row[k] for k in row.keys() if k in names})


class Registry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(sessions)")}
        if "control_msg_id" not in cols:   # added in v0.1.1
            self.conn.execute("ALTER TABLE sessions ADD COLUMN control_msg_id INTEGER")
        if "send_files" not in cols:       # added in v0.1.2
            self.conn.execute("ALTER TABLE sessions ADD COLUMN send_files INTEGER NOT NULL DEFAULT 1")
        if self.kv_get("perm_mode_follow") is None:   # v0.1.3: '' = follow PERMISSION_MODE from .env
            # before v0.1.3 every session got 'default' automatically (nobody chose it) -> follow the setting
            self.conn.execute("UPDATE sessions SET permission_mode='' WHERE permission_mode='default'")
            self.kv_set("perm_mode_follow", 1)
        path.chmod(0o600)

    def close(self) -> None:
        self.conn.close()

    # ---- key/value -------------------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # ---- Telegram update idempotency ----------------------------------------------------
    def mark_update(self, update_id: int) -> bool:
        """Record an update as seen. False means it was already processed (redelivery)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO processed_updates(update_id, at) VALUES(?, ?)", (update_id, time.time())
        )
        if cur.rowcount and update_id % 100 == 0:
            self.conn.execute("DELETE FROM processed_updates WHERE at < ?", (time.time() - 14 * 86400,))
        return cur.rowcount == 1

    # ---- topics seen before they have a session ------------------------------------------
    def set_topic_name(self, chat_id: int, topic_id: int, name: str) -> None:
        self.conn.execute(
            "INSERT INTO topic_names VALUES(?, ?, ?) ON CONFLICT(chat_id, topic_id) DO UPDATE SET name=excluded.name",
            (chat_id, topic_id, name),
        )

    def topic_name(self, chat_id: int, topic_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT name FROM topic_names WHERE chat_id=? AND topic_id=?", (chat_id, topic_id)
        ).fetchone()
        return row["name"] if row else None

    # ---- sessions -------------------------------------------------------------------------
    def create_session(self, *, chat_id: int, topic_id: int, claude_session_id: str, title: str,
                       cwd: str, title_source: str = "user", started: bool = False,
                       origin: str = "telegram", status: str = NEW, model: str | None = None) -> Session:
        now = time.time()
        cur = self.conn.execute(
            """INSERT INTO sessions(chat_id, topic_id, claude_session_id, title, title_source, cwd, status,
                                    started, origin, model, permission_mode, created_at, updated_at, last_activity_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'',?,?,?)""",
            (chat_id, topic_id, claude_session_id, title, title_source, cwd, status, int(started), origin,
             model, now, now, now),
        )
        return self.get_session(cur.lastrowid)

    def get_session(self, session_id: int) -> Session | None:
        return _build(Session, self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def session_by_topic(self, chat_id: int, topic_id: int) -> Session | None:
        return _build(Session, self.conn.execute(
            "SELECT * FROM sessions WHERE chat_id=? AND topic_id=?", (chat_id, topic_id)).fetchone())

    def session_by_claude_id(self, claude_session_id: str) -> Session | None:
        return _build(Session, self.conn.execute(
            "SELECT * FROM sessions WHERE claude_session_id=?", (claude_session_id,)).fetchone())

    def sessions(self, include_archived: bool = False) -> list[Session]:
        sql = "SELECT * FROM sessions" + ("" if include_archived else " WHERE archived=0")
        return [_build(Session, r) for r in self.conn.execute(sql + " ORDER BY last_activity_at DESC")]

    def update_session(self, session_id: int, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*values.values(), session_id))

    def touch(self, session_id: int) -> None:
        self.update_session(session_id, last_activity_at=time.time())

    # ---- turns (the per-session queue) -----------------------------------------------------
    def add_turn(self, session_id: int, prompt: str, chat_id: int | None = None,
                 message_id: int | None = None) -> Turn | None:
        """Queue a user turn. Returns None if this Telegram message was already queued."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO turns(session_id, chat_id, message_id, prompt, status, created_at)
               VALUES(?,?,?,?, 'queued', ?)""",
            (session_id, chat_id, message_id, prompt, time.time()),
        )
        return self.get_turn(cur.lastrowid) if cur.rowcount else None

    def get_turn(self, turn_id: int) -> Turn | None:
        return _build(Turn, self.conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone())

    def update_turn(self, turn_id: int, **values: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(f"UPDATE turns SET {cols} WHERE id=?", (*values.values(), turn_id))

    def turns_with_status(self, status: str) -> list[Turn]:
        return [_build(Turn, r) for r in self.conn.execute("SELECT * FROM turns WHERE status=? ORDER BY id", (status,))]

    def last_turn(self, session_id: int) -> Turn | None:
        return _build(Turn, self.conn.execute(
            "SELECT * FROM turns WHERE session_id=? ORDER BY id DESC LIMIT 1", (session_id,)).fetchone())

    def recent_errors(self, limit: int = 5) -> list[Turn]:
        return [_build(Turn, r) for r in self.conn.execute(
            "SELECT * FROM turns WHERE status='error' ORDER BY id DESC LIMIT ?", (limit,))]

    def count_turns(self, session_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM turns WHERE session_id=? AND status='done'",
                                 (session_id,)).fetchone()[0]

    def next_runnable_turn(self, busy_session_ids: set[int]) -> Turn | None:
        """Oldest queued turn (global FIFO) whose session has no active run."""
        busy = ",".join(str(i) for i in busy_session_ids) or "-1"
        return _build(Turn, self.conn.execute(f"""
            SELECT t.* FROM turns t JOIN sessions s ON s.id = t.session_id
            WHERE t.status='queued' AND s.archived=0 AND t.session_id NOT IN ({busy})
              AND t.id = (SELECT MIN(id) FROM turns WHERE session_id=t.session_id AND status='queued')
            ORDER BY t.id LIMIT 1""").fetchone())

    def queued_sessions_in_order(self) -> list[int]:
        """Session ids with queued turns, ordered by their oldest queued turn."""
        return [r[0] for r in self.conn.execute(
            """SELECT session_id FROM turns WHERE status='queued'
               GROUP BY session_id ORDER BY MIN(id)""")]

    def queued_count(self, session_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM turns WHERE session_id=? AND status='queued'",
                                 (session_id,)).fetchone()[0]

    def cancel_queued(self, session_id: int) -> list[Turn]:
        rows = [_build(Turn, r) for r in self.conn.execute(
            "SELECT * FROM turns WHERE session_id=? AND status='queued'", (session_id,))]
        self.conn.execute("UPDATE turns SET status='cancelled', finished_at=? WHERE session_id=? AND status='queued'",
                          (time.time(), session_id))
        return rows
