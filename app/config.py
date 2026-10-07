"""Static configuration for the onboarding workflow."""
import os

# Approval stages, in the order they must be completed. Each stage is owned
# by exactly one role of the same name.
STAGES = ["legal", "compliance", "site_ops"]

ROLES = {"coordinator", "legal", "compliance", "site_ops", "leadership", "admin"}
APPROVER_ROLES = set(STAGES)

REGIONS = ["NA", "EU", "APAC", "LATAM", "MEA"]

STATUS_PENDING = {stage: f"PENDING_{stage.upper()}" for stage in STAGES}
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"

# How long a stage may sit before it is escalated, per stage.
SLA_HOURS = {"legal": 72, "compliance": 72, "site_ops": 48}

DEFAULT_DB_PATH = os.environ.get("SITE_DB_PATH", "site_onboarding.db")
