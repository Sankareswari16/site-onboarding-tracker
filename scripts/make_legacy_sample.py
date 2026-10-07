"""Create a realistic, messy 'legacy' workbook (fictional data) to exercise the migration."""
import os
import random
import sys

import openpyxl

random.seed(7)

HEADERS = ["Site Name", "Investigator", "Region", "Submitted On", "Status", "Country", "Contact Email", "Notes"]

CLEAN_STATUSES = ["Approved", "APPROVED", "ok — approved", "Pending Legal", "pending - compliance",
                  "waiting for site ops", "legal approved", "Legal approved, waiting Compliance",
                  "legal + compliance approved", "Rejected", "Legal rejected", "submitted"]
AMBIGUOUS = ["approved pending updated insurance cert", "approved (see note below)", "TBD", "", "looks fine?"]
REGIONS = [("NA", "US"), ("North America", "CA"), ("EU", "DE"), ("Europe", "FR"), ("APAC", "JP"), ("LATAM", "BR")]
CITIES = ["Riverside", "Lakeshore", "Northgate", "Summit", "Harborview", "Eastwood", "Maple Grove", "Cedar Point"]
FIRST = ["Alex", "Priya", "Jonas", "Mei", "Carlos", "Fatima", "Tomas", "Hannah", "Kenji", "Olga"]
LAST = ["Morgan", "Raman", "Weber", "Chen", "Alvarez", "Haddad", "Novak", "Fischer", "Sato", "Petrova"]


def build(path: str, n: int = 40) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS)
    rows = []
    for i in range(n):
        region, country = random.choice(REGIONS)
        status = random.choice(CLEAN_STATUSES if i % 6 else AMBIGUOUS)
        rows.append([f"{random.choice(CITIES)} Clinical Research {i}", f"Dr. {random.choice(FIRST)} {random.choice(LAST)}",
                     region, f"2025-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}", status, country,
                     f"site{i}@example.org", ""])
    # The double-click problem: exact duplicates and trivially varied duplicates.
    rows.append(list(rows[3]))
    dup = list(rows[8]); dup[0] = dup[0].upper(); dup[1] = dup[1].lower()
    rows.append(dup)
    # Dirty data the old workbook happily accepted.
    rows.append(["Orphan Site", "Dr. No Email", "EU", "2025-03-03", "Approved", "DE", "", ""])
    rows.append(["Atlantis Research", "Dr. Lost", "Atlantis", "2025-03-04", "Approved", "DE", "a@example.org", ""])
    rows.append(["Bad Country Site", "Dr. Typo", "NA", "2025-03-05", "Pending Legal", "USA", "b@example.org", ""])
    for r in rows:
        ws.append(r)
    wb.save(path)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "legacy_workbook.xlsx"
    build(out)
    print(f"wrote {out}")
