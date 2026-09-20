"""
Step 4/5 - Build the dimensional warehouse and export it for Power BI.

Reads the Step 3 Parquet output, creates two staging views over it, executes
sql/01_build_star_schema.sql, validates referential integrity, then exports every
table to CSV for Power BI to import.

CONCURRENCY / ROBUSTNESS
The build opens the database read-write, holds the only connection, and closes it
in a finally block. Every downstream consumer (analysis queries, Power BI) reads
either the exported CSVs or a read-only connection. There is therefore never more
than one writer and never a shared transaction, so deadlocks are not possible by
construction rather than by convention.

The build is fully idempotent: every object is DROP ... CREATE, so re-running it
converges on the same state regardless of what was there before.

Usage:
    python src/03_build_warehouse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402

SCHEMA_SQL = cfg.SQL_DIR / "01_build_star_schema.sql"
EXPORT_DIR = cfg.PROCESSED_DIR / "powerbi"

EVENT_PARQUET = cfg.PROCESSED_DIR / "event_log_clean.parquet"
CASE_PARQUET = cfg.PROCESSED_DIR / "incident_case.parquet"

DIMENSIONS = [
    "dim_date",
    "dim_priority",
    "dim_state",
    "dim_assignment_group",
    "dim_category",
    "dim_contact_type",
    "dim_location",
    "dim_closed_code",
    "dim_resolver",
    "dim_variant",
]
FACTS = ["fact_incident_case", "fact_incident_event"]

# (fact table, fact column, dimension table, dimension key column)
RELATIONSHIPS = [
    ("fact_incident_case", "priority_key", "dim_priority", "priority_key"),
    ("fact_incident_case", "final_state_key", "dim_state", "state_key"),
    ("fact_incident_case", "assignment_group_key", "dim_assignment_group", "assignment_group_key"),
    ("fact_incident_case", "category_key", "dim_category", "category_key"),
    ("fact_incident_case", "contact_type_key", "dim_contact_type", "contact_type_key"),
    ("fact_incident_case", "location_key", "dim_location", "location_key"),
    ("fact_incident_case", "closed_code_key", "dim_closed_code", "closed_code_key"),
    ("fact_incident_case", "resolver_key", "dim_resolver", "resolver_key"),
    ("fact_incident_case", "variant_key", "dim_variant", "variant_key"),
    ("fact_incident_case", "opened_date_key", "dim_date", "date_key"),
    ("fact_incident_event", "state_key", "dim_state", "state_key"),
    ("fact_incident_event", "case_key", "fact_incident_case", "case_key"),
    ("fact_incident_event", "event_date_key", "dim_date", "date_key"),
]


def create_staging_views(con: duckdb.DuckDBPyConnection) -> None:
    """
    The only platform-specific code in the project.

    On PostgreSQL these two views would instead be tables populated by COPY. Every
    other line of SQL in sql/ is portable ANSI, so migrating the warehouse means
    replacing this function and nothing else.
    """
    for parquet in (EVENT_PARQUET, CASE_PARQUET):
        if not parquet.exists():
            raise FileNotFoundError(
                f"{parquet} not found. Run: python src/02_transform.py"
            )

    con.execute(
        f"CREATE OR REPLACE VIEW stg_events AS "
        f"SELECT * FROM read_parquet('{EVENT_PARQUET.as_posix()}')"
    )
    con.execute(
        f"CREATE OR REPLACE VIEW stg_cases AS "
        f"SELECT * FROM read_parquet('{CASE_PARQUET.as_posix()}')"
    )
    events = con.execute("SELECT COUNT(*) FROM stg_events").fetchone()[0]
    cases = con.execute("SELECT COUNT(*) FROM stg_cases").fetchone()[0]
    print(f"[stage]  stg_events {events:,} rows | stg_cases {cases:,} rows")


def execute_schema(con: duckdb.DuckDBPyConnection) -> None:
    if not SCHEMA_SQL.exists():
        raise FileNotFoundError(f"{SCHEMA_SQL} not found")
    con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    print(f"[schema] executed {SCHEMA_SQL.name}")


def report_row_counts(con: duckdb.DuckDBPyConnection) -> None:
    print("\n--- Warehouse contents ---")
    for table in DIMENSIONS + FACTS:
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<26} {n:>9,} rows")


def validate(con: duckdb.DuckDBPyConnection) -> list[str]:
    """
    Referential integrity and grain checks.

    Power BI will not tell you that 300 fact rows failed to match a dimension - it
    will quietly file them under a blank member and your totals will be wrong but
    plausible. Catching it here is the whole point.
    """
    problems: list[str] = []
    print("\n--- Warehouse validation ---")

    def check(name: str, ok: bool, detail: str) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            problems.append(f"{name}: {detail}")

    # 1. Every foreign key resolves to a dimension member.
    for fact, fk, dim, pk in RELATIONSHIPS:
        orphans = con.execute(
            f"""
            SELECT COUNT(*) FROM {fact} f
            LEFT JOIN {dim} d ON d.{pk} = f.{fk}
            WHERE f.{fk} IS NOT NULL AND d.{pk} IS NULL
            """
        ).fetchone()[0]
        check(f"FK {fact}.{fk} -> {dim}", orphans == 0, f"{orphans:,} orphan rows")

    # 2. Dimension keys are unique - a duplicate would silently fan out the fact.
    for dim, pk in [(d, d.replace("dim_", "") + "_key") for d in DIMENSIONS]:
        pk = "date_key" if dim == "dim_date" else pk
        total, distinct = con.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT {pk}) FROM {dim}"
        ).fetchone()
        check(f"{dim}.{pk} unique", total == distinct, f"{total:,} rows / {distinct:,} keys")

    # 3. Grain is preserved end to end.
    case_rows = con.execute("SELECT COUNT(*) FROM fact_incident_case").fetchone()[0]
    stg_cases = con.execute("SELECT COUNT(*) FROM stg_cases").fetchone()[0]
    check("case fact grain", case_rows == stg_cases, f"{case_rows:,} == {stg_cases:,}")

    event_rows = con.execute("SELECT COUNT(*) FROM fact_incident_event").fetchone()[0]
    stg_events = con.execute("SELECT COUNT(*) FROM stg_events").fetchone()[0]
    check("event fact grain", event_rows == stg_events, f"{event_rows:,} == {stg_events:,}")

    # 4. The SLA flags must be mutually exclusive and exhaustive, because every
    #    attainment measure divides one by their sum.
    bad_sla = con.execute(
        "SELECT COUNT(*) FROM fact_incident_case WHERE is_sla_met + is_sla_breached <> 1"
    ).fetchone()[0]
    check("SLA flags exclusive", bad_sla == 0, f"{bad_sla:,} rows where met+breached <> 1")

    # 5. dim_date must cover every date any fact points at.
    uncovered = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT opened_date_key AS k FROM fact_incident_case
            UNION
            SELECT event_date_key       FROM fact_incident_event
        ) x
        LEFT JOIN dim_date d ON d.date_key = x.k
        WHERE d.date_key IS NULL
        """
    ).fetchone()[0]
    check("dim_date coverage", uncovered == 0, f"{uncovered:,} fact dates missing from dim_date")

    return problems


