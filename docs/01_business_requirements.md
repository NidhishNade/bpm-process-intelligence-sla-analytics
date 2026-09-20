# Step 1 — Business Requirements & Business Questions

## 1.1 Business context

An enterprise IT Service Management (ITSM) function runs an **Incident Management**
process on a ServiceNow-style platform. Every incident is a *case*; every change to
that incident writes an *event* row. The support organisation is measured on whether
incidents are resolved inside their agreed **SLA**.

Leadership has visibility of *outcomes* (how many incidents breached SLA) but no
visibility of *process behaviour* — where time is lost, which routing paths are
expensive, and which teams generate rework. The question they cannot currently
answer is: **"we know our SLA miss rate, but we do not know what is causing it."**

## 1.2 Why this is a Process Intelligence problem, not a reporting problem

A conventional ITSM report aggregates the *current state* of each incident. That
throws away the thing that actually explains performance: the **sequence of states
the incident passed through, and how long it sat in each one**.

This project treats the extract as an **event log** in the process-mining sense:

| Process-mining concept | Column in this dataset |
|---|---|
| Case ID | `number` (INC…) |
| Activity | `incident_state` |
| Timestamp | `sys_updated_at` |
| Resource | `assigned_to`, `assignment_group` |
| Case attributes | `priority`, `impact`, `urgency`, `category`, `contact_type`, `location` |
| Outcome | `made_sla`, `closed_code`, `reopen_count` |

This is the same mental model as a BPMN process with a token moving through tasks —
each `incident_state` is a task the token occupies, and the gap between consecutive
`sys_updated_at` values is the time the token spent there.

## 1.3 Stakeholders and what each one needs

| Stakeholder | Decision they need to make | What the BI solution must give them |
|---|---|---|
| IT Service Delivery Head | Where to invest to lift SLA attainment | Executive KPI overview, SLA trend, breach concentration |
| Process / Operations Manager | Which process step to re-engineer | Bottleneck analysis: mean/median dwell time per state |
| Support Team Leads | Team-level workload and quality | Volume, reassignment rate and reopen rate per assignment group |
| Continuous Improvement / BPM | Which routing paths are non-standard and costly | Process-variant analysis, happy path vs exception paths |
| Service Desk Analyst | Investigate a specific incident | Incident-level drill-through with full event timeline |

## 1.4 Scope

**In scope**
- Descriptive and diagnostic process analytics on the 2016-02-29 → 2017-02-18 extract
- SLA attainment, cycle time, bottlenecks, variants, reassignment and reopen behaviour
- A dimensional model and a Power BI semantic layer with documented DAX

**Explicitly out of scope, and why**
- *Predictive SLA-breach modelling.* The brief is BI, not data science. An ML model
  here would add credibility risk without changing any decision the stakeholders above
  are making. Named as a future enhancement instead.
- *Real-time / streaming.* The source is a static historical extract; a refresh
  cadence would be invented, not real.
- *Cloud warehouse.* A single 46 MB file does not justify it. Called out in the README
  as the change that *would* be made at production volume.

## 1.5 Business questions

These are the questions the report must answer. Each maps to a later step, and each
is answerable from columns proven to exist in Step 2 profiling.

### A. Executive / outcome
1. What is the overall SLA attainment rate, and how many incidents breached?
2. How does SLA attainment trend month over month across the 12-month window?
3. What is the total incident volume, and the median time to resolve?
4. Which priority bands carry the SLA breaches — are breaches concentrated in
   Critical/High, or hidden in the Moderate bulk?

### B. Bottleneck / process performance
5. Which `incident_state` consumes the most elapsed time per incident on average?
6. How much total time is lost in waiting states (`Awaiting User Info`,
   `Awaiting Vendor`, `Awaiting Problem`, `Awaiting Evidence`) versus active work?
7. Is the bottleneck the same for incidents that made SLA and those that breached?

### C. SLA / operational
8. What is the SLA attainment rate by assignment group, and which groups are outliers
   once volume is controlled for?
9. Does the intake channel (`contact_type`: Phone, Email, Self service, IVR, Direct
   opening) affect resolution time or SLA attainment?
10. Does `knowledge` (a knowledge-base article was attached) correlate with better
    resolution time?

### D. Process variants
11. How many distinct state sequences (variants) exist, and what share of incidents
    follow the single most common path?
12. Do incidents on non-standard variants have materially worse SLA attainment?

### E. Reassignment & workload
13. What is the distribution of `reassignment_count`, and does each additional
    reassignment measurably degrade SLA attainment?
14. Which assignment groups most often hand work off, and which receive it?
15. How is workload distributed across resolvers — is there a long tail of
    under-utilised resolvers, or concentration risk on a few?

### F. Time-based
16. How does incident arrival vary by hour of day and day of week?
17. Are incidents opened outside business hours resolved more slowly?

### G. Rework / quality
18. How many incidents are reopened, and what is the SLA and cycle-time penalty?
19. Which `closed_code` values are associated with reopening?

## 1.6 Success criteria

The project is complete when:
- Every question above is answered by a visual or a documented SQL query
- Every KPI has a written definition including its denominator and exclusions
- Data-quality decisions are documented with the evidence that motivated them
- The model refreshes end to end from `data/raw` with one command
