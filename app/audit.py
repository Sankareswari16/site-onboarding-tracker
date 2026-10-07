"""
Append-only, hash-chained audit log.

Every state transition writes one row. Each row stores the hash of the
previous row, so editing or deleting any historical entry breaks every hash
after it. The DB triggers already refuse UPDATE/DELETE; the chain exists so
that tampering by someone with direct database privileges is *detectable*.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from .clock import iso

GENESIS = "0" * 64


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(prev_hash: str, site_id, event_type: str, actor_id, payload_text: str, occurred_at: str) -> str:
    material = "|".join([prev_hash, str(site_id), event_type, str(actor_id), payload_text, occurred_at])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append(conn: sqlite3.Connection, *, site_id, event_type: str, actor_id, payload: dict, now: datetime) -> int:
    """Must be called inside a write transaction so the chain stays linear."""
    last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = last["hash"] if last else GENESIS
    payload_text = _canonical(payload)
    occurred_at = iso(now)
    h = _digest(prev_hash, site_id, event_type, actor_id, payload_text, occurred_at)
    cur = conn.execute(
        "INSERT INTO audit_log (site_id, event_type, actor_id, payload, occurred_at, prev_hash, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (site_id, event_type, actor_id, payload_text, occurred_at, prev_hash, h),
    )
    return cur.lastrowid


def for_site(conn: sqlite3.Connection, site_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT a.id, a.event_type, a.actor_id, u.email AS actor_email, a.payload, a.occurred_at, a.hash "
        "FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id "
        "WHERE a.site_id = ? ORDER BY a.id",
        (site_id,),
    ).fetchall()
    return [
        {
            "id": r["id"], "event_type": r["event_type"], "actor_id": r["actor_id"],
            "actor_email": r["actor_email"], "payload": json.loads(r["payload"]),
            "occurred_at": r["occurred_at"], "hash": r["hash"],
        }
        for r in rows
    ]


def verify_chain(conn: sqlite3.Connection) -> dict:
    """Recompute every hash from genesis; report the first broken link, if any."""
    prev = GENESIS
    checked = 0
    for r in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        expected = _digest(prev, r["site_id"], r["event_type"], r["actor_id"], r["payload"], r["occurred_at"])
        if r["prev_hash"] != prev or r["hash"] != expected:
            return {"ok": False, "entries_checked": checked, "first_bad_entry_id": r["id"]}
        prev = r["hash"]
        checked += 1
    return {"ok": True, "entries_checked": checked, "first_bad_entry_id": None}
