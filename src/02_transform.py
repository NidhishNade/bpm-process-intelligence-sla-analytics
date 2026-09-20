"""
Step 3 - Event-log interpretation and transformation.

Turns the raw flat export into two analysis-ready tables at two different grains:

    event_log_clean.parquet   one row per event   (~141,700)  dwell time, sequence
    incident_case.parquet     one row per case    (24,918)    outcome, cycle time, variant

Design rule: all sequence-dependent logic (dwell time, ordering, transitions,
variant signatures) is computed ONCE here, never in SQL or DAX. Those engines
recompute per filter context, which at this row count is the difference between
a report that responds instantly and one that stalls on every slicer click.

Everything is vectorised - no per-row Python loops - so the full transform runs
in a couple of seconds and stays linear if the log grows.

Usage:
    python src/02_transform.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402


# ---------------------------------------------------------------------------
# Load and normalise
# ---------------------------------------------------------------------------


def load_raw() -> pd.DataFrame:
    if not cfg.RAW_CSV.exists():
        raise FileNotFoundError(
            f"{cfg.RAW_CSV} not found. Run: python src/00_download_data.py"
        )
    df = pd.read_csv(cfg.RAW_CSV, dtype=str, keep_default_na=False)
    print(f"[load]      {len(df):,} raw events x {df.shape[1]} columns")
    return df


def normalise(df: pd.DataFrame, audit: dict) -> pd.DataFrame:
    """Sentinel -> null, then cast each column to its real type."""
    df = df.replace(cfg.MISSING_SENTINEL, np.nan)

    # Strip stray whitespace from identifier-style columns. The source has
    # double spaces in values such as 'Opened by  8', which would otherwise
    # produce two dimension members for the same person after any re-export.
    object_cols = df.select_dtypes(include=["object", "string"]).columns
    for col in object_cols:
        df[col] = df[col].str.strip().str.replace(r"\s+", " ", regex=True)

    for col in cfg.TIMESTAMP_COLUMNS:
        df[col] = pd.to_datetime(df[col], format=cfg.TS_FORMAT, errors="coerce")

    for col in cfg.BOOLEAN_COLUMNS:
        df[col] = df[col].map({"true": True, "false": False}).astype("boolean")

    for col in cfg.INTEGER_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int32")

    audit["columns_dropped_sparse"] = cfg.SPARSE_EXCLUDED_COLUMNS
    df = df.drop(columns=cfg.SPARSE_EXCLUDED_COLUMNS)

    print(f"[normalise] sentinel -> null, types cast, "
          f"{len(cfg.SPARSE_EXCLUDED_COLUMNS)} sparse columns dropped")
    return df


def remove_invalid_states(df: pd.DataFrame, audit: dict) -> pd.DataFrame:
    """DQ-06: drop the '-100' sentinel state so it does not appear as an activity."""
    mask = df[cfg.ACTIVITY].isin(cfg.INVALID_STATES)
    removed = int(mask.sum())
    audit["events_removed_invalid_state"] = removed
    if removed:
        print(f"[clean]     dropped {removed} events in sentinel state(s) "
              f"{cfg.INVALID_STATES}")
    return df.loc[~mask].copy()


# ---------------------------------------------------------------------------
# Event grain
# ---------------------------------------------------------------------------


def build_event_log(df: pd.DataFrame, audit: dict) -> pd.DataFrame:
    """
    Order events within each case and derive dwell time.

    DQ-02 showed (case, timestamp) is not unique for 10% of rows: the source stores
    minute precision, so several updates inside one minute share a timestamp. Sorting
    on timestamp alone would order those arbitrarily and make dwell times
    non-deterministic between runs. We therefore carry the original file position as
    a stable tie-breaker - the export is written in chronological order within a case,
    so file order is the best available sub-minute signal.
    """
    df = df.reset_index(drop=True)
    df["source_row_no"] = np.arange(len(df), dtype=np.int64)

    df = df.sort_values(
        [cfg.CASE_ID, cfg.EVENT_TS, "source_row_no"], kind="mergesort"
    ).reset_index(drop=True)

    grp = df.groupby(cfg.CASE_ID, sort=False)

    # Position of the event inside its case, 1-based.
    df["event_seq"] = grp.cumcount().astype("int32") + 1
    df["events_in_case"] = grp[cfg.CASE_ID].transform("size").astype("int32")
    df["is_first_event"] = df["event_seq"] == 1
    df["is_last_event"] = df["event_seq"] == df["events_in_case"]

    # Dwell = time until the NEXT event in the same case. The final event of a case
    # has no successor, so its dwell is genuinely undefined and stays null. Filling
    # it with zero would silently deflate every average dwell time.
    next_ts = grp[cfg.EVENT_TS].shift(-1)
    df["dwell_minutes"] = (
        (next_ts - df[cfg.EVENT_TS]).dt.total_seconds() / 60.0
    ).astype("float64")
    df["dwell_hours"] = df["dwell_minutes"] / 60.0

    # The state the case moved to next - this is the transition edge, and it is what
    # a process-map visual needs. Precomputing it here means the map is a simple
    # GROUP BY rather than a self-join at query time.
    df["next_state"] = grp[cfg.ACTIVITY].shift(-1)
    df["prev_state"] = grp[cfg.ACTIVITY].shift(1)

    df["state_category"] = df[cfg.ACTIVITY].map(cfg.STATE_CATEGORY)
    df["state_sort_order"] = df[cfg.ACTIVITY].map(cfg.STATE_SORT_ORDER).astype("Int16")

    # Surrogate key. DQ-02 rules out a natural key, and Power BI needs a unique
    # column to make the event table a reliable drill-through target.
    df["event_key"] = np.arange(1, len(df) + 1, dtype=np.int64)

    negative = int((df["dwell_minutes"] < 0).sum())
    audit["events_with_negative_dwell"] = negative
    if negative:
        raise ValueError(
            f"{negative} negative dwell times after sorting - ordering logic is broken"
        )

    audit["event_rows"] = len(df)
    print(f"[events]    {len(df):,} events sequenced, dwell time derived "
          f"({df['dwell_minutes'].notna().sum():,} measurable)")
    return df


# ---------------------------------------------------------------------------
# Case grain
# ---------------------------------------------------------------------------


def build_variants(events: pd.DataFrame) -> pd.DataFrame:
    """
    Build a process-variant signature per case.

    A variant is the ordered sequence of states a case passed through. Immediately
    repeated states are collapsed (New -> Active -> Active -> Resolved becomes
    New -> Active -> Resolved) because a repeat is an in-place edit, not a routing
    decision. Without collapsing, edit noise fragments the variant count and hides
    the routing patterns the analysis is actually looking for.
    """
    ev = events[[cfg.CASE_ID, "event_seq", cfg.ACTIVITY]].copy()

    if cfg.VARIANT_COLLAPSE_REPEATS:
        same_as_prev = (
            ev.groupby(cfg.CASE_ID, sort=False)[cfg.ACTIVITY].shift(1) == ev[cfg.ACTIVITY]
        )
        ev = ev.loc[~same_as_prev]

    variants = (
        ev.groupby(cfg.CASE_ID, sort=False)[cfg.ACTIVITY]
        .agg(lambda s: cfg.VARIANT_SEPARATOR.join(s))
        .rename("variant_path")
        .reset_index()
    )
    variants["variant_length"] = (
        variants["variant_path"].str.count(cfg.VARIANT_SEPARATOR.strip()) // 2 + 1
    ).astype("int16")
    return variants


def build_case_table(events: pd.DataFrame, audit: dict) -> pd.DataFrame:
    """
    Collapse the event log to one row per incident.

    Attribute strategy is LAST observed value, per DQ-09 and DQ-12: made_sla,
    priority and category all change during a case's life, and the final state of
    the record is the outcome the business is accountable for.
    """
    events = events.sort_values([cfg.CASE_ID, "event_seq"], kind="mergesort")
    grp = events.groupby(cfg.CASE_ID, sort=False)

    # Attributes taken from the final event of each case.
    last_value_columns = [
        cfg.ACTIVITY, "made_sla", "priority", "impact", "urgency",
        "category", "subcategory", "u_symptom", "contact_type", "location",
        "assignment_group", "assigned_to", "resolved_by", "closed_code",
        "knowledge", "notify", "u_priority_confirmation", "active",
        "reassignment_count", "reopen_count", "sys_mod_count",
        "caller_id", "opened_by",
    ]
    case = grp[last_value_columns].last()
    case = case.rename(columns={cfg.ACTIVITY: "final_state"})

    # opened_at is set at creation and does not change; take the first.
    case["opened_at"] = grp["opened_at"].first()

    # resolved_at / closed_at: take the last non-null seen anywhere in the case.
    case["resolved_at"] = grp["resolved_at"].last()
    case["closed_at_raw_unreliable"] = grp["closed_at"].last()

    case["first_event_at"] = grp[cfg.EVENT_TS].first()
    case["last_event_at"] = grp[cfg.EVENT_TS].last()
    case["event_count"] = grp.size().astype("int32")

    # ---- cycle time -------------------------------------------------------
    # opened_at -> resolved_at. closed_at is deliberately NOT used (DQ-08).
    delta = case["resolved_at"] - case["opened_at"]
    case["resolution_hours"] = (delta.dt.total_seconds() / 3600.0).astype("float64")
    case["resolution_days"] = case["resolution_hours"] / 24.0

    # A case only contributes to cycle-time averages if the measure is genuinely
    # computable and non-negative. This flag is the single gate every cycle-time
    # metric downstream filters on, so the exclusion rule lives in one place.
    case["has_valid_resolution_time"] = (
        case["resolved_at"].notna()
        & case["opened_at"].notna()
        & (case["resolution_hours"] >= 0)
    )

    invalid = int((~case["has_valid_resolution_time"]).sum())
    audit["cases_without_valid_resolution_time"] = invalid

    # ---- time in working vs waiting states --------------------------------
    # Precomputed per case so the bottleneck page never has to scan the event
    # table at query time.
    dwell_by_cat = (
        events.pivot_table(
            index=cfg.CASE_ID,
            columns="state_category",
            values="dwell_hours",
            aggfunc="sum",
        )
        .reindex(columns=["Working", "Waiting", "Terminal"])
        .fillna(0.0)
    )
    case["hours_in_working_states"] = dwell_by_cat["Working"]
    case["hours_in_waiting_states"] = dwell_by_cat["Waiting"]

    # ---- outcome flags ----------------------------------------------------
    case["sla_met"] = case["made_sla"]
    case["sla_breached"] = ~case["made_sla"].astype("boolean")
    case["was_reopened"] = (case["reopen_count"].fillna(0) > 0)
    case["was_reassigned"] = (case["reassignment_count"].fillna(0) > 0)
    case["is_resolved_or_closed"] = case["final_state"].isin(cfg.TERMINAL_STATES)

    # ---- opening-time attributes -----------------------------------------
    opened = case["opened_at"]
    case["opened_date"] = opened.dt.date
    case["opened_hour"] = opened.dt.hour.astype("Int8")
    case["opened_day_of_week"] = opened.dt.dayofweek.astype("Int8")  # Monday = 0
    case["opened_is_weekend"] = opened.dt.dayofweek >= 5
    case["opened_out_of_hours"] = (
        (opened.dt.hour < cfg.BUSINESS_HOUR_START)
        | (opened.dt.hour >= cfg.BUSINESS_HOUR_END)
        | (opened.dt.dayofweek >= 5)
    )

    case = case.reset_index()

    # ---- variants ---------------------------------------------------------
    case = case.merge(build_variants(events), on=cfg.CASE_ID, how="left")

    case = case.rename(columns={cfg.CASE_ID: "incident_number"})

    audit["case_rows"] = len(case)
    print(f"[cases]     {len(case):,} incidents, "
          f"{int(case['has_valid_resolution_time'].sum()):,} with valid cycle time "
          f"({invalid:,} excluded)")
    return case


# ---------------------------------------------------------------------------
# Post-transform validation
# ---------------------------------------------------------------------------


def validate(events: pd.DataFrame, case: pd.DataFrame, raw_rows: int, audit: dict) -> list[str]:
    """
    Assert the transform preserved what it must. These run every build; a silent
    row-count drift is the classic way a BI pipeline starts lying.
    """
    problems: list[str] = []

    def check(name: str, condition: bool, detail: str) -> None:
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {name}: {detail}")
        if not condition:
            problems.append(f"{name}: {detail}")

    expected_events = raw_rows - audit["events_removed_invalid_state"]
    check(
        "row conservation",
        len(events) == expected_events,
        f"{len(events):,} events == {raw_rows:,} raw - "
        f"{audit['events_removed_invalid_state']} invalid",
    )
    check(
        "case uniqueness",
        case["incident_number"].is_unique,
        f"{len(case):,} rows, {case['incident_number'].nunique():,} distinct incidents",
    )
    check(
        "event key uniqueness",
        events["event_key"].is_unique,
        f"{events['event_key'].nunique():,} distinct keys",
    )
    check(
        "case coverage",
        set(case["incident_number"]) == set(events[cfg.CASE_ID]),
        "every case in the event log has exactly one case row",
    )
    check(
        "no negative dwell",
        not (events["dwell_minutes"] < 0).any(),
        f"min dwell = {events['dwell_minutes'].min():.1f} min",
    )
    check(
        "no negative cycle time",
        not (case.loc[case["has_valid_resolution_time"], "resolution_hours"] < 0).any(),
        "all valid resolution times are >= 0",
    )
    check(
        "sequence integrity",
        bool((events.groupby(cfg.CASE_ID)["event_seq"].max()
              == events.groupby(cfg.CASE_ID).size()).all()),
        "event_seq runs 1..n with no gaps in every case",
    )
    check(
        "sla flag populated",
        case["sla_met"].notna().all(),
        f"{case['sla_met'].notna().sum():,} of {len(case):,} cases carry an SLA outcome",
    )
    check(
        "variant assigned",
        case["variant_path"].notna().all(),
        f"{case['variant_path'].nunique():,} distinct process variants found",
    )
    return problems


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    cfg.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    audit: dict = {}

    raw = load_raw()
    raw_rows = len(raw)

    df = normalise(raw, audit)
    df = remove_invalid_states(df, audit)

    events = build_event_log(df, audit)
    case = build_case_table(events, audit)

    print("\n--- Post-transform validation ---")
    problems = validate(events, case, raw_rows, audit)

    if problems:
        print(f"\n{len(problems)} validation failure(s). Nothing written.")
        for p in problems:
            print(f"  - {p}")
        return 1

    event_out = cfg.PROCESSED_DIR / "event_log_clean.parquet"
    case_out = cfg.PROCESSED_DIR / "incident_case.parquet"
    events.to_parquet(event_out, index=False)
    case.to_parquet(case_out, index=False)

    audit["outputs"] = {
        "events": str(event_out.relative_to(cfg.ROOT)),
        "cases": str(case_out.relative_to(cfg.ROOT)),
    }
    (cfg.QUALITY_DIR / "transform_audit.json").write_text(
        json.dumps(audit, indent=2, default=str), encoding="utf-8"
    )

    print(f"\nWrote {event_out.name} ({event_out.stat().st_size / 1e6:.1f} MB)")
    print(f"Wrote {case_out.name} ({case_out.stat().st_size / 1e6:.1f} MB)")
    print("Step 3 complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
