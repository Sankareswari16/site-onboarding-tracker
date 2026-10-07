"""
Storage layer (SQLite for the runnable assessment; the schema is plain SQL
and ports directly to PostgreSQL).

Concurrency model: every state-changing operation runs inside
`BEGIN IMMEDIATE`, which takes the write lock up front. Two coordinators or
approvers acting at the same moment are therefore serialized by the database
instead of silently overwriting each other, which is the failure the shared
Excel workbook had.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN
        ('coordinator','legal','compliance','site_ops','leadership','admin')),
    region TEXT,
    api_key_hash TEXT UNIQUE NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    natural_key TEXT NOT NULL,
    site_name TEXT NOT NULL,
    investigator_name TEXT NOT NULL,
    region TEXT NOT NULL,
    country TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('PENDING_LEGAL','PENDING_COMPLIANCE','PENDING_SITE_OPS','APPROVED','REJECTED')),
    current_stage TEXT CHECK (current_stage IN ('legal','compliance','site_ops')),
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stage_entered_at TEXT,
    stage_due_at TEXT,
    escalated_at TEXT,
    completed_at TEXT,
    source TEXT NOT NULL DEFAULT 'api'
);
-- Backstop against duplicate onboarding of the same site/investigator.
-- A rejected site may legitimately be resubmitted, so it is excluded.
CREATE UNIQUE INDEX IF NOT EXISTS ux_sites_natural_key_active
    ON sites(natural_key) WHERE status != 'REJECTED';
CREATE INDEX IF NOT EXISTS ix_sites_status ON sites(status);
CREATE INDEX IF NOT EXISTS ix_sites_region ON sites(region);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL REFERENCES sites(id),
    stage TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
    decided_by TEXT NOT NULL REFERENCES users(id),
    comment TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE (site_id, stage)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT,
    event_type TEXT NOT NULL,
    actor_id TEXT,
    payload TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_site ON audit_log(site_id);

-- Append-only enforcement at the database level.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_update BEFORE UPDATE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_delete BEFORE DELETE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;

-- Notifications are queued here (transactional outbox) and sent afterwards.
-- They never change workflow state; the decision API is the only way to do that.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT,
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('role','user')),
    recipient_value TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


def init_db(path: str) -> None:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.close()


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None -> autocommit; transactions are explicit (write_tx).
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
