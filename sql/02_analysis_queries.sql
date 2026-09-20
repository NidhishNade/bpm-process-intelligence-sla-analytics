-- ===========================================================================
-- Step 6 - Analytical queries
--
-- One query per business question from docs/01_business_requirements.md.
-- Executed by src/04_run_analysis.py, which writes the results to
-- docs/03_analysis_findings.md.
--
-- Queries are separated by a '-- @name:' marker so the runner can label output.
-- All of this is portable ANSI SQL and runs unchanged on PostgreSQL.
--
-- MEASUREMENT RULES applied consistently throughout:
--   * SLA attainment counts CASES, never events (DQ-09).
--   * Cycle time uses opened_at -> resolved_at and is filtered to
--     has_valid_resolution_time = 1 (DQ-07, DQ-08).
--   * MEDIAN is reported alongside AVG wherever the distribution is skewed;
--     incident durations have a long right tail, so a mean alone misleads.
-- ===========================================================================


-- @name: Q01_overall_sla_and_volume
-- Headline KPIs. The single row an executive page opens with.
SELECT
    COUNT(*)                                              AS total_incidents,
    SUM(is_sla_met)                                       AS sla_met,
    SUM(is_sla_breached)                                  AS sla_breached,
    ROUND(100.0 * SUM(is_sla_met) / COUNT(*), 2)          AS sla_attainment_pct,
    SUM(has_valid_resolution_time)                        AS measurable_incidents,
    ROUND(MEDIAN(resolution_hours) FILTER (WHERE has_valid_resolution_time = 1), 2)
                                                          AS median_resolution_hours,
    ROUND(AVG(resolution_hours)    FILTER (WHERE has_valid_resolution_time = 1), 2)
                                                          AS avg_resolution_hours
FROM fact_incident_case;


-- @name: Q02_sla_trend_by_month
-- Q2: is attainment stable, improving or degrading across the window?
SELECT
    d.year_month,
    COUNT(*)                                     AS incidents,
    SUM(f.is_sla_breached)                       AS breached,
    ROUND(100.0 * SUM(f.is_sla_met) / COUNT(*), 2) AS sla_attainment_pct,
    ROUND(MEDIAN(f.resolution_hours) FILTER (WHERE f.has_valid_resolution_time = 1), 2)
                                                 AS median_resolution_hours
FROM fact_incident_case f
JOIN dim_date d ON d.date_key = f.opened_date_key
GROUP BY d.year_month
ORDER BY d.year_month;


-- @name: Q04_sla_by_priority
-- Q4: are breaches concentrated in Critical/High, or hidden in the Moderate bulk?
SELECT
    p.priority_label,
    COUNT(*)                                       AS incidents,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_volume,
    SUM(f.is_sla_breached)                         AS breached,
    ROUND(100.0 * SUM(f.is_sla_met) / COUNT(*), 2) AS sla_attainment_pct,
    ROUND(100.0 * SUM(f.is_sla_breached) / SUM(SUM(f.is_sla_breached)) OVER (), 2)
                                                   AS pct_of_all_breaches,
    ROUND(MEDIAN(f.resolution_hours) FILTER (WHERE f.has_valid_resolution_time = 1), 2)
                                                   AS median_resolution_hours
FROM fact_incident_case f
JOIN dim_priority p ON p.priority_key = f.priority_key
GROUP BY p.priority_label, p.priority_rank
ORDER BY p.priority_rank;


-- @name: Q05_bottleneck_by_state
-- Q5/Q6: where does elapsed time actually accumulate?
-- Uses the event fact: total dwell per state across the whole process.
SELECT
    s.state_name,
    s.state_category,
    COUNT(*)                                          AS event_occurrences,
    COUNT(e.dwell_hours)                              AS measurable_dwells,
    ROUND(SUM(e.dwell_hours), 0)                      AS total_dwell_hours,
    ROUND(100.0 * SUM(e.dwell_hours) / SUM(SUM(e.dwell_hours)) OVER (), 2)
                                                      AS pct_of_total_elapsed,
    ROUND(AVG(e.dwell_hours), 2)                      AS avg_dwell_hours,
    ROUND(MEDIAN(e.dwell_hours), 2)                   AS median_dwell_hours
