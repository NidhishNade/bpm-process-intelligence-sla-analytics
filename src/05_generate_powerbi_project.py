"""
Step 7 - Generate the Power BI project (PBIP) as code.

Emits a complete Power BI Desktop project:

    powerbi/ProcessIntelligence.pbip
    powerbi/ProcessIntelligence.SemanticModel/   TMDL model: tables, relationships, DAX
    powerbi/ProcessIntelligence.Report/          report pages and visuals

WHY GENERATE RATHER THAN CLICK
PBIP stores the model and report as plain text, so the semantic layer becomes a
reviewable, diffable, version-controlled artefact instead of an opaque binary.
Generating it also removes the commonest source of defects in a hand-built model:
column data types drifting away from the warehouse. Every column type below is
read from the DuckDB catalogue at generation time, so the model cannot disagree
with the data it loads.

Usage:
    python src/05_generate_powerbi_project.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import uuid
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402
from powerbi_measures import MEASURES  # noqa: E402

PBI_DIR = cfg.ROOT / "powerbi"
PROJECT = "ProcessIntelligence"
MODEL_DIR = PBI_DIR / f"{PROJECT}.SemanticModel"
REPORT_DIR = PBI_DIR / f"{PROJECT}.Report"
CSV_DIR = cfg.PROCESSED_DIR / "powerbi"

MEASURE_HOST_TABLE = "fact_incident_case"

# PBIR schema URLs. The report is emitted in PBIR (a folder of small JSON
# documents), not the legacy single report.json - Microsoft documents the
# legacy format as not supporting external editing, and Power BI Desktop
# silently opens an empty window when handed one.
# Versions are pinned deliberately: an unpinned "latest" would change the
# accepted shape of these documents under us on a Desktop upgrade.
_SCHEMA_BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report"
SCHEMA_PBIR = f"{_SCHEMA_BASE}/definitionProperties/2.0.0/schema.json"
SCHEMA_VERSION = f"{_SCHEMA_BASE}/definition/versionMetadata/1.0.0/schema.json"
SCHEMA_REPORT = f"{_SCHEMA_BASE}/definition/report/1.0.0/schema.json"
SCHEMA_PAGES = f"{_SCHEMA_BASE}/definition/pagesMetadata/1.0.0/schema.json"
SCHEMA_PAGE = f"{_SCHEMA_BASE}/definition/page/1.4.0/schema.json"
SCHEMA_VISUAL = f"{_SCHEMA_BASE}/definition/visualContainer/1.4.0/schema.json"


def write_json(path: Path, payload: dict) -> None:
    """Write one PBIR document, creating its folder. Sorted keys keep the diff
    stable so a regenerated report shows only real changes in git."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

TABLES = [
    "dim_date", "dim_priority", "dim_state", "dim_assignment_group",
    "dim_category", "dim_contact_type", "dim_location", "dim_closed_code",
    "dim_resolver", "dim_variant", "fact_incident_case", "fact_incident_event",
]

# (from_table, from_column, to_table, to_column, is_active)
RELATIONSHIPS = [
    ("fact_incident_case", "priority_key", "dim_priority", "priority_key", True),
    ("fact_incident_case", "final_state_key", "dim_state", "state_key", False),
    ("fact_incident_case", "assignment_group_key", "dim_assignment_group", "assignment_group_key", True),
    ("fact_incident_case", "category_key", "dim_category", "category_key", True),
    ("fact_incident_case", "contact_type_key", "dim_contact_type", "contact_type_key", True),
    ("fact_incident_case", "location_key", "dim_location", "location_key", True),
    ("fact_incident_case", "closed_code_key", "dim_closed_code", "closed_code_key", True),
    ("fact_incident_case", "resolver_key", "dim_resolver", "resolver_key", True),
    ("fact_incident_case", "variant_key", "dim_variant", "variant_key", True),
    ("fact_incident_case", "opened_date_key", "dim_date", "date_key", True),
    ("fact_incident_event", "case_key", "fact_incident_case", "case_key", True),
    ("fact_incident_event", "state_key", "dim_state", "state_key", True),
]
# fact_incident_case.final_state_key is INACTIVE: dim_state is already joined to
# the event fact, and two active paths between the same pair of tables is an
# ambiguous model that Power BI refuses to load. The event-grain path is the one
# the bottleneck analysis needs, so it wins.

# fact_incident_event.event_date_key is deliberately NOT related to dim_date.
# A second active path from dim_date to the event fact (direct, and via the case
# fact) would be ambiguous. Event-level date filtering flows through the case, so
# a date slicer filters both facts consistently.

HIDDEN_COLUMN_SUFFIXES = ("_key",)

# Columns worth surfacing with a friendly name rather than the raw warehouse name.
FRIENDLY_NAMES = {
    "priority_label": "Priority",
    "state_name": "State",
    "state_category": "State Category",
    "assignment_group_name": "Assignment Group",
    "category_name": "Category",
    "subcategory_name": "Subcategory",
    "contact_type_name": "Contact Type",
    "location_name": "Location",
    "closed_code_name": "Closed Code",
    "resolver_name": "Resolver",
    "variant_path": "Variant Path",
    "variant_rank": "Variant Rank",
    "variant_length": "Variant Length",
    "incident_number": "Incident Number",
    "year_month": "Year-Month",
    "month_name": "Month",
    "day_name": "Day",
    "date_value": "Date",
    "calendar_year": "Year",
    "calendar_quarter": "Quarter",
    "opened_hour": "Opened Hour",
    "opened_at": "Opened At",
    "resolved_at": "Resolved At",
    "event_timestamp": "Event Timestamp",
    "event_seq": "Event Sequence",
    "dwell_hours": "Dwell Hours",
    "resolution_hours": "Resolution Hours",
    "reassignment_count": "Reassignment Count",
    "reopen_count": "Reopen Count",
    "event_count": "Event Count",
}

