"""
Notifications via a transactional outbox.

Messages are written to the `outbox` table in the SAME transaction as the
state change that caused them, so a notification can never be lost or
describe a change that was rolled back. A separate step delivers them.

Delivery is notification-only: a delayed, filtered or missed message cannot
change workflow state. The task stays in the approver's queue regardless.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol

from .clock import iso
from .logging_utils import get_logger, log

logger = get_logger("outbox")


class Notifier(Protocol):
    def send(self, recipients: list[str], subject: str, body: str) -> None: ...


class LogNotifier:
    """Stand-in transport. Production would use SES/SendGrid and Slack/Teams."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, recipients: list[str], subject: str, body: str) -> None:
        self.sent.append({"recipients": recipients, "subject": subject})
        log(logger, "notification_sent", recipients=recipients, subject=subject)


def enqueue(conn: sqlite3.Connection, *, site_id, recipient_type: str, recipient_value: str,
            subject: str, body: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO outbox (site_id, recipient_type, recipient_value, subject, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (site_id, recipient_type, recipient_value, subject, body, iso(now)),
    )


def _resolve(conn: sqlite3.Connection, recipient_type: str, value: str) -> list[str]:
    if recipient_type == "role":
        rows = conn.execute("SELECT email FROM users WHERE role = ? AND active = 1", (value,)).fetchall()
    else:
        rows = conn.execute("SELECT email FROM users WHERE id = ? AND active = 1", (value,)).fetchall()
    return [r["email"] for r in rows]


def flush(conn: sqlite3.Connection, notifier: Notifier, now: datetime) -> dict:
    """Deliver pending messages. Failures are recorded and retried on the next flush."""
    sent = failed = 0
    pending = conn.execute("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id").fetchall()
    for m in pending:
        try:
            recipients = _resolve(conn, m["recipient_type"], m["recipient_value"])
            if not recipients:
                raise RuntimeError(f"no active recipients for {m['recipient_type']}={m['recipient_value']}")
            notifier.send(recipients, m["subject"], m["body"])
            conn.execute("UPDATE outbox SET sent_at = ?, attempts = attempts + 1 WHERE id = ?", (iso(now), m["id"]))
            sent += 1
        except Exception as exc:  # noqa: BLE001 - recorded, retried next flush
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?", (str(exc), m["id"])
            )
            failed += 1
            log(logger, "notification_failed", outbox_id=m["id"], error=str(exc))
    return {"sent": sent, "failed": failed, "pending_before": len(pending)}
