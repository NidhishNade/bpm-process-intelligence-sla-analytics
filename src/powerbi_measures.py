"""
Step 7 - DAX measure definitions.

Single source of truth for the semantic layer. These are injected into the TMDL
model by 05_generate_powerbi_project.py and rendered into docs/04_kpi_and_dax.md,
so the documentation and the model can never disagree.

DESIGN RULES APPLIED THROUGHOUT

1. Measures aggregate, they never iterate over the event table row by row.
   All sequence logic was precomputed in Step 3. A measure like
   SUMX(fact_incident_event, ...) would re-scan 141,707 rows in every filter
   context and is the usual reason a Power BI report feels slow.

2. Outcome flags are stored as 0/1 integers, so a conditional count is
   SUM(flag) rather than CALCULATE(COUNTROWS(), filter). SUM over an integer
   column is the cheapest operation VertiPaq performs; the filtered version
   builds a filter context on every evaluation.

3. Every ratio guards its denominator with DIVIDE(), never '/'. DIVIDE returns
   BLANK on a zero denominator instead of raising, so a slicer combination with
   no rows renders as empty rather than as an error across the whole visual.

4. Cycle-time measures filter on has_valid_resolution_time = 1. That exclusion
   rule is defined once, in the fact table, and referenced everywhere - so the
   1,556 incidents with no resolved_at (DQ-07) can never silently be counted as
   zero-duration and drag an average down.
"""

from __future__ import annotations