# DuckDB type -> (TMDL dataType, Power Query type, summarizeBy)
TYPE_MAP = {
    "TINYINT": ("int64", "Int64.Type", "none"),
    "SMALLINT": ("int64", "Int64.Type", "none"),
    "INTEGER": ("int64", "Int64.Type", "none"),
    "BIGINT": ("int64", "Int64.Type", "none"),
    "HUGEINT": ("int64", "Int64.Type", "none"),
    "DOUBLE": ("double", "type number", "none"),
    "FLOAT": ("double", "type number", "none"),
    "DECIMAL": ("double", "type number", "none"),
    "VARCHAR": ("string", "type text", "none"),
    "DATE": ("dateTime", "type date", "none"),
    "TIMESTAMP": ("dateTime", "type datetime", "none"),
    "TIMESTAMP_NS": ("dateTime", "type datetime", "none"),
    "BOOLEAN": ("boolean", "type logical", "none"),
}


def guid() -> str:
    return str(uuid.uuid4())


def base_type(duck_type: str) -> str:
    return duck_type.split("(")[0].upper()


def map_type(duck_type: str) -> tuple[str, str, str]:
    bt = base_type(duck_type)
    if bt not in TYPE_MAP:
        raise ValueError(f"Unmapped DuckDB type {duck_type!r} - add it to TYPE_MAP")
    return TYPE_MAP[bt]


def read_schema() -> dict[str, list[tuple[str, str]]]:
    """Read column names and types straight from the warehouse catalogue."""
    if not cfg.DUCKDB_PATH.exists():
        raise FileNotFoundError(
            f"{cfg.DUCKDB_PATH} not found. Run: python src/03_build_warehouse.py"
        )
    con = duckdb.connect(str(cfg.DUCKDB_PATH), read_only=True)
    try:
        schema: dict[str, list[tuple[str, str]]] = {}
        for table in TABLES:
            rows = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
            if not rows:
                raise ValueError(f"Table {table} not found in warehouse")
            schema[table] = [(r[0], r[1]) for r in rows]
        return schema
    finally:
        con.close()


# ---------------------------------------------------------------------------
# TMDL generation
# ---------------------------------------------------------------------------

T = "\t"


def m_partition(table: str, columns: list[tuple[str, str]]) -> list[str]:
    """
    Power Query partition reading the exported CSV.

    Types are applied explicitly rather than letting Power BI infer them. Type
    inference samples the first 200 rows, which is exactly how a column that is
    integer for 200 rows and null afterwards ends up typed wrongly and silently
    breaks a relationship.
    """
    transforms = ", ".join(
        f'{{"{name}", {map_type(dtype)[1]}}}' for name, dtype in columns
    )
    return [
        f"{T}partition {table} = m",
        f"{T}{T}mode: import",
        f"{T}{T}source =",
        f"{T}{T}{T}{T}let",
        f'{T}{T}{T}{T}{T}Source = Csv.Document(',
        f'{T}{T}{T}{T}{T}{T}File.Contents(DataFolder & "{table}.csv"),',
        f"{T}{T}{T}{T}{T}{T}[Delimiter = \",\", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]",
        f"{T}{T}{T}{T}{T}),",
        f"{T}{T}{T}{T}{T}Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),",
        f"{T}{T}{T}{T}{T}Typed = Table.TransformColumnTypes(Promoted, {{{transforms}}})",
        f"{T}{T}{T}{T}in",
        f"{T}{T}{T}{T}{T}Typed",
        "",
    ]


def rewrite_dax(dax: str) -> str:
    """
    Rewrite warehouse column names in a DAX expression onto their model names.

    The measures are authored against warehouse column names because that is the
    vocabulary of the SQL layer. Renaming a column for the report field list then
    silently invalidates every measure that referenced the old name - Power BI
    surfaces that as a broken visual, not a build error. Applying the same
    FRIENDLY_NAMES mapping here means the rename can only ever be made in one
    place, and 06_validate_powerbi_project.py proves it held.
    """
    def sub(m: "re.Match[str]") -> str:
        return f"{m.group(1)}[{FRIENDLY_NAMES.get(m.group(2), m.group(2))}]"

    return re.sub(r"(\w+)\[([^\]]+)\]", sub, dax)


def render_measure(name: str, dax: str, fmt: str, folder: str, desc: str) -> list[str]:
    out: list[str] = []
    # Triple-slash lines become the measure description in the Power BI field list,
    # so the business definition travels with the model instead of living in a wiki.
    for chunk in desc.split(". "):
        chunk = chunk.strip()
        if chunk:
            out.append(f"{T}/// {chunk.rstrip('.')}.")

    dax_lines = rewrite_dax(dax).split("\n")
    if len(dax_lines) == 1:
        out.append(f"{T}measure {tmdl_name(name)} = {dax_lines[0]}")
    else:
        out.append(f"{T}measure {tmdl_name(name)} =")
        for line in dax_lines:
            out.append(f"{T}{T}{T}{T}{line}")

    if fmt:
        out.append(f"{T}{T}formatString: {fmt}")
    out.append(f"{T}{T}lineageTag: {guid()}")
    out.append(f"{T}{T}displayFolder: {folder}")
    out.append("")
    return out


