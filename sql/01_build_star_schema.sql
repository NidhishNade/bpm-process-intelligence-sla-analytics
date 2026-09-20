-- ===========================================================================
-- Step 4/5 - Dimensional model (star schema)
--
-- Executed by src/03_build_warehouse.py against DuckDB. The script assumes two
-- staging views already exist, created by that script over the Parquet output
-- of Step 3:
--
--     stg_events   one row per event  (141,707)
--     stg_cases    one row per case   ( 24,918)
--
-- PORTABILITY: everything below is ANSI SQL and runs unchanged on PostgreSQL.
-- The only DuckDB-specific code in the project is the read_parquet() call that
-- creates those two staging views; on PostgreSQL they would be tables loaded by
-- COPY instead. Isolating the platform dependency in one place is deliberate.
--
-- MODELLING DECISIONS
--
-- 1. Two fact tables, not one. Executive SLA reporting is case-grain
--    (24,918 rows); bottleneck analysis is event-grain (141,707 rows). Forcing
--    both through one table means either double-counting incidents or losing
--    the sequence. They share conformed dimensions, so a slicer on priority
--    filters both correctly.
--
-- 2. Integer surrogate keys on every relationship. The natural keys here are
--    strings such as 'Group 70'. Integer keys compress far better in Power BI's
--    VertiPaq engine and produce materially faster joins and smaller models.
--
-- 3. Every dimension carries an 'Unknown' member at key 0 rather than allowing
--    NULL foreign keys. NULLs in a fact break inner joins and silently drop
--    rows from totals; an explicit Unknown member keeps every incident
--    countable and makes missing data visible instead of invisible.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- DIM_DATE
-- Generated from the observed event window rather than hard-coded, so the model
-- never has date rows that no fact can reach, and never misses one it needs.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_date;
CREATE TABLE dim_date AS
WITH bounds AS (
    SELECT
        CAST(MIN(opened_at) AS DATE) AS min_d,
        CAST(MAX(GREATEST(
            COALESCE(last_event_at, opened_at),
            COALESCE(resolved_at,   opened_at)
        )) AS DATE) AS max_d
    FROM stg_cases
),
calendar AS (
    SELECT CAST(d AS DATE) AS date_value
    FROM bounds, UNNEST(GENERATE_SERIES(min_d, max_d, INTERVAL 1 DAY)) AS t(d)
)
SELECT
    CAST(STRFTIME(date_value, '%Y%m%d') AS INTEGER)      AS date_key,
    date_value,
    CAST(EXTRACT(year    FROM date_value) AS SMALLINT)   AS calendar_year,
    CAST(EXTRACT(quarter FROM date_value) AS SMALLINT)   AS calendar_quarter,
    CAST(EXTRACT(month   FROM date_value) AS SMALLINT)   AS calendar_month,
    STRFTIME(date_value, '%B')                           AS month_name,
    STRFTIME(date_value, '%Y-%m')                        AS year_month,
    CAST(EXTRACT(day     FROM date_value) AS SMALLINT)   AS day_of_month,
    CAST(EXTRACT(dow     FROM date_value) AS SMALLINT)   AS day_of_week_num,  -- 0 = Sunday
    STRFTIME(date_value, '%A')                           AS day_name,
    CASE WHEN EXTRACT(dow FROM date_value) IN (0, 6)
         THEN TRUE ELSE FALSE END                        AS is_weekend
FROM calendar;


-- ---------------------------------------------------------------------------
-- DIM_PRIORITY
-- priority_rank lets Power BI sort '1 - Critical' before '2 - High' instead of
-- alphabetically, which is the usual cause of nonsensical axis ordering.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_priority;
CREATE TABLE dim_priority AS
SELECT 0 AS priority_key, 'Unknown' AS priority_label, 99 AS priority_rank,
       FALSE AS is_high_priority
