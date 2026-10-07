"""Migration from the legacy workbook: map confidently, quarantine honestly, never fabricate."""
import os
import sys

import openpyxl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from app import audit, db
from app.legacy import migrate, normalize_region, normalize_status
from conftest import FakeClock
from make_legacy_sample import HEADERS, build


@pytest.mark.parametrize("text,expected", [
    ("Approved", "APPROVED"), ("APPROVED", "APPROVED"), ("ok — approved", "APPROVED"),
    ("Pending Legal", "PENDING_LEGAL"), ("pending - compliance", "PENDING_COMPLIANCE"),
    ("waiting for site ops", "PENDING_SITE_OPS"), ("legal approved", "PENDING_COMPLIANCE"),
    ("Legal approved, waiting Compliance", "PENDING_COMPLIANCE"), ("legal + compliance approved", "PENDING_SITE_OPS"),
    ("Legal, Compliance and Site Ops approved", "APPROVED"), ("Rejected", "REJECTED"), ("legal rejected", "REJECTED"),
])
def test_status_mapping(text, expected):
    assert normalize_status(text)[0] == expected


@pytest.mark.parametrize("text", [
    "approved pending updated insurance cert", "approved (see note below)", "TBD", "", None, "looks fine?",
])
def test_ambiguous_status_is_quarantined_not_guessed(text):
    status, reason = normalize_status(text)
    assert status is None and reason


def test_region_aliases():
    assert normalize_region("North America") == "NA" and normalize_region(" europe ") == "EU"
    assert normalize_region("Atlantis") is None


def _write(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS)
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_migration_end_to_end(tmp_path, db_path):
    wb = str(tmp_path / "legacy.xlsx")
    build(wb)
    db.init_db(db_path)
    conn = db.connect(db_path)
    now = FakeClock()()

    report = migrate(conn, wb, now)
    s = report.summary()
    assert s["rows"] == 45 and s["imported"] + s["quarantined"] == 45 and s["already_imported"] == 0
    reasons = " | ".join(o.reason for o in report.outcomes if o.outcome == "quarantined")
    # Every kind of bad row is quarantined WITH an explanation.
    for expected in ("exact duplicate", "already being onboarded", "missing required", "unrecognized region", "not an ISO alpha-2",
                     "conditional or qualified", "blank status", "unrecognized status"):
        assert expected in reasons, expected
    assert all(o.reason for o in report.outcomes)

    # Provenance, not fabrication.
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    ev = conn.execute("SELECT payload FROM audit_log WHERE event_type='legacy_imported' LIMIT 1").fetchone()
    assert "legacy_status_text" in ev["payload"] and "not recorded" in ev["payload"]
    assert audit.verify_chain(conn)["ok"]

    # SLA clock for in-flight sites starts at import, so nothing is instantly overdue.
    assert conn.execute(
        "SELECT COUNT(*) FROM sites WHERE current_stage IS NOT NULL AND stage_due_at <= ?", (now.isoformat(),)
    ).fetchone()[0] == 0

    # Re-running is idempotent: nothing new is imported.
    again = migrate(conn, wb, now)
    assert again.count("imported") == 0 and again.count("already_imported") == s["imported"]
    conn.close()


def test_exact_and_case_variant_duplicates_collapse_to_one_site(tmp_path, db_path):
    base = ["Riverside Clinical Research", "Dr. Alex Morgan", "NA", "2025-01-01", "Pending Legal", "US", "a@example.org", ""]
    variant = ["RIVERSIDE  clinical research", "dr alex morgan", "North America", "2025-01-02", "Pending Legal", "US", "a@example.org", ""]
    wb = str(tmp_path / "dups.xlsx")
    _write(wb, [base, list(base), variant])
    db.init_db(db_path)
    conn = db.connect(db_path)
    report = migrate(conn, wb, FakeClock()())
    assert report.count("imported") == 1 and report.count("quarantined") == 2
    assert conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 1
    conn.close()


def test_migrated_sites_continue_through_the_normal_workflow(tmp_path, db_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from conftest import decide, h

    wb = str(tmp_path / "w.xlsx")
    _write(wb, [["Lakeshore Research", "Dr. Priya Raman", "EU", "2025-02-02", "legal approved", "DE", "p@example.org", ""]])
    client = TestClient(create_app(db_path=db_path, clock=FakeClock()))
    conn = db.connect(db_path)
    migrate(conn, wb, FakeClock()())
    conn.close()

    sid = client.get("/sites", headers=h("admin")).json()["results"][0]["id"]
    assert client.get(f"/sites/{sid}", headers=h("admin")).json()["status"] == "PENDING_COMPLIANCE"
    assert decide(client, sid, "compliance").json()["status"] == "PENDING_SITE_OPS"
    assert decide(client, sid, "site_ops").json()["status"] == "APPROVED"
    assert audit.verify_chain(db.connect(db_path))["ok"]