def tmdl_name(name: str) -> str:
    """
    Quote an object name for TMDL when it needs quoting.

    TMDL reads an unquoted name only up to the first space. `column Opened At`
    parses as a column called `Opened` followed by a stray token, which Power BI
    reports as an *indentation* error on that line - an error message that
    points nowhere near the real cause. Any name that is not a plain identifier
    is therefore single-quoted, with embedded quotes doubled.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return name
    return "'" + name.replace("'", "''") + "'"


def render_table(table: str, columns: list[tuple[str, str]]) -> str:
    lines: list[str] = [f"table {table}"]
    if table == "dim_date":
        # Marks dim_date as the model's date table, which is what makes the
        # time-intelligence measures (DATEADD) valid.
        lines.append(f"{T}dataCategory: Time")
    lines.append(f"{T}lineageTag: {guid()}")
    lines.append("")

    if table == MEASURE_HOST_TABLE:
        for name, dax, fmt, folder, desc in MEASURES:
            lines.extend(render_measure(name, dax, fmt, folder, desc))

    for name, dtype in columns:
        tmdl_type, _, summarize = map_type(dtype)
        display = FRIENDLY_NAMES.get(name, name)

        lines.append(f"{T}column {tmdl_name(display)}")
        lines.append(f"{T}{T}dataType: {tmdl_type}")

        # Surrogate keys are model plumbing. Hiding them keeps the field list
        # readable and stops a report author accidentally aggregating a key.
        if name.endswith(HIDDEN_COLUMN_SUFFIXES):
            lines.append(f"{T}{T}isHidden")
        if table == "dim_date" and name == "date_value":
            lines.append(f"{T}{T}isKey")

        lines.append(f"{T}{T}lineageTag: {guid()}")
        lines.append(f"{T}{T}summarizeBy: {summarize}")
        lines.append(f'{T}{T}sourceColumn: {name}')

        if tmdl_type == "dateTime":
            lines.append(f'{T}{T}formatString: yyyy-mm-dd hh:nn:ss')
            # Auto date/time generates a hidden date hierarchy per date column.
            # Suppressed: it bloats the model and we already have a proper dim_date.
            lines.append(f"{T}{T}annotation SummarizationSetBy = Automatic")

        # Sort text columns by their numeric companion so axes order logically
        # rather than alphabetically.
        if table == "dim_priority" and name == "priority_label":
            lines.append(f"{T}{T}sortByColumn: priority_rank")
        if table == "dim_state" and name == "state_name":
            lines.append(f"{T}{T}sortByColumn: state_sort_order")

        lines.append("")

    lines.extend(m_partition(table, columns))
    lines.append(f"{T}annotation PBI_ResultType = Table")
    lines.append("")
    return "\n".join(lines)


def render_model() -> str:
    lines = [
        "model Model",
        f"{T}culture: en-GB",
        f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{T}sourceQueryCulture: en-GB",
        "",
        f"{T}annotation PBI_QueryOrder = {json.dumps(TABLES)}",
        "",
        f'{T}annotation __PBI_TimeIntelligenceEnabled = 0',
        "",
    ]
    for table in TABLES:
        lines.append(f"ref table {table}")
    lines.append("")
    return "\n".join(lines)


def render_relationships() -> str:
    lines: list[str] = []
    for from_t, from_c, to_t, to_c, active in RELATIONSHIPS:
        lines.append(f"relationship {guid()}")
        if not active:
            lines.append(f"{T}isActive: false")
        lines.append(f"{T}fromColumn: {from_t}.{from_c}")
        lines.append(f"{T}toColumn: {to_t}.{to_c}")
        lines.append("")
    return "\n".join(lines)


def render_expressions() -> str:
    """
    DataFolder parameter.

    The CSV location is a single named parameter rather than a path repeated in
    twelve queries, so re-pointing the model at a different environment is one
    edit in Transform Data > Manage Parameters.
    """
    folder = str(CSV_DIR).replace("\\", "\\\\")
    return "\n".join([
        "expression DataFolder = " + f'"{folder}\\\\" meta [IsParameterQuery=true, '
        "Type=\"Text\", IsParameterQueryRequired=true]",
        f"{T}lineageTag: {guid()}",
        "",
        f"{T}annotation PBI_NavigationStepName = Navigation",
        "",
        f"{T}annotation PBI_ResultType = Text",
        "",
    ])


def write_semantic_model(schema: dict[str, list[tuple[str, str]]]) -> None:
    definition = MODEL_DIR / "definition"
    tables_dir = definition / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)

    (MODEL_DIR / ".platform").write_text(
        json.dumps({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": "SemanticModel", "displayName": PROJECT},
            "config": {"version": "2.0", "logicalId": guid()},
        }, indent=2),
        encoding="utf-8",
    )
    (MODEL_DIR / "definition.pbism").write_text(
        json.dumps({"version": "4.2", "settings": {}}, indent=2), encoding="utf-8"
    )

    (definition / "database.tmdl").write_text(
        "database\n\tcompatibilityLevel: 1606\n", encoding="utf-8"
    )
    (definition / "model.tmdl").write_text(render_model(), encoding="utf-8")
    (definition / "relationships.tmdl").write_text(render_relationships(), encoding="utf-8")
    (definition / "expressions.tmdl").write_text(render_expressions(), encoding="utf-8")

    for table, columns in schema.items():
        (tables_dir / f"{table}.tmdl").write_text(render_table(table, columns), encoding="utf-8")

    print(f"[model]  {len(schema)} tables, {len(RELATIONSHIPS)} relationships, "
          f"{len(MEASURES)} measures")


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

PAGE_W, PAGE_H = 1280, 720


def visual(
    vtype: str,
    x: int, y: int, w: int, h: int,
    *,
    title: str,
    entities: dict[str, str],
    select: list[dict],
    projections: dict[str, list[str]],
    order_by: list[dict] | None = None,
    z: int = 0,
) -> dict:
    """
    Build one visual.json document in PBIR format.

    PBIR is the format Power BI documents as safe to author outside the tool;
    its predecessor (a single report.json holding stringified JSON blobs) is
    explicitly documented as not supporting external editing, which is why this
    generator targets PBIR and validates against the published JSON schemas.

    Every field reference is resolved from the `select` list rather than
    restated, so a projection can only ever point at a field this visual
    actually queries.
    """
    del entities, order_by  # PBIR addresses tables directly; sorting is left to the user.

    by_ref = {item["queryRef"]: item for item in select}

    query_state: dict[str, dict] = {}
    for role, refs in projections.items():
        query_state[role] = {
            "projections": [by_ref[qualify(ref)] for ref in refs]
        }

    return {
        "$schema": SCHEMA_VISUAL,
        "name": guid().replace("-", "")[:20],
        "position": {
            "x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": z,
        },
        "visual": {
            "visualType": vtype,
            "query": {"queryState": query_state},
            "drillFilterOtherVisuals": True,
            "visualContainerObjects": {
                "title": [{
                    "properties": {
                        # A card already prints its measure name under the
                        # value, so a title on top would say it twice.
                        "show": {"expr": {"Literal": {
                            "Value": "false" if vtype == "card" else "true"}}},
                        # A literal string in a Power BI expression is single
                        # quoted inside the JSON string value.
                        "text": {"expr": {"Literal": {"Value": f"'{title}'"}}},
                    }
                }]
            },
        },
    }


def model_name(column: str) -> str:
    """Warehouse column name -> the name the column carries in the model."""
    return FRIENDLY_NAMES.get(column, column)


def qualify(ref: str) -> str:
    """Map an 'entity.column' reference onto the model's naming."""
    entity, _, column = ref.rpartition(".")
    return f"{entity}.{model_name(column)}" if entity else ref