UNION ALL
SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY priority) AS INTEGER) AS priority_key,
    priority                                               AS priority_label,
    CAST(TRY_CAST(SUBSTR(priority, 1, 1) AS INTEGER) AS INTEGER) AS priority_rank,
    CASE WHEN SUBSTR(priority, 1, 1) IN ('1', '2') THEN TRUE ELSE FALSE END
                                                           AS is_high_priority
FROM (SELECT DISTINCT priority FROM stg_cases WHERE priority IS NOT NULL);


-- ---------------------------------------------------------------------------
-- DIM_STATE
-- state_category (Working / Waiting / Terminal) is the attribute that makes the
-- bottleneck page answerable: it separates time the team spent working from
-- time the process spent blocked on somebody else.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_state;
CREATE TABLE dim_state AS
SELECT 0 AS state_key, 'Unknown' AS state_name, 'Unknown' AS state_category,
       99 AS state_sort_order, FALSE AS is_waiting_state
UNION ALL
SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY MIN(state_sort_order)) AS INTEGER) AS state_key,
    incident_state                                                      AS state_name,
    MIN(state_category)                                                 AS state_category,
    CAST(MIN(state_sort_order) AS INTEGER)                              AS state_sort_order,
    CASE WHEN MIN(state_category) = 'Waiting' THEN TRUE ELSE FALSE END  AS is_waiting_state
FROM stg_events
WHERE incident_state IS NOT NULL
GROUP BY incident_state;


-- ---------------------------------------------------------------------------
-- DIM_ASSIGNMENT_GROUP
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_assignment_group;
CREATE TABLE dim_assignment_group AS
SELECT 0 AS assignment_group_key, 'Unassigned' AS assignment_group_name
UNION ALL
SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY assignment_group) AS INTEGER),
    assignment_group
FROM (SELECT DISTINCT assignment_group FROM stg_cases WHERE assignment_group IS NOT NULL);


-- ---------------------------------------------------------------------------
-- DIM_CATEGORY
-- Category and subcategory are modelled as one dimension at subcategory grain,
-- not two joined dimensions. They form a strict hierarchy, and splitting them
-- would create a snowflake that buys nothing and costs a join.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_category;
CREATE TABLE dim_category AS
SELECT 0 AS category_key, 'Unknown' AS category_name, 'Unknown' AS subcategory_name
UNION ALL
SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY category, subcategory) AS INTEGER),
    category,
    subcategory
FROM (
    SELECT DISTINCT
        COALESCE(category,    'Unknown') AS category,
        COALESCE(subcategory, 'Unknown') AS subcategory
    FROM stg_cases
    WHERE category IS NOT NULL OR subcategory IS NOT NULL
);


-- ---------------------------------------------------------------------------
-- DIM_CONTACT_TYPE  (intake channel)
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_contact_type;
CREATE TABLE dim_contact_type AS
SELECT 0 AS contact_type_key, 'Unknown' AS contact_type_name, FALSE AS is_self_service
UNION ALL
SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY contact_type) AS INTEGER),
    contact_type,
    CASE WHEN contact_type IN ('Self service', 'IVR') THEN TRUE ELSE FALSE END
FROM (SELECT DISTINCT contact_type FROM stg_cases WHERE contact_type IS NOT NULL);


-- ---------------------------------------------------------------------------
-- DIM_LOCATION
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_location;
CREATE TABLE dim_location AS
SELECT 0 AS location_key, 'Unknown' AS location_name
UNION ALL
SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY location) AS INTEGER),
    location
FROM (SELECT DISTINCT location FROM stg_cases WHERE location IS NOT NULL);


-- ---------------------------------------------------------------------------
-- DIM_CLOSED_CODE  (resolution outcome code)
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_closed_code;
CREATE TABLE dim_closed_code AS
SELECT 0 AS closed_code_key, 'Not Closed' AS closed_code_name
UNION ALL
SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY closed_code) AS INTEGER),
    closed_code
FROM (SELECT DISTINCT closed_code FROM stg_cases WHERE closed_code IS NOT NULL);


