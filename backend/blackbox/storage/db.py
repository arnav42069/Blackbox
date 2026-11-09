from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def uid() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


class Database:
    """One serialized connection. Transactions never span an await or external IO."""

    def __init__(self, path: Path, recover: bool = True):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        PRAGMA foreign_keys=ON;
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at REAL, updated_at REAL,
            scope_json TEXT, status TEXT DEFAULT 'idle');
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions ON DELETE CASCADE,
            role TEXT, content TEXT, created_at REAL, attachment_ids_json TEXT, metadata_json TEXT);
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions ON DELETE CASCADE,
            path TEXT, mime TEXT, dimensions TEXT, size_bytes INTEGER, created_at REAL);
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions ON DELETE CASCADE,
            started_at REAL, ended_at REAL, status TEXT, counts_json TEXT, metrics_json TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(session_id) WHERE status='running';
        CREATE TABLE IF NOT EXISTS event_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT REFERENCES sessions ON DELETE CASCADE,
            run_id TEXT REFERENCES runs ON DELETE CASCADE, type TEXT, payload_json TEXT, created_at REAL);
        CREATE INDEX IF NOT EXISTS journal_session ON event_journal(session_id,id);
        CREATE TABLE IF NOT EXISTS tool_events (
            id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs ON DELETE CASCADE, tool_name TEXT,
            args_json TEXT, output_json TEXT, exit_code INTEGER, status TEXT, started_at REAL, ended_at REAL);
        CREATE TABLE IF NOT EXISTS model_capabilities (
            model_digest TEXT PRIMARY KEY, model_name TEXT, metadata_json TEXT, probe_json TEXT, tested_at REAL);
        """)
        if recover:
            stale = self.rows("SELECT id,session_id FROM runs WHERE status='running'")
            with self.transaction():
                self.conn.execute("UPDATE tool_events SET status='interrupted',ended_at=? WHERE status='running'", (now(),))
                self.conn.execute("UPDATE runs SET status='interrupted', ended_at=? WHERE status='running'", (now(),))
                self.conn.execute("UPDATE sessions SET status='interrupted' WHERE status='running'")
                self.conn.execute("UPDATE sessions SET scope_json=NULL")
                for run in stale:
                    for tool in self.rows("SELECT id,tool_name FROM tool_events WHERE run_id=? AND status='interrupted'", (run['id'],)):
                        self.event(run['session_id'], run['id'], 'tool_finished', {'tool_id': tool['id'], 'name': tool['tool_name'], 'status': 'interrupted', 'ok': False, 'summary': 'Backend restarted; command was not resumed'})
                    self.event(run['session_id'], run['id'], 'run_finished', {'reason': 'interrupted'})
        self.private_files()

    def private_files(self):
        for p in self.path.parent.glob(self.path.name + "*"):
            p.chmod(0o600)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def execute(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def create_session(self, model):
        sid = uid()
        self.execute("INSERT INTO sessions VALUES (?,?,?,?,?,NULL,'idle')", (sid, "New session", model, now(), now()))
        return self.session(sid)

    def session(self, sid):
        s = self.one("SELECT * FROM sessions WHERE id=?", (sid,))
        if s:
            s["scope"] = json.loads(s.pop("scope_json") or "null")
        return s

    def messages(self, sid, limit=100):
        rows = self.rows("SELECT * FROM (SELECT * FROM messages WHERE session_id=? ORDER BY created_at DESC LIMIT ?) ORDER BY created_at", (sid, limit))
        for row in rows:
            row["attachment_ids"] = json.loads(row.pop("attachment_ids_json"))
            row["metadata"] = json.loads(row.pop("metadata_json"))
        return rows

    def message(self, sid, role, content, attachments=None, metadata=None):
        mid = uid()
        self.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (mid, sid, role, content, now(), json.dumps(attachments or []), json.dumps(metadata or {})))
        self.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), sid))
        return mid

    def event(self, sid, rid, kind, payload):
        ts = now()
        cur = self.execute("INSERT INTO event_journal(session_id,run_id,type,payload_json,created_at) VALUES (?,?,?,?,?)", (sid, rid, kind, json.dumps(payload), ts))
        self.execute("DELETE FROM event_journal WHERE session_id=? AND id IN (SELECT id FROM event_journal WHERE session_id=? ORDER BY id DESC LIMIT -1 OFFSET 2000)", (sid, sid))
        return {"event_id": cur.lastrowid, "type": kind, "run_id": rid, "ts": ts, **payload}

    def events(self, sid, after=0):
        rows = self.rows("SELECT * FROM event_journal WHERE session_id=? AND id>? ORDER BY id LIMIT 2000", (sid, after))
        return [{"event_id": r["id"], "type": r["type"], "run_id": r["run_id"], "ts": r["created_at"], **json.loads(r["payload_json"])} for r in rows]

    def close(self):
        self.conn.close()
