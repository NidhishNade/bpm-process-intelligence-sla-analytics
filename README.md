# BPM Process Intelligence & SLA Analytics Platform

End-to-end Business Intelligence solution over an enterprise **IT Incident Management**
event log (141,712 events across 24,918 incidents, Feb 2016 – Feb 2017), built to answer
a question that conventional ITSM reporting cannot: *we know our SLA miss rate, but we
do not know what is causing it.*

The project treats the extract as a **process-mining event log** rather than a flat
table — case, activity, timestamp, resource — and builds a dimensional model and BI
semantic layer on top of it.

> **Status: in progress.** Steps 0–2 complete. See [Build progress](#build-progress).

---

## Stack and the reasoning behind it

| Layer | Tool | Why |
|---|---|---|
| Acquisition & preparation | Python / pandas | Event-log reshaping, dwell-time derivation and data-quality gates are procedural work that SQL handles awkwardly |
| Analytical store | DuckDB (Postgres-portable ANSI SQL) | Single-file, zero-install analytical engine with full window-function support; DDL is written to port to PostgreSQL unchanged |
| Data model | Star schema | Conformed dimensions over a case-grain and an event-grain fact |
| Reporting & semantics | Power BI + DAX | Semantic layer, KPI definitions, drill-through |

**Deliberately excluded:** ML, cloud warehousing and streaming. The dataset is a static
46 MB historical extract, and none of the three would change a decision any stakeholder
in this scenario is making. Adding them would be résumé-driven architecture.

---

## Data source

- **Kaggle:** [Process Mining Event Log: Incident Management](https://www.kaggle.com/datasets/albertopmd/process-mining-event-log-incident-management)
- **Origin:** UCI ML Repository dataset 498, *Incident management process enriched event log*

`src/00_download_data.py` pulls from the UCI archive because it requires no API
credentials, keeping the pipeline reproducible for anyone cloning this repo. Raw data is
`.gitignore`d; run the script to reproduce it.

---

## Quickstart

```bash
pip install -r requirements.txt
python src/00_download_data.py
python src/01_profile_and_quality.py
```

---

## What profiling actually found

Three findings from Step 2 shaped every downstream modelling decision. Full evidence in
[`data/quality/dq_report.md`](data/quality/dq_report.md).

**1. `closed_at` is a batch artefact, not a business event (DQ-08).**
Five timestamps account for ~13,800 closures — 24/3/2016 18:40–19:01. That is a
housekeeping job mass-closing a backlog, not agents completing work. Any cycle-time
metric built on `closed_at` would be measuring the batch job. **`resolved_at` is used
instead.**

**2. `made_sla` is a live flag, not a static outcome (DQ-09).**
It changes mid-case for 9,114 of 24,918 incidents (36.6%) — it flips the moment the
clock breaches. Aggregating it at event grain would overstate attainment. **The
case-grain fact takes the last observed value per case.**

**3. Five columns are effectively empty (DQ-05).**
`caused_by` (99.98% unknown), `vendor`, `cmdb_ci`, `rfc`, `problem_id` are all >98%
unknown. They are excluded from the model rather than shipped as near-empty attributes
that invite false conclusions from small non-null subsets.

Also handled: minute-precision timestamps mean `(case, timestamp)` is not unique for 10%
of rows (DQ-02 — so the fact table carries a surrogate key and an explicit sequence
number), a `-100` sentinel state (DQ-06), and 1,556 terminal cases with no
`resolved_at` (DQ-07 — excluded from the cycle-time denominator, not counted as zero).

---

## Repository layout

```
data/
  raw/        source event log (gitignored, reproducible via src/00)
  processed/  modelled outputs (gitignored)
  quality/    profiling + data-quality evidence (committed)
docs/         requirements, model documentation, KPI definitions
sql/          schema DDL and analytical queries
src/          Python pipeline
powerbi/      .pbix report and DAX measure documentation
```

---

## Build progress

- [x] **Step 0** — Reproducible data acquisition with structural verification
- [x] **Step 1** — [Business requirements & 19 business questions](docs/01_business_requirements.md)
- [x] **Step 2** — [Profiling & 12 data-quality checks](data/quality/dq_report.md)
- [ ] Step 3 — Event-log interpretation & Python transformation
- [ ] Step 4 — SQL schema and analytical queries
- [ ] Step 5 — Star schema
- [ ] Step 6 — KPI definitions and DAX
- [ ] Step 7 — Power BI report pages
- [ ] Step 8 — Findings and recommendations

---

## Author

**Nidhish Nade** — BPM Developer (4+ years: BPMN, workflow automation, Java/Spring Boot,
SQL, APIs) transitioning into Business Intelligence. This project applies process
knowledge from BPM engineering to a BI toolchain.
