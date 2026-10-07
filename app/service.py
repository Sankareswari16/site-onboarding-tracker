"""
Core workflow service: intake, the approval state machine, escalation, reporting.

The state machine
-----------------
    PENDING_LEGAL -> PENDING_COMPLIANCE -> PENDING_SITE_OPS -> APPROVED
          \\________________ any rejection ________________/--> REJECTED

Status is never edited by hand. It is a consequence of recorded decisions,
which is what removes the "coordinator transcribes a reply-all thread into a
spreadsheet cell" step and with it the status drift.

Every function that changes state runs inside one write transaction that also
writes the audit entry and any notifications. Either all of it commits or none.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timedelta

from . import audit, outbox
from .auth import visibility_clause
from .clock import iso, parse
from .config import (SLA_HOURS, STAGES, STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED)
from .db import write_tx
from .errors import Conflict, Forbidden, NotFound
from .logging_utils import get_logger, log

logger = get_logger("service")


# ----------------------------------------------------------------- helpers --

def natural_key(site_name: str, investigator_name: str, region: str) -> str:
    """Normalized identity of a site, so trivial variations don't create duplicates."""
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return "|".join([norm(site_name), norm(investigator_name), region.upper()])


def _site_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _get_visible_site(conn: sqlite3.Connection, actor: dict, site_id: str) -> dict:
    clause, params = visibility_clause(actor)
    row = conn.execute(f"SELECT * FROM sites WHERE id = ? AND ({clause})", [site_id, *params]).fetchone()
    # Deliberately 404 (not 403) for sites the caller may not see: don't leak existence.
    if row is None:
        raise NotFound(f"site '{site_id}' not found")
    return _site_dict(row)


def _due(stage: str, now: datetime) -> str:
    return iso(now + timedelta(hours=SLA_HOURS[stage]))


# ------------------------------------------------------------------ intake --

def create_site(conn: sqlite3.Connection, actor: dict, data: dict, now: datetime) -> tuple[dict, bool, list[dict]]:
    """
    Submit a new site. Returns (site, created, warnings).

    Idempotent on `idempotency_key`: replaying the same submission returns the
    existing record (created=False) instead of creating a duplicate, which is
    what fixes the double-click / retry duplicate.
    """
    if actor["role"] not in ("coordinator", "admin"):
        raise Forbidden("only coordinators and admins can submit sites")
    if actor["role"] == "coordinator" and data["region"] != actor["region"]:
        raise Forbidden(f"coordinators can only submit sites for their own region ({actor['region']})")

    nk = natural_key(data["site_name"], data["investigator_name"], data["region"])

    with write_tx(conn):
        existing = conn.execute("SELECT * FROM sites WHERE idempotency_key = ?", (data["idempotency_key"],)).fetchone()
        if existing:
            if existing["natural_key"] == nk and existing["created_by"] == actor["id"]:
                return _site_dict(existing), False, []
            raise Conflict("idempotency_key was already used for a different submission")

        dup = conn.execute(
            "SELECT id, status FROM sites WHERE natural_key = ? AND status != ?", (nk, STATUS_REJECTED)
        ).fetchone()
        if dup:
            raise Conflict(
                f"this site/investigator is already being onboarded as {dup['id']} (status {dup['status']})",
                existing_site_id=dup["id"],
            )

        # Softer signal: same site name in the same region under a different investigator.
        warnings = []
        site_only = natural_key(data["site_name"], "", data["region"]).split("|")[0]
        for r in conn.execute(
            "SELECT id, investigator_name, status FROM sites WHERE region = ? AND status != ?",
            (data["region"], STATUS_REJECTED),
        ):
            if natural_key(data["site_name"], r["investigator_name"], data["region"]).split("|")[0] == site_only:
                warnings.append({"type": "possible_duplicate_site_name", "site_id": r["id"],
                                 "detail": f"same site name already onboarded with investigator '{r['investigator_name']}'"})

        first = STAGES[0]
        site_id = str(uuid.uuid4())
        ts = iso(now)
        conn.execute(
            "INSERT INTO sites (id, idempotency_key, natural_key, site_name, investigator_name, region, country, "
            "contact_email, status, current_stage, version, created_by, created_at, updated_at, "
            "stage_entered_at, stage_due_at, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?, 'api')",
            (site_id, data["idempotency_key"], nk, data["site_name"].strip(), data["investigator_name"].strip(),
             data["region"], data["country"].upper(), data["contact_email"], STATUS_PENDING[first], first,
             actor["id"], ts, ts, ts, _due(first, now)),
        )
        audit.append(conn, site_id=site_id, event_type="site_submitted", actor_id=actor["id"], now=now,
                     payload={"site_name": data["site_name"], "investigator_name": data["investigator_name"],
                              "region": data["region"], "status": STATUS_PENDING[first]})
        outbox.enqueue(conn, site_id=site_id, recipient_type="role", recipient_value=first, now=now,
                       subject=f"Approval needed ({first}): {data['site_name']}",
                       body=f"A new site '{data['site_name']}' ({data['region']}) is waiting for your decision.")
        row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()

    log(logger, "site_submitted", site_id=site_id, actor=actor["email"], region=data["region"])
    return _site_dict(row), True, warnings