# (name, dax, format_string, display_folder, description)
MEASURES: list[tuple[str, str, str, str, str]] = [
    # ---------------------------------------------------------------- volume
    (
        "Total Incidents",
        "COUNTROWS ( fact_incident_case )",
        "#,0",
        "01 Volume",
        "Distinct incidents. Counted at case grain, never event grain - the event "
        "table holds 141,707 rows for 24,918 incidents, so counting there would "
        "overstate volume roughly 5.7x.",
    ),
    (
        "Total Events",
        "COUNTROWS ( fact_incident_event )",
        "#,0",
        "01 Volume",
        "Process steps recorded across all incidents.",
    ),
    (
        "Avg Events per Incident",
        "DIVIDE ( [Total Events], [Total Incidents] )",
        "#,0.0",
        "01 Volume",
        "Process touch count. A rising value means more handling per incident.",
    ),
    # ------------------------------------------------------------------- SLA
    (
        "SLA Met",
        "SUM ( fact_incident_case[is_sla_met] )",
        "#,0",
        "02 SLA",
        "Incidents whose final observed made_sla value was true. Last-value "
        "semantics per DQ-09.",
    ),
    (
        "SLA Breached",
        "SUM ( fact_incident_case[is_sla_breached] )",
        "#,0",
        "02 SLA",
        "Incidents whose final observed made_sla value was false.",
    ),
    (
        "SLA Attainment %",
        "DIVIDE ( [SLA Met], [Total Incidents] )",
        "0.0%",
        "02 SLA",
        "Headline KPI. Denominator is ALL incidents, not just resolved ones - "
        "excluding unresolved incidents would flatter the number by hiding the "
        "cases most likely to have breached.",
    ),
    (
        "SLA Breach %",
        "DIVIDE ( [SLA Breached], [Total Incidents] )",
        "0.0%",
        "02 SLA",
        "Complement of attainment. Held as its own measure so breach-focused "
        "visuals do not have to express 1 - x.",
    ),
    (
        "SLA Attainment % PM",
        "CALCULATE ( [SLA Attainment %], DATEADD ( dim_date[date_value], -1, MONTH ) )",
        "0.0%",
        "02 SLA",
        "Prior-month attainment. Requires dim_date to be marked as the date table.",
    ),
    (
        "SLA Attainment % MoM",
        "VAR Current = [SLA Attainment %]\n"
        "VAR Prior = [SLA Attainment % PM]\n"
        "RETURN IF ( NOT ISBLANK ( Prior ), Current - Prior )",
        "+0.0%;-0.0%;0.0%",
        "02 SLA",
        "Month-over-month change in percentage points. Returns blank rather than "
        "a misleading full-value delta when there is no prior month.",
    ),
    # ------------------------------------------------------------ cycle time
    (
        "Incidents with Valid Cycle Time",
        "SUM ( fact_incident_case[has_valid_resolution_time] )",
        "#,0",
        "03 Cycle Time",
        "The cycle-time denominator. Shown on the report so the audience can see "
        "how many incidents the duration KPIs are actually based on.",
    ),
    (
        "Median Resolution Hours",
        "CALCULATE (\n"
        "    MEDIAN ( fact_incident_case[resolution_hours] ),\n"
        "    fact_incident_case[has_valid_resolution_time] = 1\n"
        ")",
        "#,0.0",
        "03 Cycle Time",
        "Primary cycle-time KPI. Median rather than mean: the distribution has a "
        "long right tail, and the mean (178h) is over eight times the median "
        "(22h), so a mean would describe almost no real incident.",
    ),
    (
        "Avg Resolution Hours",
        "CALCULATE (\n"
        "    AVERAGE ( fact_incident_case[resolution_hours] ),\n"
        "    fact_incident_case[has_valid_resolution_time] = 1\n"
        ")",
        "#,0.0",
        "03 Cycle Time",
        "Retained alongside the median so the skew is visible rather than hidden.",
    ),
    (
        "Median Resolution Days",
        "DIVIDE ( [Median Resolution Hours], 24 )",
        "#,0.0",
        "03 Cycle Time",
        "Same measure in the unit business stakeholders tend to think in.",
    ),
    (
        "P90 Resolution Hours",
        "CALCULATE (\n"
        "    PERCENTILE.INC ( fact_incident_case[resolution_hours], 0.9 ),\n"
        "    fact_incident_case[has_valid_resolution_time] = 1\n"
        ")",
        "#,0.0",
        "03 Cycle Time",
        "Tail risk. The median describes the typical incident; P90 describes the "
        "experience that generates escalations.",
    ),
    # ------------------------------------------------------------ bottleneck
    (
        "Total Dwell Hours",
        "SUM ( fact_incident_event[dwell_hours] )",
        "#,0",
        "04 Bottleneck",
        "Total elapsed time accumulated in the filtered states. Nulls (the final "
        "event of each case, which has no successor) are ignored by SUM, which is "
        "the correct behaviour - that time is genuinely unmeasured, not zero.",
    ),
    (
        "Avg Dwell Hours",
        "AVERAGE ( fact_incident_event[dwell_hours] )",
        "#,0.0",
        "04 Bottleneck",
        "Mean time an incident sits in a state before moving on.",
    ),
    (
        "Median Dwell Hours",
        "MEDIAN ( fact_incident_event[dwell_hours] )",
        "#,0.0",
        "04 Bottleneck",
        "Typical dwell. Diverges sharply from the mean in the Active state, which "
        "is the signal that a minority of incidents are stalling there.",
    ),
    (
        "Waiting Hours",
        "CALCULATE ( [Total Dwell Hours], dim_state[state_category] = \"Waiting\" )",
        "#,0",
        "04 Bottleneck",
        "Elapsed time blocked on a third party: user, vendor, problem or evidence.",
    ),
    (
        "Working Hours",
        "CALCULATE ( [Total Dwell Hours], dim_state[state_category] = \"Working\" )",
        "#,0",
        "04 Bottleneck",
        "Elapsed time in New or Active, where the resolver team holds the work.",
    ),
    (
        "Waiting Time %",
        "DIVIDE ( [Waiting Hours], [Total Dwell Hours] )",
        "0.0%",
        "04 Bottleneck",
        "Share of elapsed time the process is blocked. Separates a capacity "
        "problem (fix by adding people) from a dependency problem (fix by "
        "changing the process).",
    ),
    (
        "% of Total Elapsed Time",
        "DIVIDE (\n"
        "    [Total Dwell Hours],\n"
        "    CALCULATE ( [Total Dwell Hours], REMOVEFILTERS ( dim_state ) )\n"
        ")",
        "0.0%",
        "04 Bottleneck",
        "Each state's share of total elapsed time. REMOVEFILTERS only on dim_state "
        "so the denominator still respects date and priority slicers - the ratio "
        "stays meaningful inside any filtered view.",
    ),
    # ---------------------------------------------------------- reassignment
    (
        "Reassigned Incidents",
        "SUM ( fact_incident_case[is_reassigned] )",
        "#,0",
        "05 Workload",
        "Incidents handed between groups at least once.",
    ),
    (
        "Reassignment Rate %",
        "DIVIDE ( [Reassigned Incidents], [Total Incidents] )",
        "0.0%",
        "05 Workload",
        "Routing accuracy proxy. A high rate means work is not reaching the right "
        "team first time.",
    ),
    (
        "Avg Reassignments",
        "AVERAGE ( fact_incident_case[reassignment_count] )",
        "#,0.00",
        "05 Workload",
        "Mean handoffs per incident.",
    ),
    (
        "Total Reassignments",
        "SUM ( fact_incident_case[reassignment_count] )",
        "#,0",
        "05 Workload",
        "Absolute handoff volume - the size of the rework pool.",
    ),
    # ----------------------------------------------------------------- rework
    (
        "Reopened Incidents",
        "SUM ( fact_incident_case[is_reopened] )",
        "#,0",
        "06 Quality",
        "Incidents reopened at least once after being resolved.",
    ),
    (
        "Reopen Rate %",
        "DIVIDE ( [Reopened Incidents], [Total Incidents] )",
        "0.0%",
        "06 Quality",
        "First-time-fix failure rate. The cleanest available proxy for resolution "
        "quality as opposed to resolution speed.",
    ),
    (
        "Knowledge Article Usage %",
        "DIVIDE ( SUM ( fact_incident_case[used_knowledge_article] ), [Total Incidents] )",
        "0.0%",
        "06 Quality",
        "Share of incidents resolved with a knowledge article attached.",
    ),
    # --------------------------------------------------------------- variants
    (
        "Distinct Variants",
        "DISTINCTCOUNT ( fact_incident_case[variant_key] )",
        "#,0",
        "07 Process Variants",
        "Number of distinct routing paths in the filtered population. Process "
        "complexity in a single number.",
    ),
    (
        "Happy Path Incidents",
        "CALCULATE ( [Total Incidents], dim_variant[variant_rank] = 1 )",
        "#,0",
        "07 Process Variants",
        "Incidents following the single most frequent path.",
    ),
    (
        "Happy Path %",
        "DIVIDE (\n"
        "    [Happy Path Incidents],\n"
        "    CALCULATE ( [Total Incidents], REMOVEFILTERS ( dim_variant ) )\n"
        ")",
        "0.0%",
        "07 Process Variants",
        "Process conformance. REMOVEFILTERS on dim_variant keeps the denominator "
        "as the whole population even when a variant visual is cross-filtering.",
    ),
    (
        "Top 5 Variant %",
        "DIVIDE (\n"
        "    CALCULATE ( [Total Incidents], dim_variant[variant_rank] <= 5 ),\n"
        "    CALCULATE ( [Total Incidents], REMOVEFILTERS ( dim_variant ) )\n"
        ")",
        "0.0%",
        "07 Process Variants",
        "Share of volume on the five dominant paths - the standardised core of "
        "the process.",
    ),
    # ---------------------------------------------------------------- context
    (
        "Out of Hours %",
        "DIVIDE ( SUM ( fact_incident_case[opened_out_of_hours] ), [Total Incidents] )",
        "0.0%",
        "08 Demand",
        "Share of demand arriving outside 08:00-18:00 Mon-Fri.",
    ),
    (
        "Selected Incident",
        "SELECTEDVALUE (\n"
        "    fact_incident_case[incident_number],\n"
        "    \"Pick an incident\"\n"
        ")",
        "",
        "09 Drill-through",
        "Title text for the drill-through page. SELECTEDVALUE returns the fallback "
        "string rather than blanking when more than one incident is in context, so "
        "the page header always explains itself.",
    ),
]