FROM fact_incident_event e
JOIN dim_state s ON s.state_key = e.state_key
WHERE e.dwell_hours IS NOT NULL
GROUP BY s.state_name, s.state_category
ORDER BY total_dwell_hours DESC;


-- @name: Q06_working_vs_waiting_split
-- Q6: how much of total elapsed time is the team blocked rather than working?
SELECT
    s.state_category,
    ROUND(SUM(e.dwell_hours), 0)                      AS total_hours,
    ROUND(100.0 * SUM(e.dwell_hours) / SUM(SUM(e.dwell_hours)) OVER (), 2) AS pct_of_elapsed
FROM fact_incident_event e
JOIN dim_state s ON s.state_key = e.state_key
WHERE e.dwell_hours IS NOT NULL
GROUP BY s.state_category
ORDER BY total_hours DESC;


-- @name: Q07_bottleneck_met_vs_breached
-- Q7: is the bottleneck the same for incidents that met SLA and those that missed?
-- This is the diagnostic that turns "Active is slow" into an actionable finding.
SELECT
    s.state_name,
    ROUND(AVG(e.dwell_hours) FILTER (WHERE f.is_sla_met = 1), 2)     AS avg_dwell_met,
    ROUND(AVG(e.dwell_hours) FILTER (WHERE f.is_sla_breached = 1), 2) AS avg_dwell_breached,
    ROUND(
        AVG(e.dwell_hours) FILTER (WHERE f.is_sla_breached = 1)
      - AVG(e.dwell_hours) FILTER (WHERE f.is_sla_met = 1), 2)        AS gap_hours
FROM fact_incident_event e
JOIN dim_state s          ON s.state_key = e.state_key
JOIN fact_incident_case f ON f.case_key  = e.case_key
WHERE e.dwell_hours IS NOT NULL
GROUP BY s.state_name
ORDER BY gap_hours DESC NULLS LAST;


-- @name: Q08_sla_by_assignment_group
-- Q8: which teams are outliers once volume is controlled for?
-- The volume floor stops a group with 4 incidents appearing as a top performer.
SELECT
    g.assignment_group_name,
    COUNT(*)                                       AS incidents,
    SUM(f.is_sla_breached)                         AS breached,
    ROUND(100.0 * SUM(f.is_sla_met) / COUNT(*), 2) AS sla_attainment_pct,
    ROUND(MEDIAN(f.resolution_hours) FILTER (WHERE f.has_valid_resolution_time = 1), 2)
                                                   AS median_resolution_hours,
    ROUND(AVG(CAST(f.reassignment_count AS DOUBLE)), 2) AS avg_reassignments
FROM fact_incident_case f
JOIN dim_assignment_group g ON g.assignment_group_key = f.assignment_group_key
GROUP BY g.assignment_group_name
HAVING COUNT(*) >= 100
ORDER BY sla_attainment_pct ASC
LIMIT 15;


-- @name: Q09_sla_by_contact_type
-- Q9: does the intake channel affect outcome?
SELECT
    c.contact_type_name,
    COUNT(*)                                       AS incidents,
    ROUND(100.0 * SUM(f.is_sla_met) / COUNT(*), 2) AS sla_attainment_pct,
    ROUND(MEDIAN(f.resolution_hours) FILTER (WHERE f.has_valid_resolution_time = 1), 2)
                                                   AS median_resolution_hours
FROM fact_incident_case f
JOIN dim_contact_type c ON c.contact_type_key = f.contact_type_key
GROUP BY c.contact_type_name
ORDER BY incidents DESC;