# ---------------------------------------------------------------- approval --

def decide(conn: sqlite3.Connection, actor: dict, site_id: str, decision: str, comment: str | None,
           expected_version: int | None, now: datetime) -> dict:
    """
    Record an approve/reject decision for the stage the site is currently at.

    Rules enforced here (not in the UI, not in an email thread):
      - the caller must hold the role that owns the current stage
      - a stage can be decided exactly once (UNIQUE(site_id, stage) is the backstop)
      - a rejection stops the workflow immediately; later stages are never asked
      - optional optimistic concurrency via expected_version
    """
    with write_tx(conn):
        site = _get_visible_site(conn, actor, site_id)

        prior = conn.execute(
            "SELECT a.decision, a.decided_at, u.email FROM approvals a JOIN users u ON u.id = a.decided_by "
            "WHERE a.site_id = ? AND a.stage = ?", (site_id, actor["role"])
        ).fetchone()
        if prior:
            raise Conflict(f"stage '{actor['role']}' was already {prior['decision']} by {prior['email']} "
                           f"at {prior['decided_at']}")

        if site["status"] in (STATUS_APPROVED, STATUS_REJECTED):
            raise Conflict(f"site is already finalized ({site['status']})")

        stage = site["current_stage"]
        if actor["role"] != stage:
            raise Forbidden(f"site is waiting on the '{stage}' stage; role '{actor['role']}' cannot decide it")

        if expected_version is not None and expected_version != site["version"]:
            raise Conflict(f"site changed since you loaded it (your version {expected_version}, "
                           f"current {site['version']}); reload and retry", current_version=site["version"])

        ts = iso(now)
        conn.execute(
            "INSERT INTO approvals (site_id, stage, decision, decided_by, comment, decided_at) VALUES (?,?,?,?,?,?)",
            (site_id, stage, "approved" if decision == "approve" else "rejected", actor["id"], comment, ts),
        )

        from_status = site["status"]
        if decision == "reject":
            to_status, next_stage = STATUS_REJECTED, None
        else:
            idx = STAGES.index(stage)
            if idx + 1 < len(STAGES):
                next_stage = STAGES[idx + 1]
                to_status = STATUS_PENDING[next_stage]
            else:
                next_stage, to_status = None, STATUS_APPROVED

        conn.execute(
            "UPDATE sites SET status = ?, current_stage = ?, version = version + 1, updated_at = ?, "
            "stage_entered_at = ?, stage_due_at = ?, escalated_at = NULL, completed_at = ? WHERE id = ?",
            (to_status, next_stage, ts, ts if next_stage else None, _due(next_stage, now) if next_stage else None,
             ts if next_stage is None else None, site_id),
        )
        audit.append(conn, site_id=site_id, event_type=f"stage_{'approved' if decision == 'approve' else 'rejected'}",
                     actor_id=actor["id"], now=now,
                     payload={"stage": stage, "comment": comment, "from_status": from_status,
                              "to_status": to_status, "version_after": site["version"] + 1})

        name = site["site_name"]
        if next_stage:
            outbox.enqueue(conn, site_id=site_id, recipient_type="role", recipient_value=next_stage, now=now,
                           subject=f"Approval needed ({next_stage}): {name}",
                           body=f"'{name}' passed {stage} and is now waiting for {next_stage}.")
        else:
            outcome = "approved" if to_status == STATUS_APPROVED else f"rejected at {stage}"
            outbox.enqueue(conn, site_id=site_id, recipient_type="user", recipient_value=site["created_by"], now=now,
                           subject=f"Onboarding {outcome}: {name}",
                           body=f"Final outcome for '{name}': {to_status}." + (f" Reason: {comment}" if comment else ""))
        row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()

    log(logger, "decision_recorded", site_id=site_id, stage=stage, decision=decision, actor=actor["email"],
        to_status=to_status)
    return _site_dict(row)


