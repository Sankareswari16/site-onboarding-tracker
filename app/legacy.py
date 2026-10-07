"""
One-time migration from the legacy Excel workbook.

Principles:
  * Free-text statuses are mapped by explicit, ordered rules. Anything the rules
    cannot map with confidence is QUARANTINED with a reason, never guessed.
  * Approver identities and decision timestamps do not exist in the legacy data,
    so none are fabricated: no `approvals` rows are created for migrated sites.
    The audit entry records the original status text as provenance instead.
  * Re-running is safe: each legacy row gets a deterministic idempotency key.
  * The SLA clock for migrated in-flight sites starts at import time, so a
    migration does not instantly escalate every historical site.
"""
from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import openpyxl

from . import audit
from .clock import iso
from .config import SLA_HOURS, STAGES, STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED
from .db import write_tx
from .service import natural_key

MIGRATION_USER_ID = "u-migration"

REGION_ALIASES = {
    "na": "NA", "north america": "NA", "n. america": "NA", "n america": "NA", "usa": "NA", "us": "NA",
    "eu": "EU", "europe": "EU", "emea": "EU",
    "apac": "APAC", "asia pacific": "APAC", "asia-pacific": "APAC",
    "latam": "LATAM", "latin america": "LATAM",
    "mea": "MEA", "middle east": "MEA",
}

_STAGE_RE = r"(legal|compliance|site[\s_-]?ops|ops)"
_CONDITIONAL = ("conditional", "subject to", "pending updated", "pending receipt", "see note", "but ", "except", "unless")


def _stage(word: str) -> str:
    """Map a free-text stage word ('Site Ops', 'site-ops', 'ops', 'Legal') to a stage id."""
    squashed = re.sub(r"[^a-z]", "", word.lower())
    return "site_ops" if squashed in ("siteops", "ops") else squashed


def normalize_status(text: str | None) -> tuple[str | None, str]:
    """Return (canonical_status, reason). status is None when the text cannot be mapped confidently."""
    if text is None or not str(text).strip():
        return None, "blank status"
    t = re.sub(r"\s+", " ", str(text).lower().replace("—", "-").replace("–", "-")).strip()

    if re.search(r"reject|declin|denied", t):
        return STATUS_REJECTED, "matched rejection keyword"
    if "approv" in t and any(m in t for m in _CONDITIONAL):
        return None, "conditional or qualified approval requires human judgement"

    wait = re.search(rf"(?:pending|waiting(?: for| on)?|awaiting|with)\W*{_STAGE_RE}", t)
    if wait:
        return STATUS_PENDING[_stage(wait.group(1))], "explicit 'pending <stage>' wording"

    # "<stage(s)> approved": every stage word before the word 'approved' counts as passed.
    before = t.split("approv")[0] if "approv" in t else ""
    passed = [_stage(m) for m in re.findall(_STAGE_RE, before)]
    if passed:
        highest = max(STAGES.index(s) for s in passed)
        if highest + 1 < len(STAGES):
            return STATUS_PENDING[STAGES[highest + 1]], f"approved through {STAGES[highest]}"
        return STATUS_APPROVED, "approved through final stage"

    if re.fullmatch(r"(ok\W+)?(fully |all |final )?approved\W*", t):
        return STATUS_APPROVED, "plain 'approved'"
    if t in ("pending", "in progress", "submitted", "new"):
        return STATUS_PENDING[STAGES[0]], "not yet started: treated as awaiting first stage"
    return None, "unrecognized status text"


def normalize_region(text: str | None) -> str | None:
    return REGION_ALIASES.get(re.sub(r"\s+", " ", str(text or "")).strip().lower())


@dataclass
class RowOutcome:
    row_number: int
    site_name: str
    raw_status: str
    outcome: str  # imported | quarantined | already_imported
    mapped_status: str = ""
    reason: str = ""


@dataclass
class MigrationReport:
    outcomes: list[RowOutcome] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for o in self.outcomes if o.outcome == outcome)

    def summary(self) -> dict:
        return {"rows": len(self.outcomes), "imported": self.count("imported"),
                "already_imported": self.count("already_imported"), "quarantined": self.count("quarantined")}

    def write_csv(self, path: str) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["row_number", "site_name", "raw_status", "outcome", "mapped_status", "reason"])
            for o in self.outcomes:
                w.writerow([o.row_number, o.site_name, o.raw_status, o.outcome, o.mapped_status, o.reason])