-- @name: Q10_knowledge_article_effect
-- Q10: does attaching a knowledge article correlate with a better outcome?
SELECT
    CASE WHEN used_knowledge_article = 1 THEN 'Knowledge article used'
         ELSE 'No knowledge article' END           AS knowledge_usage,
    COUNT(*)                                       AS incidents,
    ROUND(100.0 * SUM(is_sla_met) / COUNT(*), 2)   AS sla_attainment_pct,
    ROUND(MEDIAN(resolution_hours) FILTER (WHERE has_valid_resolution_time = 1), 2)
                                                   AS median_resolution_hours,
    ROUND(AVG(CAST(reassignment_count AS DOUBLE)), 2) AS avg_reassignments
FROM fact_incident_case
GROUP BY used_knowledge_article
ORDER BY used_knowledge_article DESC;


-- @name: Q11_top_process_variants
-- Q11: how concentrated is routing behaviour?
SELECT
    v.variant_rank,
    v.variant_path,
    v.variant_length,
    COUNT(*)                                       AS incidents,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_incidents,
    ROUND(100.0 * SUM(f.is_sla_met) / COUNT(*), 2) AS sla_attainment_pct,
    ROUND(MEDIAN(f.resolution_hours) FILTER (WHERE f.has_valid_resolution_time = 1), 2)
                                                   AS median_resolution_hours
FROM fact_incident_case f
JOIN dim_variant v ON v.variant_key = f.variant_key
GROUP BY v.variant_rank, v.variant_path, v.variant_length
ORDER BY incidents DESC
LIMIT 10;


-- @name: Q12_happy_path_vs_exception
-- Q12: do non-standard routing paths perform materially worse?
SELECT
    CASE WHEN v.variant_rank <= 5 THEN 'Top 5 variants'
         ELSE 'Long-tail variants' END             AS variant_group,
    COUNT(DISTINCT v.variant_path)                 AS distinct_variants,
    COUNT(*)                                       AS incidents,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_incidents,
    ROUND(100.0 * SUM(f.is_sla_met) / COUNT(*), 2) AS sla_attainment_pct,
    ROUND(MEDIAN(f.resolution_hours) FILTER (WHERE f.has_valid_resolution_time = 1), 2)
                                                   AS median_resolution_hours,
    ROUND(AVG(CAST(f.event_count AS DOUBLE)), 1)   AS avg_events_per_incident
FROM fact_incident_case f
JOIN dim_variant v ON v.variant_key = f.variant_key
GROUP BY variant_group
ORDER BY incidents DESC;


-- @name: Q13_reassignment_impact
-- Q13: does each additional handoff measurably degrade the outcome?
-- Buckets the long tail so the result stays readable.
SELECT
    CASE WHEN reassignment_count >= 5 THEN '5+'
         ELSE CAST(reassignment_count AS VARCHAR) END AS reassignments,
    COUNT(*)                                       AS incidents,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_incidents,
    ROUND(100.0 * SUM(is_sla_met) / COUNT(*), 2)   AS sla_attainment_pct,
    ROUND(MEDIAN(resolution_hours) FILTER (WHERE has_valid_resolution_time = 1), 2)
                                                   AS median_resolution_hours
FROM fact_incident_case
GROUP BY reassignments
ORDER BY MIN(reassignment_count);


-- @name: Q15_resolver_workload_concentration
-- Q15: is workload spread evenly, or concentrated on a handful of people?
WITH per_resolver AS (
    SELECT
        r.resolver_name,
        COUNT(*) AS incidents,
        ROUND(100.0 * SUM(f.is_sla_met) / COUNT(*), 2) AS sla_attainment_pct
    FROM fact_incident_case f
    JOIN dim_resolver r ON r.resolver_key = f.resolver_key
    WHERE r.resolver_name <> 'Unassigned'
    GROUP BY r.resolver_name
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (ORDER BY incidents DESC) AS rn,
           SUM(incidents) OVER ()                      AS total_incidents,
           COUNT(*)      OVER ()                       AS total_resolvers
    FROM per_resolver
)
SELECT
    'Top 10 resolvers'  AS cohort,
    COUNT(*)            AS resolvers,
    SUM(incidents)      AS incidents,
    ROUND(100.0 * SUM(incidents) / MIN(total_incidents), 2) AS pct_of_volume,
    ROUND(AVG(sla_attainment_pct), 2)                       AS avg_sla_attainment_pct