def export_for_powerbi(con: duckdb.DuckDBPyConnection) -> None:
    """
    Export to CSV for Power BI import.

    CSV rather than a live DuckDB connection: Power BI has no native DuckDB
    connector, and an ODBC bridge would add a moving part and a second process
    holding the database file open. Import mode into VertiPaq is also simply the
    faster option for a model this size - queries never leave memory.
    """
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    for table in DIMENSIONS + FACTS:
        out = EXPORT_DIR / f"{table}.csv"
        con.execute(
            f"COPY {table} TO '{out.as_posix()}' (HEADER, DELIMITER ',', DATEFORMAT '%Y-%m-%d')"
        )
    total_mb = sum(f.stat().st_size for f in EXPORT_DIR.glob("*.csv")) / 1e6
    print(f"\n[export] {len(DIMENSIONS + FACTS)} tables -> {EXPORT_DIR} ({total_mb:.1f} MB)")


def main() -> int:
    cfg.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    # Single writer, explicitly closed. No pooling, no background threads, no
    # second connection anywhere in the codebase.
    con = duckdb.connect(str(cfg.DUCKDB_PATH))
    try:
        create_staging_views(con)
        execute_schema(con)
        report_row_counts(con)
        problems = validate(con)

        if problems:
            print(f"\n{len(problems)} validation failure(s) - not exporting.")
            for p in problems:
                print(f"  - {p}")
            return 1

        export_for_powerbi(con)
    finally:
        con.close()

    print(f"\nWarehouse: {cfg.DUCKDB_PATH}")
    print("Step 4/5 complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