def _ensure_migration_user(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO users (id, email, name, role, region, api_key_hash, active) "
        "VALUES (?, 'migration@system.local', 'Legacy migration', 'admin', NULL, ?, 0)",
        (MIGRATION_USER_ID, hashlib.sha256(b"migration-user-cannot-log-in").hexdigest()),
    )


def _read_rows(path: str) -> list[dict]:
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    return [dict(zip(headers, r)) for r in rows[1:] if any(c is not None and str(c).strip() for c in r)]


def migrate(conn: sqlite3.Connection, workbook_path: str, now: datetime) -> MigrationReport:
    report = MigrationReport()
    seen_this_run: set[str] = set()
    with write_tx(conn):
        _ensure_migration_user(conn)

    for i, row in enumerate(_read_rows(workbook_path), start=2):  # row 1 is the header
        site = str(row.get("Site Name") or "").strip()
        inv = str(row.get("Investigator") or "").strip()
        raw_status = str(row.get("Status") or "").strip()
        country = str(row.get("Country") or "").strip().upper()
        email = str(row.get("Contact Email") or "").strip()
        submitted = str(row.get("Submitted On") or "").strip()

        def quarantine(reason: str, mapped: str = "") -> None:
            report.outcomes.append(RowOutcome(i, site, raw_status, "quarantined", mapped, reason))

        missing = [n for n, v in (("Site Name", site), ("Investigator", inv), ("Country", country),
                                  ("Contact Email", email)) if not v]
        if missing:
            quarantine(f"missing required field(s): {', '.join(missing)}")
            continue
        if len(country) != 2:
            quarantine(f"country '{country}' is not an ISO alpha-2 code")
            continue
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            quarantine(f"contact email '{email}' is not valid")
            continue
        region = normalize_region(row.get("Region"))
        if region is None:
            quarantine(f"unrecognized region '{row.get('Region')}'")
            continue
        status, why = normalize_status(raw_status)
        if status is None:
            quarantine(why)
            continue

        key = "legacy-" + hashlib.sha1(f"{site}|{inv}|{region}|{submitted}".encode()).hexdigest()[:20]
        if key in seen_this_run:
            quarantine("exact duplicate of an earlier row in this workbook", status)
            continue
        seen_this_run.add(key)
        nk = natural_key(site, inv, region)
        stage = next((s for s in STAGES if STATUS_PENDING[s] == status), None)
        ts = iso(now)

        try:
            with write_tx(conn):
                if conn.execute("SELECT 1 FROM sites WHERE idempotency_key = ?", (key,)).fetchone():
                    report.outcomes.append(RowOutcome(i, site, raw_status, "already_imported", status, "re-run"))
                    continue
                sid = "legacy-" + key[7:]
                conn.execute(
                    "INSERT INTO sites (id, idempotency_key, natural_key, site_name, investigator_name, region, country, "
                    "contact_email, status, current_stage, version, created_by, created_at, updated_at, "
                    "stage_entered_at, stage_due_at, completed_at, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?, 'legacy_import')",
                    (sid, key, nk, site, inv, region, country, email, status, stage, MIGRATION_USER_ID,
                     submitted or ts, ts, ts if stage else None,
                     iso(now + timedelta(hours=SLA_HOURS[stage])) if stage else None,
                     ts if stage is None else None),
                )
                audit.append(conn, site_id=sid, event_type="legacy_imported", actor_id=MIGRATION_USER_ID, now=now,
                             payload={"legacy_status_text": raw_status, "mapped_status": status, "mapping_rule": why,
                                      "legacy_row_number": i,
                                      "note": "approver identities and decision times were not recorded in the "
                                              "legacy system and are intentionally not reconstructed"})
            report.outcomes.append(RowOutcome(i, site, raw_status, "imported", status, why))
        except sqlite3.IntegrityError:
            quarantine("duplicate of a site already being onboarded (same site/investigator/region)", status)
    return report