# ------------------------------------------------------------------- reads --

def get_site(conn: sqlite3.Connection, actor: dict, site_id: str) -> dict:
    site = _get_visible_site(conn, actor, site_id)
    site["approvals"] = [
        dict(r) for r in conn.execute(
            "SELECT a.stage, a.decision, a.comment, a.decided_at, u.email AS decided_by_email "
            "FROM approvals a JOIN users u ON u.id = a.decided_by WHERE a.site_id = ? ORDER BY a.id", (site_id,))
    ]
    return site


def list_sites(conn: sqlite3.Connection, actor: dict, *, status: str | None = None, region: str | None = None,
               limit: int = 50, offset: int = 0) -> list[dict]:
    clause, params = visibility_clause(actor)
    sql, args = f"SELECT * FROM sites WHERE ({clause})", list(params)
    if status:
        sql += " AND status = ?"
        args.append(status)
    if region:
        sql += " AND region = ?"
        args.append(region)
    sql += " ORDER BY created_at, id LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [_site_dict(r) for r in conn.execute(sql, args)]


def my_tasks(conn: sqlite3.Connection, actor: dict, now: datetime) -> list[dict]:
    """The approver's queue: everything currently waiting on their stage, oldest-due first."""
    if actor["role"] not in STAGES:
        return []
    rows = conn.execute(
        "SELECT * FROM sites WHERE current_stage = ? ORDER BY stage_due_at, id", (actor["role"],)
    ).fetchall()
    tasks = []
    for r in rows:
        d = _site_dict(r)
        d["overdue"] = parse(d["stage_due_at"]) < now
        tasks.append(d)
    return tasks


# -------------------------------------------------------------- escalation --

def run_escalations(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    """
    Flag every site whose current stage has exceeded its SLA and notify the
    stage owners and admins. Idempotent: a site is escalated once per stage.
    In production this is a durable timer (e.g. a Temporal timer or a scheduled job).
    """
    escalated = []
    with write_tx(conn):
        rows = conn.execute(
            "SELECT * FROM sites WHERE current_stage IS NOT NULL AND escalated_at IS NULL AND stage_due_at < ?",
            (iso(now),),
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE sites SET escalated_at = ? WHERE id = ?", (iso(now), r["id"]))
            audit.append(conn, site_id=r["id"], event_type="stage_escalated", actor_id=None, now=now,
                         payload={"stage": r["current_stage"], "due_at": r["stage_due_at"]})
            for role in (r["current_stage"], "admin"):
                outbox.enqueue(conn, site_id=r["id"], recipient_type="role", recipient_value=role, now=now,
                               subject=f"OVERDUE ({r['current_stage']}): {r['site_name']}",
                               body=f"'{r['site_name']}' has been waiting on {r['current_stage']} past its SLA "
                                    f"(due {r['stage_due_at']}).")
            escalated.append({"site_id": r["id"], "stage": r["current_stage"], "due_at": r["stage_due_at"]})
    if escalated:
        log(logger, "escalations_raised", count=len(escalated))
    return escalated


# --------------------------------------------------------------- reporting --

def summary(conn: sqlite3.Connection, now: datetime) -> dict:
    """Live report straight from the system of record: no snapshot, no staleness."""
    by_status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM sites GROUP BY status")}
    by_region: dict = {}
    for r in conn.execute("SELECT region, status, COUNT(*) n FROM sites GROUP BY region, status"):
        by_region.setdefault(r["region"], {})[r["status"]] = r["n"]

    pending = conn.execute(
        "SELECT id, site_name, region, current_stage, stage_entered_at, stage_due_at FROM sites "
        "WHERE current_stage IS NOT NULL ORDER BY stage_entered_at"
    ).fetchall()
    stages = {s: {"waiting": 0, "overdue": 0} for s in STAGES}
    oldest = []
    for p in pending:
        stages[p["current_stage"]]["waiting"] += 1
        if parse(p["stage_due_at"]) < now:
            stages[p["current_stage"]]["overdue"] += 1
        oldest.append({
            "site_id": p["id"], "site_name": p["site_name"], "region": p["region"], "stage": p["current_stage"],
            "hours_waiting": round((now - parse(p["stage_entered_at"])).total_seconds() / 3600, 1),
        })
    oldest.sort(key=lambda x: -x["hours_waiting"])

    return {
        "generated_at": iso(now),
        "total_sites": sum(by_status.values()),
        "by_status": by_status,
        "by_region": by_region,
        "pending_by_stage": stages,
        "longest_waiting": oldest[:5],
    }
