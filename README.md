# Investigator Site Onboarding Tracker — rebuild
## The six failures, and what fixes each

| Failure at ~150 sites | Root cause | What fixes it here | Proved by |
|---|---|---|---|
| Approval emails missed or filtered | State lived in an inbox | Approval is an authenticated API call. Email is notification only, queued in a transactional outbox; a lost message cannot lose the task, which stays in the approver's queue | `test_lost_notification_cannot_lose_the_task` |
| Race conditions between coordinators | Excel has no transactional locking | Every write runs in `BEGIN IMMEDIATE`; `UNIQUE(site_id, stage)` and a partial unique index on the site's natural key are backstops | `tests/test_concurrency.py` (8–12 simultaneous threads) |
| Status drifts from what was approved | A human transcribed threads into cells | Status is never edited. It is computed by the state machine from recorded decisions | `test_full_approval_path`, `test_rejection_halts_chain_immediately` |
| No audit trail | Excel/email have no structured history | Append-only `audit_log` (DB triggers block UPDATE/DELETE) plus a SHA-256 hash chain so even privileged tampering is detectable | `test_audit_records_who_what_when`, `test_hash_chain_detects_tampering...` |
| Stale monthly summary | Batch job read a file of unknown state | `/reports/summary` is a live query of the system of record, with a SLA/overdue view | `test_report_is_live_not_a_snapshot` |
| Anyone with the link sees everything | Shared-link access model | API-key auth + role/region-scoped visibility; unauthorized sites return 404, not 403, so existence isn't leaked | `tests/test_access_control.py` |

Two further failures from the earlier analysis are also covered: **duplicate
submissions** (idempotency keys + normalized natural-key uniqueness) and
**approvals that never get chased** (per-stage SLA with idempotent escalation).

## Workflow

```
PENDING_LEGAL -> PENDING_COMPLIANCE -> PENDING_SITE_OPS -> APPROVED
      \_______________ any rejection (comment required) ______________/--> REJECTED
```

- Only the role that owns the current stage can decide it. Skipping or reordering is impossible.
- A rejection stops the chain immediately; later stages are never asked.
- Admins and leadership cannot approve (separation of duties). Leadership is read-only.
- A stage is decided exactly once. A second approver gets `409` naming who decided and when.
- Optional `expected_version` gives optimistic concurrency for UIs.

## Running it

```bash
pip install -r requirements.txt

python3 -m pytest tests/ -v          # 54 tests
python3 scripts/demo.py              # narrated end-to-end walkthrough

# run the API (interactive docs at http://localhost:8000/docs)
uvicorn app.main:create_app --factory
```

Demo API keys (local use only, see `app/seed.py`): `demo-coord-na`, `demo-coord-eu`,
`demo-legal-1`, `demo-legal-2`, `demo-compliance-1`, `demo-siteops-1`,
`demo-leadership-1`, `demo-admin-1`. Pass as the `X-API-Key` header.

```bash
curl -X POST localhost:8000/sites -H "X-API-Key: demo-coord-na" -H "Content-Type: application/json" -d '{
  "idempotency_key":"my-key-0001","site_name":"Harborview Research","investigator_name":"Dr. Mei Chen",
  "region":"NA","country":"US","contact_email":"mei@example.org"}'
curl localhost:8000/my/tasks -H "X-API-Key: demo-legal-1"
curl -X POST localhost:8000/sites/<id>/decisions -H "X-API-Key: demo-legal-1" -H "Content-Type: application/json" \
  -d '{"decision":"approve","comment":"Contract reviewed"}'
```

## API

| Endpoint | Who | Purpose |
|---|---|---|
| `POST /sites` | coordinator (own region), admin | Idempotent submission. `201` new, `200` replay, `409` duplicate |
| `GET /sites`, `GET /sites/{id}` | role-scoped | Visibility enforced per role/region |
| `POST /sites/{id}/decisions` | stage owner | `approve` / `reject` (+ comment, optional `expected_version`) |
| `GET /sites/{id}/audit` | whoever can see the site | Who did what, when, with hashes |
| `GET /my/tasks` | approvers | Queue for their stage, oldest-due first, with `overdue` flag |
| `GET /reports/summary` | leadership, admin | Live counts by status/region/stage, overdue, longest waiting |
| `GET /audit/verify` | leadership, admin | Recomputes the whole hash chain |
| `POST /admin/escalations/run` | admin | Flag stages past SLA (idempotent) |
| `POST /admin/outbox/flush` | admin | Deliver queued notifications, retry failures |

## Migrating off the Excel workbook

