"""
Step 2 - Dataset profiling and data-quality assessment.

Produces evidence, not opinions. Every cleaning rule applied later in
02_transform.py must be traceable to a check that failed here.

Outputs:
    data/quality/column_profile.csv   per-column profile
    data/quality/dq_results.csv       named checks with PASS/WARN/FAIL
    data/quality/dq_report.md         human-readable findings

Usage:
    python src/01_profile_and_quality.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW_CSV = ROOT / "data" / "raw" / "incident_event_log.csv"
QUALITY_DIR = ROOT / "data" / "quality"

# The source system exports unknown values as a literal '?' rather than an empty
# field. Treating it as a normal string would silently inflate every distinct count.
MISSING_SENTINEL = "?"

CASE_ID = "number"
ACTIVITY = "incident_state"
EVENT_TS = "sys_updated_at"
TS_FORMAT = "%d/%m/%Y %H:%M"

TIMESTAMP_COLUMNS = ["opened_at", "sys_created_at", "sys_updated_at", "resolved_at", "closed_at"]

# States that represent the process waiting on somebody outside the resolver team.
WAITING_STATES = ["Awaiting User Info", "Awaiting Vendor", "Awaiting Problem", "Awaiting Evidence"]

# Columns so sparsely populated they cannot support analysis. Threshold is a
# judgement call, documented here rather than buried in the transform.
SPARSE_THRESHOLD_PCT = 90.0


@dataclass
class CheckResult:
    check_id: str
    description: str
    status: str  # PASS | WARN | FAIL
    observed: str
    implication: str


def load_raw() -> pd.DataFrame:
    """Load every column as string so nothing is silently coerced before we inspect it."""
    if not RAW_CSV.exists():
        raise FileNotFoundError(f"{RAW_CSV} not found. Run: python src/00_download_data.py")
    df = pd.read_csv(RAW_CSV, dtype=str, keep_default_na=False)
    print(f"Loaded {len(df):,} events x {df.shape[1]} columns")
    return df


def parse_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(
        series.replace(MISSING_SENTINEL, None), format=TS_FORMAT, errors="coerce"
    )


def build_column_profile(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    total = len(df)
    for col in df.columns:
        unknown = int((df[col] == MISSING_SENTINEL).sum())
        blank = int((df[col].str.strip() == "").sum())
        vc = df.loc[df[col] != MISSING_SENTINEL, col].value_counts()
        rows.append(
            {
                "column": col,
                "distinct_values": int(df[col].nunique()),
                "unknown_count": unknown,
                "unknown_pct": round(unknown / total * 100, 2),
                "blank_count": blank,
                "most_common_value": vc.index[0] if len(vc) else None,
                "most_common_count": int(vc.iloc[0]) if len(vc) else 0,
                "usable_for_analysis": unknown / total * 100 < SPARSE_THRESHOLD_PCT,
            }
        )
    return pd.DataFrame(rows).sort_values("unknown_pct", ascending=False)


def run_checks(df: pd.DataFrame, profile: pd.DataFrame) -> list[CheckResult]:
    results: list[CheckResult] = []
    total = len(df)
    add = results.append

    # --- DQ-01 completeness of the three process-mining mandatory fields -------
    mandatory = [CASE_ID, ACTIVITY, EVENT_TS]
    bad = {
        c: int((df[c] == MISSING_SENTINEL).sum() + (df[c].str.strip() == "").sum())
        for c in mandatory
    }
    add(
        CheckResult(
            "DQ-01",
            "Case ID, activity and timestamp are populated on every event",
            "PASS" if sum(bad.values()) == 0 else "FAIL",
            f"missing per column: {bad}",
            "A null in any of these makes the event unusable for process mining; "
            "the row would have to be dropped.",
        )
    )

    # --- DQ-02 event grain uniqueness ----------------------------------------
    dup_ts = int(df.duplicated([CASE_ID, EVENT_TS]).sum())
    add(
        CheckResult(
            "DQ-02",
            "(case_id, timestamp) uniquely identifies an event",
            "PASS" if dup_ts == 0 else "WARN",
            f"{dup_ts:,} rows share a (number, sys_updated_at) pair ({dup_ts / total * 100:.1f}%)",
            "Timestamps are minute-precision, so several updates within the same minute "
            "collapse onto one value. The fact table therefore needs a surrogate event "
            "key and an explicit within-case sequence number, not a natural key.",
        )
    )

    # --- DQ-03 full-row duplicates -------------------------------------------
    dup_all = int(df.duplicated().sum())
    add(
        CheckResult(
            "DQ-03",
            "No fully duplicated event rows",
            "PASS" if dup_all == 0 else "WARN",
            f"{dup_all:,} identical rows",
            "Exact duplicates would double-count volume and distort dwell times.",
        )
    )

    # --- DQ-04 timestamp parseability ----------------------------------------
    parse_detail = {}
    parse_fail = False
    for col in TIMESTAMP_COLUMNS:
        raw_known = df[col] != MISSING_SENTINEL
        parsed = parse_ts(df[col])
        unparseable = int((raw_known & parsed.isna()).sum())
        parse_detail[col] = unparseable
        parse_fail |= unparseable > 0
    add(
        CheckResult(
            "DQ-04",
            f"All non-'?' timestamps parse as {TS_FORMAT}",
            "FAIL" if parse_fail else "PASS",
            f"unparseable per column: {parse_detail}",
            "Confirms the source uses day-first European format. Parsing these as "
            "month-first would silently corrupt every duration in the project.",
        )
    )

    # --- DQ-05 sparse columns -------------------------------------------------
    sparse = profile.loc[profile["unknown_pct"] >= SPARSE_THRESHOLD_PCT, ["column", "unknown_pct"]]
    add(
        CheckResult(
            "DQ-05",
            f"Columns at least {SPARSE_THRESHOLD_PCT:.0f}% unknown are identified",
            "WARN" if len(sparse) else "PASS",
            "; ".join(f"{r.column} {r.unknown_pct}%" for r in sparse.itertuples()) or "none",
            "These carry almost no signal. They are excluded from the dimensional model "
            "rather than shipped as near-empty attributes that invite false conclusions.",
        )
    )

    # --- DQ-06 invalid activity values ---------------------------------------
    valid_states = {"New", "Active", "Resolved", "Closed", *WAITING_STATES}
    invalid = df.loc[~df[ACTIVITY].isin(valid_states), ACTIVITY].value_counts()
    add(
        CheckResult(
            "DQ-06",
            "Every incident_state is a recognised process activity",
            "PASS" if invalid.empty else "WARN",
            f"{int(invalid.sum())} events in unrecognised states: {invalid.to_dict()}",
            "Sentinel values like '-100' are source-system artefacts, not real process "
            "steps. Left in, they appear as a phantom activity in the process map.",
        )
    )

    # --- DQ-07 resolution timestamp present for resolved/closed cases ---------
    last = df.sort_values([CASE_ID, EVENT_TS]).groupby(CASE_ID).tail(1)
    terminal = last[last[ACTIVITY].isin(["Resolved", "Closed"])]
    no_resolved_at = int((terminal["resolved_at"] == MISSING_SENTINEL).sum())
    add(
        CheckResult(
            "DQ-07",
            "Cases ending in Resolved/Closed carry a resolved_at timestamp",
            "PASS" if no_resolved_at == 0 else "WARN",
            f"{no_resolved_at:,} of {len(terminal):,} terminal cases have resolved_at unknown",
            "Cycle time cannot be computed for these; they must be excluded from the "
            "cycle-time denominator rather than counted as zero duration.",
        )
    )

    # --- DQ-08 closed_at reliability -----------------------------------------
    closed_vc = df.loc[df["closed_at"] != MISSING_SENTINEL, "closed_at"].value_counts()
    top_share = closed_vc.iloc[0] / total * 100 if len(closed_vc) else 0
    top5 = closed_vc.head(5)
    add(
        CheckResult(
            "DQ-08",
            "closed_at is distributed, not concentrated on batch timestamps",
            "FAIL" if top_share > 1.0 else "PASS",
            f"{df['closed_at'].nunique():,} distinct values over {total:,} events; "
            f"top 5 = {top5.to_dict()}",
            "A handful of timestamps account for a large share of all closures: an "
            "automated bulk-close job, not real agent activity. closed_at is therefore "
            "NOT a valid basis for cycle time. resolved_at is used instead.",
        )
    )

    # --- DQ-09 made_sla stability within a case ------------------------------
    varying = int((df.groupby(CASE_ID)["made_sla"].nunique() > 1).sum())
    n_cases = df[CASE_ID].nunique()
    add(
        CheckResult(
            "DQ-09",
            "made_sla is constant across all events of a case",
            "PASS" if varying == 0 else "FAIL",
            f"{varying:,} of {n_cases:,} cases ({varying / n_cases * 100:.1f}%) change made_sla mid-case",
            "made_sla is a live evaluation that flips the moment the clock is breached, "
            "not a static outcome. Counting it at event grain would overstate attainment. "
            "The case-grain fact must take the LAST observed value per case.",
        )
    )

    # --- DQ-10 chronological ordering ----------------------------------------
    tmp = df[[CASE_ID, EVENT_TS]].copy()
    tmp["ts"] = parse_ts(tmp[EVENT_TS])
    tmp = tmp.sort_values([CASE_ID, "ts"])
    backwards = int((tmp.groupby(CASE_ID)["ts"].diff() < pd.Timedelta(0)).sum())
    add(
        CheckResult(
            "DQ-10",
            "Events within a case are chronologically consistent",
            "PASS" if backwards == 0 else "WARN",
            f"{backwards:,} negative time gaps after sorting",
            "Negative dwell times would corrupt bottleneck analysis.",
        )
    )

    # --- DQ-11 opened_at precedes resolution ---------------------------------
    o = parse_ts(df["opened_at"])
    r = parse_ts(df["resolved_at"])
    both = o.notna() & r.notna()
    inverted = int((r < o).sum())
    add(
        CheckResult(
            "DQ-11",
            "resolved_at is never earlier than opened_at",
            "PASS" if inverted == 0 else "WARN",
            f"{inverted:,} events of {int(both.sum()):,} comparable have resolved_at < opened_at",
            "Negative cycle times must be quarantined, never averaged in.",
        )
    )

    # --- DQ-12 referential consistency of case attributes --------------------
    unstable = {}
    for col in ["priority", "category", "contact_type", "caller_id"]:
        n = int((df.groupby(CASE_ID)[col].nunique() > 1).sum())
        if n:
            unstable[col] = n
    add(
        CheckResult(
            "DQ-12",
            "Case-level attributes are stable across a case's events",
            "PASS" if not unstable else "WARN",
            f"cases with more than one distinct value: {unstable or 'none'}",
            "Attributes that change mid-case cannot be modelled as static case dimensions; "
            "the case-grain fact takes the last observed value, consistent with DQ-09.",
        )
    )

    return results


def write_markdown(results: list[CheckResult], profile: pd.DataFrame, df: pd.DataFrame) -> None:
    lines: list[str] = []
    a = lines.append
    a("# Step 2 - Data Profiling & Data-Quality Report\n")
    a("> Generated by `src/01_profile_and_quality.py`. Do not edit by hand.\n")

    a("## Dataset shape\n")
    a(f"- Events (rows): **{len(df):,}**")
    a(f"- Cases (distinct `number`): **{df[CASE_ID].nunique():,}**")
    a(f"- Columns: **{df.shape[1]}**")
    ts = parse_ts(df[EVENT_TS])
    a(f"- Event window: **{ts.min():%Y-%m-%d} to {ts.max():%Y-%m-%d}**")
    per_case = df.groupby(CASE_ID).size()
    a(
        f"- Events per case: min {per_case.min()}, median {int(per_case.median())}, "
        f"mean {per_case.mean():.2f}, max {per_case.max()}\n"
    )

    a("## Data-quality checks\n")
    a("| ID | Check | Status | Observed |")
    a("|---|---|---|---|")
    for r in results:
        a(f"| {r.check_id} | {r.description} | **{r.status}** | {r.observed} |")
    a("")

    a("## Why each finding matters\n")
    for r in results:
        if r.status != "PASS":
            a(f"### {r.check_id} - {r.description} ({r.status})")
            a(f"**Observed:** {r.observed}\n")
            a(f"**Implication:** {r.implication}\n")

    a("## Column profile (sparsest first)\n")
    a("| Column | Distinct | Unknown | Unknown % | Usable |")
    a("|---|---|---|---|---|")
    for row in profile.itertuples():
        a(
            f"| `{row.column}` | {row.distinct_values:,} | {row.unknown_count:,} | "
            f"{row.unknown_pct}% | {'yes' if row.usable_for_analysis else 'NO'} |"
        )
    a("")

    (QUALITY_DIR / "dq_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    QUALITY_DIR.mkdir(parents=True, exist_ok=True)
    df = load_raw()

    profile = build_column_profile(df)
    profile.to_csv(QUALITY_DIR / "column_profile.csv", index=False)

    results = run_checks(df, profile)
    pd.DataFrame([asdict(r) for r in results]).to_csv(QUALITY_DIR / "dq_results.csv", index=False)

    write_markdown(results, profile, df)

    print("\n--- Data-quality checks ---")
    for r in results:
        print(f"  [{r.status:4s}] {r.check_id}  {r.description}")
        print(f"         {r.observed}")

    failures = [r for r in results if r.status == "FAIL"]
    warnings = [r for r in results if r.status == "WARN"]
    print(
        f"\n{len(results)} checks: {len(results) - len(failures) - len(warnings)} PASS, "
        f"{len(warnings)} WARN, {len(failures)} FAIL"
    )
    print(f"Artefacts written to {QUALITY_DIR}")

    # A FAIL is not a crash: these are known characteristics of the source data that
    # Step 3 is required to handle. The build fails only if the mandatory-field check
    # breaks, because that would make the log unusable.
    blocking = [r for r in failures if r.check_id == "DQ-01"]
    if blocking:
        print("BLOCKING: mandatory process-mining fields are incomplete.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