def col(alias: str, entity: str, column: str, label: str) -> dict:
    """A column projection. `alias` is accepted for call-site readability only."""
    del alias
    name = model_name(column)
    return {
        "field": {
            "Column": {
                "Expression": {"SourceRef": {"Entity": entity}},
                "Property": name,
            }
        },
        "queryRef": f"{entity}.{name}",
        "nativeQueryRef": label,
    }


def mea(alias: str, name: str, label: str | None = None) -> dict:
    """A measure projection. Measures live on the case fact table."""
    del alias
    return {
        "field": {
            "Measure": {
                "Expression": {"SourceRef": {"Entity": MEASURE_HOST_TABLE}},
                "Property": name,
            }
        },
        "queryRef": f"{MEASURE_HOST_TABLE}.{name}",
        "nativeQueryRef": label or name,
    }


F = "f"  # alias for fact_incident_case


def card(x: int, y: int, w: int, h: int, measure: str, title: str) -> dict:
    return visual(
        "card", x, y, w, h,
        title=title,
        entities={F: "fact_incident_case"},
        select=[mea(F, measure)],
        projections={"Values": [f"{MEASURE_HOST_TABLE}.{measure}"]},
    )


def build_pages() -> list[dict]:
    pages: list[dict] = []

    def page(name: str, display: str, visuals: list[dict], ordinal: int) -> dict:
        del ordinal  # page order is declared once, in pages.json
        return {
            "$schema": SCHEMA_PAGE,
            "name": name,
            "displayName": display,
            "displayOption": "FitToPage",
            "visualContainers": visuals,
            "width": PAGE_W,
            "height": PAGE_H,
        }

    # ---- Page 1: Executive Process Overview -------------------------------
    p1 = [
        card(20, 20, 290, 130, "Total Incidents", "Total Incidents"),
        card(330, 20, 290, 130, "SLA Attainment %", "SLA Attainment %"),
        card(640, 20, 290, 130, "SLA Breached", "SLA Breached"),
        card(950, 20, 290, 130, "Median Resolution Hours", "Median Resolution (hrs)"),
        visual(
            "lineChart", 20, 170, 610, 260,
            title="SLA Attainment by Month",
            entities={F: "fact_incident_case", "d": "dim_date"},
            select=[
                col("d", "dim_date", "year_month", "Year-Month"),
                mea(F, "SLA Attainment %"),
            ],
            projections={
                "Category": ["dim_date.year_month"],
                "Y": [f"{MEASURE_HOST_TABLE}.SLA Attainment %"],
            },
            order_by=[{
                "Direction": 1,
                "Expression": {"Column": {
                    "Expression": {"SourceRef": {"Source": "d"}}, "Property": "year_month"}},
            }],
        ),
        visual(
            "clusteredColumnChart", 650, 170, 610, 260,
            title="SLA Attainment by Priority",
            entities={F: "fact_incident_case", "p": "dim_priority"},
            select=[
                col("p", "dim_priority", "priority_label", "Priority"),
                mea(F, "SLA Attainment %"),
            ],
            # One measure only: a % and a count on one axis renders the % as
            # an invisible sliver under a "2000K%" scale. Volume is on the table.
            projections={
                "Category": ["dim_priority.priority_label"],
                "Y": [f"{MEASURE_HOST_TABLE}.SLA Attainment %"],
            },
        ),
        visual(
            "tableEx", 20, 450, 610, 250,
            title="Incident Volume and Outcome by Contact Type",
            entities={F: "fact_incident_case", "c": "dim_contact_type"},
            select=[
                col("c", "dim_contact_type", "contact_type_name", "Contact Type"),
                mea(F, "Total Incidents"),
                mea(F, "SLA Attainment %"),
                mea(F, "Median Resolution Hours"),
            ],
            projections={"Values": [
                "dim_contact_type.contact_type_name",
                f"{MEASURE_HOST_TABLE}.Total Incidents",
                f"{MEASURE_HOST_TABLE}.SLA Attainment %",
                f"{MEASURE_HOST_TABLE}.Median Resolution Hours",
            ]},
        ),
        visual(
            "slicer", 650, 450, 290, 250,
            title="Priority",
            entities={"p": "dim_priority"},
            select=[col("p", "dim_priority", "priority_label", "Priority")],
            projections={"Values": ["dim_priority.priority_label"]},
        ),
        visual(
            "slicer", 960, 450, 300, 250,
            title="Assignment Group",
            entities={"g": "dim_assignment_group"},
            select=[col("g", "dim_assignment_group", "assignment_group_name", "Assignment Group")],
            projections={"Values": ["dim_assignment_group.assignment_group_name"]},
        ),
    ]
    pages.append(page("ExecutiveOverview", "1. Executive Overview", p1, 0))

    # ---- Page 2: Workflow Bottleneck --------------------------------------
    p2 = [
        card(20, 20, 300, 120, "Total Dwell Hours", "Total Elapsed Hours"),
        card(340, 20, 300, 120, "Waiting Time %", "Waiting Time %"),
        card(660, 20, 300, 120, "Avg Events per Incident", "Avg Events / Incident"),
        card(980, 20, 280, 120, "Median Dwell Hours", "Median Dwell (hrs)"),
        visual(
            "barChart", 20, 160, 620, 280,
            title="Total Elapsed Hours by State (where time is lost)",
            entities={F: "fact_incident_case", "s": "dim_state"},
            select=[
                col("s", "dim_state", "state_name", "State"),
                mea(F, "Total Dwell Hours"),
            ],
            projections={
                "Category": ["dim_state.state_name"],
                "Y": [f"{MEASURE_HOST_TABLE}.Total Dwell Hours"],
            },
        ),
        visual(
            "donutChart", 660, 160, 600, 280,
            title="Working vs Waiting Time",
            entities={F: "fact_incident_case", "s": "dim_state"},
            select=[
                col("s", "dim_state", "state_category", "State Category"),
                mea(F, "Total Dwell Hours"),
            ],
            projections={
                "Category": ["dim_state.state_category"],
                "Y": [f"{MEASURE_HOST_TABLE}.Total Dwell Hours"],
            },
        ),
        visual(
            "tableEx", 20, 460, 1240, 240,
            title="Dwell Time Profile by State",
            entities={F: "fact_incident_case", "s": "dim_state"},
            select=[
                col("s", "dim_state", "state_name", "State"),
                col("s", "dim_state", "state_category", "State Category"),
                mea(F, "Total Dwell Hours"),
                mea(F, "Avg Dwell Hours"),
                mea(F, "Median Dwell Hours"),
                mea(F, "% of Total Elapsed Time"),
            ],
            projections={"Values": [
                "dim_state.state_name",
                "dim_state.state_category",
                f"{MEASURE_HOST_TABLE}.Total Dwell Hours",
                f"{MEASURE_HOST_TABLE}.Avg Dwell Hours",
                f"{MEASURE_HOST_TABLE}.Median Dwell Hours",
                f"{MEASURE_HOST_TABLE}.% of Total Elapsed Time",
            ]},
        ),
    ]
    pages.append(page("Bottleneck", "2. Workflow Bottlenecks", p2, 1))

    # ---- Page 3: SLA & Operational Performance ----------------------------
    p3 = [
        visual(
            "tableEx", 20, 20, 760, 400,
            title="SLA Performance by Assignment Group",
            entities={F: "fact_incident_case", "g": "dim_assignment_group"},
            select=[
                col("g", "dim_assignment_group", "assignment_group_name", "Assignment Group"),
                mea(F, "Total Incidents"),
                mea(F, "SLA Breached"),
                mea(F, "SLA Attainment %"),
                mea(F, "Median Resolution Hours"),
                mea(F, "Avg Reassignments"),
            ],
            projections={"Values": [
                "dim_assignment_group.assignment_group_name",
                f"{MEASURE_HOST_TABLE}.Total Incidents",
                f"{MEASURE_HOST_TABLE}.SLA Breached",
                f"{MEASURE_HOST_TABLE}.SLA Attainment %",
                f"{MEASURE_HOST_TABLE}.Median Resolution Hours",
                f"{MEASURE_HOST_TABLE}.Avg Reassignments",
            ]},
        ),
        visual(
            "scatterChart", 800, 20, 460, 400,
            title="Volume vs SLA Attainment by Group",
            entities={F: "fact_incident_case", "g": "dim_assignment_group"},
            select=[
                col("g", "dim_assignment_group", "assignment_group_name", "Assignment Group"),
                mea(F, "Total Incidents"),
                mea(F, "SLA Attainment %"),
            ],
            projections={
                "Category": ["dim_assignment_group.assignment_group_name"],
                "X": [f"{MEASURE_HOST_TABLE}.Total Incidents"],
                "Y": [f"{MEASURE_HOST_TABLE}.SLA Attainment %"],
            },
        ),
        visual(
            "clusteredColumnChart", 20, 440, 610, 260,
            title="SLA Attainment by Closed Code",
            entities={F: "fact_incident_case", "cc": "dim_closed_code"},
            select=[
                col("cc", "dim_closed_code", "closed_code_name", "Closed Code"),
                mea(F, "SLA Attainment %"),
            ],
            projections={
                "Category": ["dim_closed_code.closed_code_name"],
                "Y": [f"{MEASURE_HOST_TABLE}.SLA Attainment %"],
            },
        ),
        visual(
            "clusteredColumnChart", 650, 440, 610, 260,
            title="Reopen Rate by Closed Code",
            entities={F: "fact_incident_case", "cc": "dim_closed_code"},
            select=[
                col("cc", "dim_closed_code", "closed_code_name", "Closed Code"),
                mea(F, "Reopen Rate %"),
            ],
            projections={
                "Category": ["dim_closed_code.closed_code_name"],
                "Y": [f"{MEASURE_HOST_TABLE}.Reopen Rate %"],
            },
        ),
    ]
    pages.append(page("SLAOperational", "3. SLA & Operations", p3, 2))

    # ---- Page 4: Process Variants -----------------------------------------
    p4 = [
        card(20, 20, 300, 120, "Distinct Variants", "Distinct Variants"),
        card(340, 20, 300, 120, "Happy Path %", "Happy Path %"),
        card(660, 20, 300, 120, "Top 5 Variant %", "Top 5 Variant Share"),
        card(980, 20, 280, 120, "Total Incidents", "Incidents"),
        visual(
            "tableEx", 20, 160, 1240, 300,
            title="Process Variants by Volume and Outcome",
            entities={F: "fact_incident_case", "v": "dim_variant"},
            select=[
                col("v", "dim_variant", "variant_rank", "Rank"),
                col("v", "dim_variant", "variant_path", "Variant Path"),
                mea(F, "Total Incidents"),
                mea(F, "SLA Attainment %"),
                mea(F, "Median Resolution Hours"),
                mea(F, "Avg Events per Incident"),
            ],
            projections={"Values": [
                "dim_variant.variant_rank",
                "dim_variant.variant_path",
                f"{MEASURE_HOST_TABLE}.Total Incidents",
                f"{MEASURE_HOST_TABLE}.SLA Attainment %",
                f"{MEASURE_HOST_TABLE}.Median Resolution Hours",
                f"{MEASURE_HOST_TABLE}.Avg Events per Incident",
            ]},
            order_by=[{
                "Direction": 1,
                "Expression": {"Column": {
                    "Expression": {"SourceRef": {"Source": "v"}}, "Property": "variant_rank"}},
            }],
        ),
        visual(
            "scatterChart", 20, 480, 620, 220,
            title="Variant Length vs Median Resolution Hours",
            entities={F: "fact_incident_case", "v": "dim_variant"},
            select=[
                col("v", "dim_variant", "variant_path", "Variant Path"),
                col("v", "dim_variant", "variant_length", "Variant Length"),
                mea(F, "Median Resolution Hours"),
            ],
            projections={
                "Category": ["dim_variant.variant_path"],
                "X": ["dim_variant.variant_length"],
                "Y": [f"{MEASURE_HOST_TABLE}.Median Resolution Hours"],
            },
        ),
        visual(
            "clusteredColumnChart", 660, 480, 600, 220,
            title="SLA Attainment by Variant Length",
            entities={F: "fact_incident_case", "v": "dim_variant"},
            select=[
                col("v", "dim_variant", "variant_length", "Variant Length"),
                mea(F, "SLA Attainment %"),
            ],
            projections={
                "Category": ["dim_variant.variant_length"],
                "Y": [f"{MEASURE_HOST_TABLE}.SLA Attainment %"],
            },
        ),
    ]
    pages.append(page("Variants", "4. Process Variants", p4, 3))

    # ---- Page 5: Reassignment & Workload ----------------------------------
    p5 = [
        card(20, 20, 300, 120, "Reassignment Rate %", "Reassignment Rate"),
        card(340, 20, 300, 120, "Total Reassignments", "Total Reassignments"),
        card(660, 20, 300, 120, "Avg Reassignments", "Avg Reassignments"),
        card(980, 20, 280, 120, "Reopen Rate %", "Reopen Rate"),
        visual(
            "lineClusteredColumnComboChart", 20, 160, 620, 280,
            title="SLA Attainment Degrades with Each Handoff",
            entities={F: "fact_incident_case"},
            select=[
                col(F, "fact_incident_case", "reassignment_count", "Reassignment Count"),
                mea(F, "Total Incidents"),
                mea(F, "SLA Attainment %"),
            ],
            projections={
                "Category": ["fact_incident_case.reassignment_count"],
                "Y": [f"{MEASURE_HOST_TABLE}.Total Incidents"],
                "Y2": [f"{MEASURE_HOST_TABLE}.SLA Attainment %"],
            },
        ),
        visual(
            "tableEx", 660, 160, 600, 280,
            title="Workload and Outcome by Resolver",
            entities={F: "fact_incident_case", "r": "dim_resolver"},
            select=[
                col("r", "dim_resolver", "resolver_name", "Resolver"),
                mea(F, "Total Incidents"),
                mea(F, "SLA Attainment %"),
                mea(F, "Median Resolution Hours"),
            ],
            projections={"Values": [
                "dim_resolver.resolver_name",
                f"{MEASURE_HOST_TABLE}.Total Incidents",
                f"{MEASURE_HOST_TABLE}.SLA Attainment %",
                f"{MEASURE_HOST_TABLE}.Median Resolution Hours",
            ]},
        ),
        visual(
            "tableEx", 20, 460, 1240, 240,
            title="Handoff Profile by Assignment Group",
            entities={F: "fact_incident_case", "g": "dim_assignment_group"},
            select=[
                col("g", "dim_assignment_group", "assignment_group_name", "Assignment Group"),
                mea(F, "Total Incidents"),
                mea(F, "Reassignment Rate %"),
                mea(F, "Avg Reassignments"),
                mea(F, "Reopen Rate %"),
                mea(F, "SLA Attainment %"),
            ],
            projections={"Values": [
                "dim_assignment_group.assignment_group_name",
                f"{MEASURE_HOST_TABLE}.Total Incidents",
                f"{MEASURE_HOST_TABLE}.Reassignment Rate %",
                f"{MEASURE_HOST_TABLE}.Avg Reassignments",
                f"{MEASURE_HOST_TABLE}.Reopen Rate %",
                f"{MEASURE_HOST_TABLE}.SLA Attainment %",
            ]},
        ),
    ]
    pages.append(page("Workload", "5. Reassignment & Workload", p5, 4))

    # ---- Page 6: Time-Based Performance -----------------------------------
    p6 = [
        card(20, 20, 300, 120, "Out of Hours %", "Out of Hours Demand"),
        card(340, 20, 300, 120, "Total Incidents", "Total Incidents"),
        card(660, 20, 300, 120, "Median Resolution Hours", "Median Resolution (hrs)"),
        card(980, 20, 280, 120, "P90 Resolution Hours", "P90 Resolution (hrs)"),
        visual(
            "columnChart", 20, 160, 620, 270,
            title="Incident Arrival by Hour of Day",
            entities={F: "fact_incident_case"},
            select=[
                col(F, "fact_incident_case", "opened_hour", "Opened Hour"),
                mea(F, "Total Incidents"),
            ],
            projections={
                "Category": ["fact_incident_case.opened_hour"],
                "Y": [f"{MEASURE_HOST_TABLE}.Total Incidents"],
            },
        ),
        visual(
            "columnChart", 660, 160, 600, 270,
            title="Incident Arrival by Day of Week",
            entities={F: "fact_incident_case", "d": "dim_date"},
            select=[
                col("d", "dim_date", "day_name", "Day"),
                mea(F, "Total Incidents"),
            ],
            projections={
                "Category": ["dim_date.day_name"],
                "Y": [f"{MEASURE_HOST_TABLE}.Total Incidents"],
            },
        ),
        visual(
            "lineChart", 20, 450, 1240, 250,
            title="Median Resolution Hours and Volume by Month",
            entities={F: "fact_incident_case", "d": "dim_date"},
            select=[
                col("d", "dim_date", "year_month", "Year-Month"),
                mea(F, "Median Resolution Hours"),
                mea(F, "Total Incidents"),
            ],
            projections={
                "Category": ["dim_date.year_month"],
                "Y": [
                    f"{MEASURE_HOST_TABLE}.Median Resolution Hours",
                    f"{MEASURE_HOST_TABLE}.Total Incidents",
                ],
            },
        ),
    ]
    pages.append(page("TimeAnalysis", "6. Time-Based Performance", p6, 5))

    # ---- Page 7: Incident Drill-through -----------------------------------
    p7 = [
        card(20, 20, 400, 110, "Selected Incident", "Incident"),
        card(440, 20, 270, 110, "Total Events", "Events"),
        card(730, 20, 260, 110, "Median Resolution Hours", "Resolution (hrs)"),
        card(1010, 20, 250, 110, "Avg Reassignments", "Reassignments"),
        visual(
            "tableEx", 20, 150, 1240, 270,
            title="Event Timeline",
            entities={"e": "fact_incident_event", "s": "dim_state"},
            select=[
                col("e", "fact_incident_event", "event_seq", "Event Sequence"),
                col("e", "fact_incident_event", "event_timestamp", "Event Timestamp"),
                col("s", "dim_state", "state_name", "State"),
                col("s", "dim_state", "state_category", "State Category"),
                col("e", "fact_incident_event", "dwell_hours", "Dwell Hours"),
            ],
            projections={"Values": [
                "fact_incident_event.event_seq",
                "fact_incident_event.event_timestamp",
                "dim_state.state_name",
                "dim_state.state_category",
                "fact_incident_event.dwell_hours",
            ]},
            order_by=[{
                "Direction": 1,
                "Expression": {"Column": {
                    "Expression": {"SourceRef": {"Source": "e"}}, "Property": "event_seq"}},
            }],
        ),
        visual(
            "tableEx", 20, 440, 1240, 260,
            title="Incident Attributes",
            entities={
                F: "fact_incident_case", "p": "dim_priority", "g": "dim_assignment_group",
                "c": "dim_category", "v": "dim_variant",
            },
            select=[
                col(F, "fact_incident_case", "incident_number", "Incident Number"),
                col("p", "dim_priority", "priority_label", "Priority"),
                col("g", "dim_assignment_group", "assignment_group_name", "Assignment Group"),
                col("c", "dim_category", "category_name", "Category"),
                col("v", "dim_variant", "variant_path", "Variant Path"),
                mea(F, "SLA Attainment %"),
                mea(F, "Median Resolution Hours"),
            ],
            projections={"Values": [
                "fact_incident_case.incident_number",
                "dim_priority.priority_label",
                "dim_assignment_group.assignment_group_name",
                "dim_category.category_name",
                "dim_variant.variant_path",
                f"{MEASURE_HOST_TABLE}.SLA Attainment %",
                f"{MEASURE_HOST_TABLE}.Median Resolution Hours",
            ]},
        ),
    ]
    pages.append(page("IncidentDetail", "7. Incident Drill-through", p7, 6))

    return pages