```bash
python3 scripts/make_legacy_sample.py legacy_workbook.xlsx      # a deliberately messy sample
python3 scripts/migrate_legacy.py legacy_workbook.xlsx report.csv
```

On the included sample: **45 rows -> 33 imported, 12 quarantined**, each with a reason.

- Free-text statuses (`"ok — approved"`, `"legal approved, waiting Compliance"`) are mapped by explicit,
  ordered, tested rules. **Ambiguous text is quarantined, not guessed**: `"approved (see note below)"`
  and `"approved pending updated insurance cert"` need a human, because collapsing them is exactly how
  the old process lost nuance.
- Legacy duplicates (the double-click problem) are detected, including case/punctuation variants.
- **No approver identities or timestamps are fabricated.** The old system never recorded them. No
  `approvals` rows are created for migrated sites; the audit entry stores the original status text as provenance.
- Safe to re-run (deterministic keys). The SLA clock for in-flight sites starts at import so a migration
  does not instantly escalate every historical site.
- Rollout order and parallel running are in the design document (dual-run, then cut over intake, then approvers).

## What is real, and what is simplified

| Concern | In this repo | Production swap-in | Why the swap is low-risk |
|---|---|---|---|
| Database | SQLite (WAL, `BEGIN IMMEDIATE`) | PostgreSQL (row locks / `SERIALIZABLE`) | Plain SQL schema; partial unique index and constraints exist in Postgres. SQLite allows one writer at a time, which is fine here and not at large scale |
| Workflow engine | In-process state machine in `service.decide` (single transaction) | Temporal or Camunda for durable timers and richer human tasks | Transitions are already explicit, versioned and audited. The escalation sweep becomes a durable timer |
| Messaging | Transactional outbox + flush endpoint | Outbox relay to Kafka/SES/Slack | The outbox is the standard pattern for this; transport sits behind a `Notifier` protocol |
| Identity | Per-user API keys (hashed) | SSO / OIDC (Azure AD, Okta) | Everything downstream consumes only the resolved `actor`; the change is local to `app/auth.py` |
| Approval UI | None (OpenAPI docs + curl) | Small web app / Slack buttons | The API is the contract; email/Slack never change state |

I deliberately did not add Kafka and Temporal here. The problem at ~150 sites is correctness
(concurrency, audit, access), not throughput, and the runnable, testable core matters more than
infrastructure that a reviewer would have to stand up before seeing anything work.

## Design decisions worth questioning

- **404 instead of 403** for sites a caller cannot see, so existence is not leaked.
- **Rejection requires a comment.** The audit trail should say *why*, not just *who*.
- **Admins cannot approve.** Separation of duties; an override path would need its own audited, dual-control design.
- **Hash chain in addition to DB triggers.** Triggers stop the application; the chain makes tampering by a DB admin detectable (tested by dropping the trigger and rewriting a row).
- **Mutation-checked concurrency tests.** Weakening locking (`BEGIN IMMEDIATE` -> `BEGIN`) makes all three concurrency tests fail, so they detect broken locking, not just pass.

## Known limitations (honest list)

- **Duplicate detection is normalization-based, not fuzzy.** "Dr. J. Smith" vs "Dr John Smith" are different keys. A same-site-name/different-investigator submission returns a warning rather than a block. Fuzzy matching with a coordinator review step is the next improvement.
- **Escalation needs a scheduler.** `run_escalations` is idempotent and safe to call from cron/a timer; nothing calls it automatically here.
- **Notifications use a logging transport.** Delivery, retry bookkeeping and failure handling are real; the SMTP/Slack adapter is not.
- **Not a validated GxP system.** The audit trail, access control and append-only controls are the *foundations* regulated environments need, but this does not claim 21 CFR Part 11 compliance. That would additionally need e-signature binding, validation documentation, and controlled operational procedures.
- **SQLite single-writer.** Fine for this workload; use PostgreSQL beyond it.

## Layout

```
app/
  main.py        FastAPI app factory, routes, error mapping, request logging
  service.py     intake, approval state machine, escalation, reporting
  db.py          schema (constraints, append-only triggers), write transactions
  audit.py       hash-chained audit log + verification
  outbox.py      transactional outbox + delivery/retry
  auth.py        API-key auth and role/region visibility rules
  legacy.py      Excel migration: status mapping, quarantine, provenance
  schemas.py     request validation      config.py   stages/SLA/regions
scripts/         demo.py, make_legacy_sample.py, migrate_legacy.py
tests/           54 tests: workflow, intake, access control, concurrency, audit/ops, migration
```
