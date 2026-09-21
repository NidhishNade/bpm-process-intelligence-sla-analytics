"""
Step 7 (validation) - Static checks on the generated Power BI project.

Power BI reports fail late and quietly. A column renamed in the model but still
referenced by a visual does not raise at build time; it raises as "Couldn't load
the data for this visual" on a tile, in front of whoever you are demoing to.
A measure referencing a dropped column behaves the same way.

This script catches all of that before the file is ever opened, by parsing the
generated TMDL and report JSON and checking every reference resolves:

    V1  every table declared in model.tmdl has a table file
    V2  both sides of every relationship exist and have matching data types
    V3  every column/measure a DAX expression references exists
    V4  every column a visual queries exists in the model
    V5  every projection queryRef agrees with the field it points at
    V6  every partition's sourceColumn list matches the CSV header on disk

Exit code is non-zero if anything fails, so it can gate a commit.

Usage:
    python src/06_validate_powerbi_project.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402

PBI_DIR = cfg.ROOT / "powerbi"
PROJECT = "ProcessIntelligence"
DEFINITION = PBI_DIR / f"{PROJECT}.SemanticModel" / "definition"
REPORT_DEF = PBI_DIR / f"{PROJECT}.Report" / "definition"
CSV_DIR = cfg.PROCESSED_DIR / "powerbi"

# DAX functions and keywords that look like bare identifiers but are not tables.
DAX_KEYWORDS = {
    "VAR", "RETURN", "IF", "NOT", "ISBLANK", "BLANK", "TRUE", "FALSE",
    "DIVIDE", "SUM", "SUMX", "AVERAGE", "MEDIAN", "COUNTROWS", "DISTINCTCOUNT",
    "CALCULATE", "REMOVEFILTERS", "SELECTEDVALUE", "DATEADD", "MONTH", "YEAR",
    "PERCENTILE", "INC", "MAX", "MIN", "ALL",
}

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str) -> None:
    global checks
    checks += 1
    if not ok:
        failures.append(f"{name}: {detail}")
        print(f"  [FAIL] {name}: {detail}")


# ---------------------------------------------------------------------------
# TMDL parsing
# ---------------------------------------------------------------------------

class Table:
    def __init__(self, name: str) -> None:
        self.name = name
        self.columns: dict[str, str] = {}      # model name -> dataType
        self.source_columns: list[str] = []    # warehouse names, in order
        self.measures: list[tuple[str, str]] = []  # (name, dax)


def parse_table(path: Path) -> Table:
    text = path.read_text(encoding="utf-8")
    table = Table(re.match(r"table (\S+)", text).group(1))

    # Columns: 'column <name>' then an indented block containing dataType and
    # sourceColumn. Names may contain spaces, so match to end of line.
    for block in re.finditer(
        r"^\tcolumn (.+?)\n((?:\t\t.*\n)+)", text, re.MULTILINE
    ):
        name = unquote(block.group(1).strip(), path.stem)
        body = block.group(2)
        dtype = re.search(r"dataType: (\S+)", body)
        source = re.search(r"sourceColumn: (\S+)", body)
        table.columns[name] = dtype.group(1) if dtype else "?"
        if source:
            table.source_columns.append(source.group(1))

    # Measures: 'measure 'Name' = <dax>' single or multi line.
    for block in re.finditer(
        r"^\tmeasure ('(?:[^']|'')+'|\w+) =([^\n]*)\n((?:\t{3,}.*\n)*)",
        text, re.MULTILINE
    ):
        dax = block.group(2).strip() + "\n" + block.group(3)
        table.measures.append((unquote(block.group(1), path.stem), dax))

    return table


def unquote(raw: str, where: str) -> str:
    """
    Decode a TMDL object name, and fail any name that needed quoting but was
    not quoted. Power BI rejects `column Opened At` with a misleading
    'indentation' error; this check reproduces that rule so it fails here.
    """
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    check(f"V0 {where}: {raw!r} quoted",
          re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw) is not None,
          "name with spaces/special characters must be single-quoted")
    return raw


def load_model() -> dict[str, Table]:
    if not DEFINITION.exists():
        raise FileNotFoundError(
            f"{DEFINITION} not found. Run: python src/05_generate_powerbi_project.py"
        )
    model_text = (DEFINITION / "model.tmdl").read_text(encoding="utf-8")
    declared = re.findall(r"^ref table (\S+)$", model_text, re.MULTILINE)

    tables: dict[str, Table] = {}
    print("\n--- V1 declared tables have definition files ---")
    for name in declared:
        path = DEFINITION / "tables" / f"{name}.tmdl"
        check(f"V1 {name}", path.exists(), f"missing {path.name}")
        if path.exists():
            tables[name] = parse_table(path)

    orphans = {
        p.stem for p in (DEFINITION / "tables").glob("*.tmdl")
    } - set(declared)
    check("V1 no orphan table files", not orphans, f"{sorted(orphans)}")
    return tables


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def validate_relationships(tables: dict[str, Table]) -> None:
    print("\n--- V2 relationships resolve on both sides ---")
    text = (DEFINITION / "relationships.tmdl").read_text(encoding="utf-8")
    pairs = re.findall(
        r"fromColumn: (\S+)\.(\S+)\n\ttoColumn: (\S+)\.(\S+)", text
    )
    check("V2 relationships parsed", len(pairs) > 0, "none found")

    for ft, fc, tt, tc in pairs:
        label = f"V2 {ft}.{fc} -> {tt}.{tc}"
        if ft not in tables or tt not in tables:
            check(label, False, "table missing from model")
            continue
        from_ok = fc in tables[ft].columns
        to_ok = tc in tables[tt].columns
        check(label, from_ok and to_ok,
              f"from={'ok' if from_ok else 'MISSING'} to={'ok' if to_ok else 'MISSING'}")
        if from_ok and to_ok:
            a, b = tables[ft].columns[fc], tables[tt].columns[tc]
            # A relationship across mismatched types is accepted by Power BI and
            # then silently matches nothing.
            check(f"{label} type match", a == b, f"{a} vs {b}")


def validate_dax(tables: dict[str, Table]) -> None:
    print("\n--- V3 DAX references resolve ---")
    all_measures = {m for t in tables.values() for m, _ in t.measures}

    for table in tables.values():
        for name, dax in table.measures:
            # table[column] references
            for ref_table, ref_col in re.findall(r"(\w+)\[([^\]]+)\]", dax):
                ok = ref_table in tables and ref_col in tables[ref_table].columns
                check(f"V3 '{name}' -> {ref_table}[{ref_col}]", ok, "unresolved")

            # bare [Measure] references
            stripped = re.sub(r"\w+\[[^\]]+\]", "", dax)
            for ref in re.findall(r"\[([^\]]+)\]", stripped):
                check(f"V3 '{name}' -> [{ref}]", ref in all_measures, "unknown measure")


def validate_report(tables: dict[str, Table]) -> None:
    """
    Walk the PBIR tree. The report is a folder of small JSON documents rather
    than one report.json, so the checks are structural as well as semantic:
    a page listed in pageOrder with no page.json on disk is a report that
    opens with a missing tab, and Power BI gives no warning about it.
    """
    print("\n--- V4/V5 report visuals resolve ---")
    all_measures = {m for t in tables.values() for m, _ in t.measures}

    pages_json = REPORT_DEF / "pages" / "pages.json"
    check("V4 pages.json exists", pages_json.exists(), f"missing {pages_json}")
    if not pages_json.exists():
        return
    meta = json.loads(pages_json.read_text(encoding="utf-8"))
    order = meta.get("pageOrder", [])
    check("V4 pageOrder non-empty", bool(order), "no pages declared")
    check("V4 activePageName in pageOrder",
          meta.get("activePageName") in order,
          f"{meta.get('activePageName')!r} not among {order}")

    on_disk = {p.name for p in (REPORT_DEF / "pages").iterdir() if p.is_dir()}
    check("V4 pageOrder matches folders", set(order) == on_disk,
          f"declared {sorted(order)} vs on disk {sorted(on_disk)}")

    n_visuals = 0
    for page_name in order:
        page_dir = REPORT_DEF / "pages" / page_name
        page_file = page_dir / "page.json"
        if not page_file.exists():
            check(f"V4 [{page_name}] page.json", False, "missing")
            continue
        page = json.loads(page_file.read_text(encoding="utf-8"))
        label = page.get("displayName", page_name)
        check(f"V4 [{label}] name matches folder", page.get("name") == page_name,
              f"page.json name is {page.get('name')!r}")

        for vfile in sorted((page_dir / "visuals").glob("*/visual.json")):
            n_visuals += 1
            v = json.loads(vfile.read_text(encoding="utf-8"))
            # Position is required by the schema; a visual missing height or
            # width is accepted by the file format and then never drawn.
            pos = v.get("position", {})
            check(f"V4 [{label}] {vfile.parent.name} position",
                  all(k in pos for k in ("x", "y", "width", "height")),
                  f"incomplete position {pos}")

            query_state = v.get("visual", {}).get("query", {}).get("queryState", {})
            for role, state in query_state.items():
                for proj in state.get("projections", []):
                    field = proj["field"]
                    if "Column" in field:
                        entity = field["Column"]["Expression"]["SourceRef"]["Entity"]
                        prop = field["Column"]["Property"]
                        ok = entity in tables and prop in tables[entity].columns
                        check(f"V4 [{label}] {entity}.{prop}", ok, "column not in model")
                    else:
                        entity = field["Measure"]["Expression"]["SourceRef"]["Entity"]
                        prop = field["Measure"]["Property"]
                        check(f"V4 [{label}] [{prop}]", prop in all_measures,
                              "measure not in model")
                    # queryRef is what the visual uses to tie a projection back
                    # to its field. If it disagrees with the field it names,
                    # the tile renders blank rather than erroring.
                    check(f"V5 [{label}] {role} -> {proj['queryRef']}",
                          proj["queryRef"] == f"{entity}.{prop}",
                          f"queryRef disagrees with field {entity}.{prop}")

    print(f"  checked {n_visuals} visuals across {len(order)} pages")


def validate_sources(tables: dict[str, Table]) -> None:
    print("\n--- V6 partitions match the exported CSV headers ---")
    for name, table in tables.items():
        path = CSV_DIR / f"{name}.csv"
        if not path.exists():
            check(f"V6 {name}", False, f"{path.name} not exported")
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            header = next(csv.reader(fh))
        check(f"V6 {name}", header == table.source_columns,
              f"model {table.source_columns} vs csv {header}")


def main() -> int:
    print(f"Validating {PBI_DIR}")
    tables = load_model()
    validate_relationships(tables)
    validate_dax(tables)
    validate_report(tables)
    validate_sources(tables)

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Power BI project validated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