-- ---------------------------------------------------------------------------
-- DIM_RESOLVER
-- The individual who resolved the incident. Kept separate from assignment group
-- because workload concentration is a person-level question (Q15) while SLA
-- accountability is a team-level one (Q8).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_resolver;
CREATE TABLE dim_resolver AS
SELECT 0 AS resolver_key, 'Unassigned' AS resolver_name
UNION ALL
SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY resolved_by) AS INTEGER),
    resolved_by
FROM (SELECT DISTINCT resolved_by FROM stg_cases WHERE resolved_by IS NOT NULL);


-- ---------------------------------------------------------------------------
-- DIM_VARIANT
-- One row per distinct routing path. variant_rank (1 = most frequent) is stored
-- so 'top N variants' is a filter on an indexed integer rather than a windowed
-- ranking recomputed on every visual interaction.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_variant;
CREATE TABLE dim_variant AS
WITH v AS (
    SELECT
        variant_path,
        MIN(variant_length)     AS variant_length,
        COUNT(*)                AS incident_count
    FROM stg_cases
    WHERE variant_path IS NOT NULL
    GROUP BY variant_path
)
SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY incident_count DESC, variant_path) AS INTEGER)
                                                                     AS variant_key,
    variant_path,
    CAST(variant_length AS SMALLINT)                                 AS variant_length,
    CAST(incident_count AS INTEGER)                                  AS incident_count,
    CAST(ROW_NUMBER() OVER (ORDER BY incident_count DESC, variant_path) AS INTEGER)
                                                                     AS variant_rank,
    CASE WHEN ROW_NUMBER() OVER (ORDER BY incident_count DESC, variant_path) = 1
         THEN TRUE ELSE FALSE END                                    AS is_happy_path
FROM v;


-- ===========================================================================
-- FACT_INCIDENT_CASE   grain: one row per incident
-- ===========================================================================
DROP TABLE IF EXISTS fact_incident_case;
CREATE TABLE fact_incident_case AS
SELECT
    CAST(ROW_NUMBER() OVER (ORDER BY c.incident_number) AS INTEGER) AS case_key,
    c.incident_number,

    -- Foreign keys. COALESCE to 0 routes every unmatched value to the dimension's
    -- Unknown member, guaranteeing no fact row is ever lost to a failed join.
    COALESCE(dp.priority_key,          0) AS priority_key,
    COALESCE(ds.state_key,             0) AS final_state_key,
    COALESCE(dag.assignment_group_key, 0) AS assignment_group_key,
    COALESCE(dc.category_key,          0) AS category_key,
    COALESCE(dct.contact_type_key,     0) AS contact_type_key,
    COALESCE(dl.location_key,          0) AS location_key,
    COALESCE(dcc.closed_code_key,      0) AS closed_code_key,
    COALESCE(dr.resolver_key,          0) AS resolver_key,
    COALESCE(dv.variant_key,           0) AS variant_key,
    CAST(STRFTIME(c.opened_at, '%Y%m%d') AS INTEGER) AS opened_date_key,
    CASE WHEN c.resolved_at IS NULL THEN NULL
         ELSE CAST(STRFTIME(c.resolved_at, '%Y%m%d') AS INTEGER) END AS resolved_date_key,

    -- Degenerate attributes
    c.impact,
    c.urgency,
    c.opened_at,
    c.resolved_at,

    -- Measures
    CAST(c.event_count         AS INTEGER) AS event_count,
    CAST(c.reassignment_count  AS INTEGER) AS reassignment_count,
    CAST(c.reopen_count        AS INTEGER) AS reopen_count,
    CAST(c.sys_mod_count       AS INTEGER) AS modification_count,
    c.resolution_hours,
    c.hours_in_working_states,
    c.hours_in_waiting_states,

    -- Outcome flags. Stored as 0/1 integers: VertiPaq stores and aggregates these
    -- far more cheaply than booleans or strings, and SUM() over them is the
    -- cheapest possible way to express a count of matching rows in DAX.
    CAST(CASE WHEN c.sla_met                   THEN 1 ELSE 0 END AS TINYINT) AS is_sla_met,
    CAST(CASE WHEN NOT c.sla_met               THEN 1 ELSE 0 END AS TINYINT) AS is_sla_breached,
    CAST(CASE WHEN c.was_reopened              THEN 1 ELSE 0 END AS TINYINT) AS is_reopened,
    CAST(CASE WHEN c.was_reassigned            THEN 1 ELSE 0 END AS TINYINT) AS is_reassigned,
    CAST(CASE WHEN c.is_resolved_or_closed     THEN 1 ELSE 0 END AS TINYINT) AS is_resolved,
    CAST(CASE WHEN c.has_valid_resolution_time THEN 1 ELSE 0 END AS TINYINT) AS has_valid_resolution_time,
    CAST(CASE WHEN c.knowledge                 THEN 1 ELSE 0 END AS TINYINT) AS used_knowledge_article,
    CAST(CASE WHEN c.opened_out_of_hours       THEN 1 ELSE 0 END AS TINYINT) AS opened_out_of_hours,

    CAST(c.opened_hour        AS TINYINT)  AS opened_hour,
    CAST(c.opened_day_of_week AS TINYINT)  AS opened_day_of_week
