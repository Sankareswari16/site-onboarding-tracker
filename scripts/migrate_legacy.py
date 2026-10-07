"""Usage: python scripts/migrate_legacy.py <workbook.xlsx> [report.csv]"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import audit, db  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.config import DEFAULT_DB_PATH  # noqa: E402
from app.legacy import migrate  # noqa: E402
from app.seed import seed_demo_users  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    workbook = sys.argv[1]
    report_path = sys.argv[2] if len(sys.argv) > 2 else "migration_report.csv"

    db.init_db(DEFAULT_DB_PATH)
    conn = db.connect(DEFAULT_DB_PATH)
    seed_demo_users(conn)
    report = migrate(conn, workbook, utcnow())
    report.write_csv(report_path)

    print("migration summary:", report.summary())
    print("audit chain:", audit.verify_chain(conn))
    print(f"row-by-row report (including every quarantined row and why): {report_path}")
    for o in report.outcomes:
        if o.outcome == "quarantined":
            print(f"  QUARANTINED row {o.row_number}: {o.site_name!r} status={o.raw_status!r} -> {o.reason}")
