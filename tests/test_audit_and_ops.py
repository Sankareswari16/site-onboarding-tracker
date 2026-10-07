"""Audit trail integrity, escalation, notifications, and live reporting."""
import sqlite3

import pytest

from app import audit, db, outbox
from conftest import decide, h, submit


def _conn(db_path):
    return db.connect(db_path)


# ------------------------------------------------------------------- audit --

def test_audit_records_who_what_when(client):
    site = submit(client)
    decide(client, site["id"], "legal1", comment="Contract terms acceptable")
    decide(client, site["id"], "compliance", "reject", "GCP training certificate expired")

    events = client.get(f"/sites/{site['id']}/audit", headers=h("admin")).json()["events"]
    assert [e["event_type"] for e in events] == ["site_submitted", "stage_approved", "stage_rejected"]
    assert events[1]["actor_email"] == "legal1@example.org"
    assert events[2]["actor_email"] == "compliance1@example.org"
    assert events[2]["payload"]["comment"] == "GCP training certificate expired"
    assert events[2]["payload"]["from_status"] == "PENDING_COMPLIANCE" and events[2]["payload"]["to_status"] == "REJECTED"
    assert all(e["occurred_at"] for e in events)


def test_audit_is_append_only_at_the_database_level(client, db_path):
    site = submit(client)
    decide(client, site["id"], "legal1")  # ensures an approvals row exists to attack
    conn = _conn(db_path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET event_type = 'tampered'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE approvals SET decision = 'rejected'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM approvals")
    conn.close()


def test_hash_chain_detects_tampering_even_by_a_privileged_user(client, db_path):
    a = submit(client, 1)
    submit(client, 2)
    decide(client, a["id"], "legal1")
    assert client.get("/audit/verify", headers=h("admin")).json() == {
        "ok": True, "entries_checked": 3, "first_bad_entry_id": None}

    # Someone with DB admin rights drops the trigger and rewrites history.
    conn = _conn(db_path)
    conn.execute("DROP TRIGGER audit_log_no_update")
    conn.execute("UPDATE audit_log SET payload = replace(payload, 'approved', 'rejected') WHERE id = 3")
    conn.execute("UPDATE audit_log SET actor_id = 'u-admin-1' WHERE id = 3")
    conn.close()

    result = client.get("/audit/verify", headers=h("admin")).json()
    assert result["ok"] is False and result["first_bad_entry_id"] == 3


# -------------------------------------------------------------- escalation --

def test_overdue_stage_escalates_once(client, clock, db_path):
    site = submit(client)
    clock.advance(hours=71)
    assert client.post("/admin/escalations/run", headers=h("admin")).json()["escalated"] == 0

    clock.advance(hours=2)  # 73h > 72h legal SLA
    tasks = client.get("/my/tasks", headers=h("legal1")).json()["tasks"]
    assert tasks[0]["overdue"] is True
    r = client.post("/admin/escalations/run", headers=h("admin")).json()
    assert r["escalated"] == 1 and r["sites"][0]["stage"] == "legal"
    # Idempotent: running again does not re-escalate or spam.
    assert client.post("/admin/escalations/run", headers=h("admin")).json()["escalated"] == 0

    events = client.get(f"/sites/{site['id']}/audit", headers=h("admin")).json()["events"]
    assert events[-1]["event_type"] == "stage_escalated" and events[-1]["actor_id"] is None


def test_sla_clock_restarts_when_stage_advances(client, clock):
    site = submit(client)
    clock.advance(hours=70)
    decide(client, site["id"], "legal1")  # compliance clock starts now
    clock.advance(hours=70)
    assert client.post("/admin/escalations/run", headers=h("admin")).json()["escalated"] == 0
    clock.advance(hours=3)
    assert client.post("/admin/escalations/run", headers=h("admin")).json()["escalated"] == 1


# ----------------------------------------------------------- notifications --

def test_notifications_are_queued_atomically_and_delivered(client, db_path):
    site = submit(client)
    decide(client, site["id"], "legal1")
    conn = _conn(db_path)
    queued = conn.execute("SELECT recipient_value, subject FROM outbox ORDER BY id").fetchall()
    conn.close()
    assert [q["recipient_value"] for q in queued] == ["legal", "compliance"]

    r = client.post("/admin/outbox/flush", headers=h("admin")).json()
    assert r == {"sent": 2, "failed": 0, "pending_before": 2}
    assert client.post("/admin/outbox/flush", headers=h("admin")).json()["pending_before"] == 0


def test_lost_notification_cannot_lose_the_task(db_path, clock, client):
    """The old failure: a filtered email silently stalled onboarding. Now the task lives in the queue."""
    class BrokenTransport:
        def send(self, recipients, subject, body):
            raise ConnectionError("mail relay down")

    site = submit(client)
    conn = _conn(db_path)
    result = outbox.flush(conn, BrokenTransport(), clock())
    assert result["failed"] == 1 and result["sent"] == 0
    row = conn.execute("SELECT attempts, last_error, sent_at FROM outbox").fetchone()
    assert row["attempts"] == 1 and "relay down" in row["last_error"] and row["sent_at"] is None
    conn.close()
    # Approver still sees the pending task and can act on it.
    assert client.get("/my/tasks", headers=h("legal1")).json()["count"] == 1
    assert decide(client, site["id"], "legal1").status_code == 200


# --------------------------------------------------------------- reporting --

def test_report_is_live_not_a_snapshot(client, clock):
    a, b = submit(client, 1), submit(client, 2)
    submit(client, 3, "coord_eu", region="EU")
    r1 = client.get("/reports/summary", headers=h("leadership")).json()
    assert r1["total_sites"] == 3 and r1["by_status"] == {"PENDING_LEGAL": 3}

    decide(client, a["id"], "legal1")
    decide(client, b["id"], "legal1", "reject", "Sanctions screening hit")
    clock.advance(hours=80)
    r2 = client.get("/reports/summary", headers=h("leadership")).json()  # reflects the change immediately
    assert r2["by_status"] == {"PENDING_LEGAL": 1, "PENDING_COMPLIANCE": 1, "REJECTED": 1}
    assert r2["by_region"]["EU"] == {"PENDING_LEGAL": 1}
    assert r2["pending_by_stage"]["legal"] == {"waiting": 1, "overdue": 1}
    assert r2["longest_waiting"][0]["hours_waiting"] == 80.0