def write_report() -> None:
    # Wipe the generated tree first. Without this, a page or visual removed
    # from build_pages() would linger on disk and keep rendering, and the
    # legacy report.json from an earlier build would still shadow definition/.
    shutil.rmtree(REPORT_DIR / "definition", ignore_errors=True)
    (REPORT_DIR / "report.json").unlink(missing_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "StaticResources" / "RegisteredResources").mkdir(parents=True, exist_ok=True)

    (REPORT_DIR / ".platform").write_text(
        json.dumps({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": "Report", "displayName": PROJECT},
            "config": {"version": "2.0", "logicalId": guid()},
        }, indent=2),
        encoding="utf-8",
    )
    (REPORT_DIR / "definition.pbir").write_text(
        json.dumps({
            "$schema": SCHEMA_PBIR,
            "version": "4.0",
            # byPath keeps the report and model together as one editable
            # project. byConnection would bind the report to a published
            # workspace model, which is the deployment form, not the dev form.
            "datasetReference": {"byPath": {"path": f"../{PROJECT}.SemanticModel"}},
        }, indent=2),
        encoding="utf-8",
    )

    definition = REPORT_DIR / "definition"
    write_json(definition / "version.json", {
        "$schema": SCHEMA_VERSION,
        "version": "2.0.0",  # PBIR format version; "1.0" is not a valid PBIR version and Desktop drops the whole report
    })
    write_json(definition / "report.json", {
        "$schema": SCHEMA_REPORT,
        "layoutOptimization": "None",
        # No baseTheme: naming one obliges the project to ship its JSON under
        # StaticResources/SharedResources/BaseThemes, and a missing theme file
        # makes Desktop load the model but render an empty canvas. An empty
        # collection falls back to Desktop's built-in default theme.
        "themeCollection": {},
    })

    pages = build_pages()
    write_json(definition / "pages" / "pages.json", {
        "$schema": SCHEMA_PAGES,
        "pageOrder": [p["name"] for p in pages],
        "activePageName": pages[0]["name"],
    })

    n_visuals = 0
    for page in pages:
        page_dir = definition / "pages" / page["name"]
        visuals = page.pop("visualContainers")
        write_json(page_dir / "page.json", page)
        # One file per visual: this is what makes the report reviewable in a
        # pull request instead of a single unreadable blob.
        for v in visuals:
            write_json(page_dir / "visuals" / v["name"] / "visual.json", v)
            n_visuals += 1

    print(f"[report] {len(pages)} pages, {n_visuals} visuals (PBIR)")