FROM stg_cases c
LEFT JOIN dim_priority         dp  ON dp.priority_label       = c.priority
LEFT JOIN dim_state            ds  ON ds.state_name           = c.final_state
LEFT JOIN dim_assignment_group dag ON dag.assignment_group_name = c.assignment_group
LEFT JOIN dim_category         dc  ON dc.category_name        = COALESCE(c.category, 'Unknown')
                                  AND dc.subcategory_name     = COALESCE(c.subcategory, 'Unknown')
LEFT JOIN dim_contact_type     dct ON dct.contact_type_name   = c.contact_type
LEFT JOIN dim_location         dl  ON dl.location_name        = c.location
LEFT JOIN dim_closed_code      dcc ON dcc.closed_code_name    = c.closed_code
LEFT JOIN dim_resolver         dr  ON dr.resolver_name        = c.resolved_by
LEFT JOIN dim_variant          dv  ON dv.variant_path         = c.variant_path;


-- ===========================================================================
-- FACT_INCIDENT_EVENT   grain: one row per event
--
-- Carries case_key so a drill-through from any case-level visual can reach the
-- full timeline of that incident with a single relationship hop.
-- ===========================================================================
DROP TABLE IF EXISTS fact_incident_event;
CREATE TABLE fact_incident_event AS
SELECT
    CAST(e.event_key AS BIGINT)            AS event_key,
    f.case_key,
    e.number                               AS incident_number,

    COALESCE(ds.state_key,  0)             AS state_key,
    COALESCE(dsn.state_key, 0)             AS next_state_key,
    f.priority_key,
    f.assignment_group_key,
    CAST(STRFTIME(e.sys_updated_at, '%Y%m%d') AS INTEGER) AS event_date_key,

    e.sys_updated_at                       AS event_timestamp,
    CAST(e.event_seq      AS INTEGER)      AS event_seq,
    CAST(e.events_in_case AS INTEGER)      AS events_in_case,

    e.dwell_minutes,
    e.dwell_hours,

    CAST(CASE WHEN e.is_first_event THEN 1 ELSE 0 END AS TINYINT) AS is_first_event,
    CAST(CASE WHEN e.is_last_event  THEN 1 ELSE 0 END AS TINYINT) AS is_last_event,
    CAST(CASE WHEN e.state_category = 'Waiting' THEN 1 ELSE 0 END AS TINYINT) AS is_waiting_event,

    CAST(EXTRACT(hour FROM e.sys_updated_at) AS TINYINT) AS event_hour
FROM stg_events e
INNER JOIN fact_incident_case f ON f.incident_number = e.number
LEFT JOIN dim_state ds  ON ds.state_name  = e.incident_state
LEFT JOIN dim_state dsn ON dsn.state_name = e.next_state;
