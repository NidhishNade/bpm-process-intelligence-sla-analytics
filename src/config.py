"""
Shared configuration for the pipeline.

Single source of truth for paths, column semantics and business rules. Every rule
here traces back to a data-quality check in data/quality/dq_report.md; the check ID
is named in the comment so a reviewer can find the evidence.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
QUALITY_DIR = DATA_DIR / "quality"
SQL_DIR = ROOT / "sql"
DOCS_DIR = ROOT / "docs"

RAW_CSV = RAW_DIR / "incident_event_log.csv"
DUCKDB_PATH = DATA_DIR / "process_intelligence.duckdb"

# ---------------------------------------------------------------------------
# Source encoding
# ---------------------------------------------------------------------------

# The source exports unknown values as a literal '?'. Converting these to real
# nulls is the first transformation step; without it every distinct count and
# every join is wrong.
MISSING_SENTINEL = "?"

# Day-first European format, confirmed by DQ-04 (0 unparseable values across all
# five timestamp columns). Parsing month-first would silently corrupt every
# duration in the project, so the format is pinned rather than inferred.
TS_FORMAT = "%d/%m/%Y %H:%M"

# ---------------------------------------------------------------------------
# Process-mining column mapping
# ---------------------------------------------------------------------------

CASE_ID = "number"
ACTIVITY = "incident_state"
EVENT_TS = "sys_updated_at"

TIMESTAMP_COLUMNS = ["opened_at", "sys_created_at", "sys_updated_at", "resolved_at", "closed_at"]

BOOLEAN_COLUMNS = ["active", "made_sla", "knowledge", "u_priority_confirmation"]

INTEGER_COLUMNS = ["reassignment_count", "reopen_count", "sys_mod_count"]

# ---------------------------------------------------------------------------
# Process model
# ---------------------------------------------------------------------------

STATE_NEW = "New"
STATE_ACTIVE = "Active"
STATE_RESOLVED = "Resolved"
STATE_CLOSED = "Closed"

# States where the clock is running but the resolver team is blocked on a third
# party. Separating these from Active is the core of the bottleneck analysis:
# time lost waiting is a different management problem from time lost working.
WAITING_STATES = [
    "Awaiting User Info",
    "Awaiting Vendor",
    "Awaiting Problem",
    "Awaiting Evidence",
]

WORKING_STATES = [STATE_NEW, STATE_ACTIVE]
TERMINAL_STATES = [STATE_RESOLVED, STATE_CLOSED]

VALID_STATES = WORKING_STATES + WAITING_STATES + TERMINAL_STATES

# DQ-06: 5 events carry the sentinel state '-100'. It is a source-system artefact,
# not a process step. Left in, it renders as a phantom activity in the process map.
INVALID_STATES = ["-100"]

# Ordering used for consistent axis sorting in Power BI. Numbers are deliberately
# spaced so a new state can be inserted without renumbering.
STATE_SORT_ORDER = {
    STATE_NEW: 10,
    STATE_ACTIVE: 20,
    "Awaiting User Info": 30,
    "Awaiting Vendor": 40,
    "Awaiting Problem": 50,
    "Awaiting Evidence": 60,
    STATE_RESOLVED: 70,
    STATE_CLOSED: 80,
}

STATE_CATEGORY = {
    **{s: "Working" for s in WORKING_STATES},
    **{s: "Waiting" for s in WAITING_STATES},
    **{s: "Terminal" for s in TERMINAL_STATES},
}

# ---------------------------------------------------------------------------
# Excluded columns
# ---------------------------------------------------------------------------

# DQ-05: all of these exceed 98% unknown. They are dropped rather than carried as
# near-empty attributes, which would invite conclusions drawn from a tiny and
# almost certainly unrepresentative non-null subset.
SPARSE_EXCLUDED_COLUMNS = ["caused_by", "vendor", "cmdb_ci", "rfc", "problem_id"]

# DQ-08: closed_at is dominated by a bulk-close batch job (5 timestamps account for
# ~13,800 closures on 24/03/2016 between 18:40 and 19:01). It measures the batch,
# not the process, so it is excluded from every duration metric. It is retained on
# the case table for audit purposes only, clearly named.
UNRELIABLE_DURATION_COLUMNS = ["closed_at"]

# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------

# DQ-09: made_sla flips mid-case for 36.6% of incidents because it is a live
# evaluation, not a stored outcome. The case-grain truth is the LAST observed value.
CASE_ATTRIBUTE_STRATEGY = "last"

# Cycle time is measured opened_at -> resolved_at. DQ-07 identified 1,556 terminal
# cases with no resolved_at; these are excluded from the cycle-time denominator
# rather than counted as zero duration, which would drag every average down.
CYCLE_TIME_START = "opened_at"
CYCLE_TIME_END = "resolved_at"

# Variant analysis collapses immediately-repeated states, standard process-mining
# practice: New -> Active -> Active -> Resolved is the same routing behaviour as
# New -> Active -> Resolved. Without this, minor-edit noise explodes the variant
# count and hides the real routing patterns.
VARIANT_COLLAPSE_REPEATS = True
VARIANT_SEPARATOR = " -> "

# Business hours used for the out-of-hours analysis (question 17).
BUSINESS_HOUR_START = 8
BUSINESS_HOUR_END = 18