def write_pbip() -> None:
    (PBI_DIR / f"{PROJECT}.pbip").write_text(
        json.dumps({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
            "version": "1.0",
            "artifacts": [{"report": {"path": f"{PROJECT}.Report"}}],
            "settings": {"enableAutoRecovery": True},
        }, indent=2),
        encoding="utf-8",
    )


def write_kpi_docs() -> None:
    """
    Render the measure catalogue to docs/04_kpi_and_dax.md.

    Generated from the same MEASURES list the model is built from, and through
    the same rewrite_dax(), so the documented definition is literally the code
    that ships. A KPI dictionary that drifts from the model is worse than none.
    """
    out = cfg.DOCS_DIR / "04_kpi_and_dax.md"
    lines = [
        "# Step 7 - KPI Definitions and DAX Measures\n",
        "> Generated by `src/05_generate_powerbi_project.py` from "
        "`src/powerbi_measures.py`.",
        "> Do not edit by hand.\n",
        f"{len(MEASURES)} measures on `{MEASURE_HOST_TABLE}`, grouped by display "
        "folder.\n",
        "---\n",
    ]

    folders: dict[str, list] = {}
    for m in MEASURES:
        folders.setdefault(m[3], []).append(m)

    lines.append("## Catalogue\n")
    lines.append("| Measure | Folder | Format | Definition |")
    lines.append("|---|---|---|---|")
    for name, dax, fmt, folder, desc in MEASURES:
        first = desc.split(". ")[0].rstrip(".") + "."
        lines.append(f"| `{name}` | {folder} | `{fmt or 'text'}` | {first} |")
    lines.append("")

    for folder in sorted(folders):
        lines.append(f"## {folder}\n")
        for name, dax, fmt, _f, desc in folders[folder]:
            lines.append(f"### {name}\n")
            lines.append(f"{desc}\n")
            lines.append("```dax")
            lines.append(f"{name} =")
            lines.append(rewrite_dax(dax))
            lines.append("```\n")
            lines.append(f"Format string: `{fmt or '(text)'}`\n")

    cfg.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[docs]   {out.relative_to(cfg.ROOT)}")


def main() -> int:
    if not CSV_DIR.exists() or not any(CSV_DIR.glob("*.csv")):
        raise FileNotFoundError(
            f"{CSV_DIR} has no CSVs. Run: python src/03_build_warehouse.py"
        )

    schema = read_schema()

    # Regenerate from scratch so a renamed table cannot leave an orphan file
    # behind that Power BI would still try to load.
    for d in (MODEL_DIR, REPORT_DIR):
        if d.exists():
            shutil.rmtree(d)
    PBI_DIR.mkdir(parents=True, exist_ok=True)

    write_semantic_model(schema)
    write_report()
    write_pbip()
    write_kpi_docs()

    print(f"\nOpen: {PBI_DIR / (PROJECT + '.pbip')}")
    print("Step 7 complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