FROM ranked WHERE rn <= 10
UNION ALL
SELECT
    'All other resolvers',
    COUNT(*),
    SUM(incidents),
    ROUND(100.0 * SUM(incidents) / MIN(total_incidents), 2),
    ROUND(AVG(sla_attainment_pct), 2)
FROM ranked WHERE rn > 10;


-- @name: Q16_arrival_by_hour
-- Q16: when does demand arrive? Drives shift-pattern recommendations.
SELECT
    opened_hour,
    COUNT(*)                                     AS incidents,
    ROUND(100.0 * SUM(is_sla_met) / COUNT(*), 2) AS sla_attainment_pct
FROM fact_incident_case
GROUP BY opened_hour
ORDER BY opened_hour;


-- @name: Q17_out_of_hours_effect
-- Q17: are incidents raised outside business hours resolved more slowly?
SELECT
    CASE WHEN opened_out_of_hours = 1 THEN 'Opened out of hours'
         ELSE 'Opened in business hours' END      AS window,
    COUNT(*)                                      AS incidents,
    ROUND(100.0 * SUM(is_sla_met) / COUNT(*), 2)  AS sla_attainment_pct,
    ROUND(MEDIAN(resolution_hours) FILTER (WHERE has_valid_resolution_time = 1), 2)
                                                  AS median_resolution_hours
FROM fact_incident_case
GROUP BY opened_out_of_hours
ORDER BY opened_out_of_hours;


-- @name: Q18_reopen_impact
-- Q18: what does rework cost?
SELECT
    CASE WHEN is_reopened = 1 THEN 'Reopened at least once'
         ELSE 'Never reopened' END                AS rework,
    COUNT(*)                                      AS incidents,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_incidents,
    ROUND(100.0 * SUM(is_sla_met) / COUNT(*), 2)  AS sla_attainment_pct,
    ROUND(MEDIAN(resolution_hours) FILTER (WHERE has_valid_resolution_time = 1), 2)
                                                  AS median_resolution_hours,
    ROUND(AVG(CAST(event_count AS DOUBLE)), 1)    AS avg_events_per_incident
FROM fact_incident_case
GROUP BY is_reopened
ORDER BY is_reopened DESC;


-- @name: Q19_closed_code_and_reopening
-- Q19: which resolution codes are associated with rework?
SELECT
    cc.closed_code_name,
    COUNT(*)                                      AS incidents,
    SUM(f.is_reopened)                            AS reopened,
    ROUND(100.0 * SUM(f.is_reopened) / COUNT(*), 2) AS reopen_rate_pct,
    ROUND(100.0 * SUM(f.is_sla_met) / COUNT(*), 2)  AS sla_attainment_pct
FROM fact_incident_case f
JOIN dim_closed_code cc ON cc.closed_code_key = f.closed_code_key
GROUP BY cc.closed_code_name
HAVING COUNT(*) >= 50
ORDER BY reopen_rate_pct DESC
LIMIT 10;


-- @name: Q20_state_transition_matrix
-- Feeds the process-map visual: the most frequent edges between states.
SELECT
    s_from.state_name                             AS from_state,
    s_to.state_name                               AS to_state,
    COUNT(*)                                      AS transitions,
    ROUND(AVG(e.dwell_hours), 2)                  AS avg_hours_before_transition
FROM fact_incident_event e
JOIN dim_state s_from ON s_from.state_key = e.state_key
JOIN dim_state s_to   ON s_to.state_key   = e.next_state_key
WHERE e.next_state_key <> 0
GROUP BY s_from.state_name, s_to.state_name
ORDER BY transitions DESC
LIMIT 15;
