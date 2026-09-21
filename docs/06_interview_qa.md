# Interview Q&A: defending the design

Answers are written in the first person, the way I'd give them. Every number comes from
[05 Insights & recommendations](05_insights_and_recommendations.md) or
[03 Analysis findings](03_analysis_findings.md).

---

## Project and framing

**Q: Summarise the project in 30 seconds.**
I took an IT incident-management event log (24,918 incidents and 141,707 state changes)
and treated it as a process-mining log rather than a flat table. Python derives dwell
time, event sequence and process variants. DuckDB holds a star schema with two fact
tables. Power BI reports on it through 34 DAX measures across seven pages. The main
result: SLA attainment is 63.4%, and the strongest single predictor of a breach is the
number of handoffs.

**Q: Why this dataset, given your background?**
I've spent about 4.3 years as a BPM developer building workflows in BPMN engines. An
incident event log has the same shape as a process engine's audit trail: a case, an
activity, a timestamp and a resource. I wanted a BI project where that process
knowledge actually changed the analysis, rather than a generic sales dashboard.

**Q: What would you say you're *not* claiming?**
- No trend after May 2016. 99% of volume falls in Mar–May 2016, and later months have
  5–94 incidents each.
- The knowledge-article gap (26.8% vs 69.5% attainment) is selection bias, not cause.
- The dataset has no cost data, so nothing is expressed in currency.

---

## Data quality

**Q: What was the most important data-quality finding?**
`closed_at` is a batch artefact. Five timestamps account for about 13,800 closures,
which looks like a housekeeping job, not agents finishing work. So cycle time runs
`opened_at` → `resolved_at`. If I'd used `closed_at`, every duration metric would have
been wrong.

**Q: `made_sla` changes mid-case. How did you handle it?**
It changes for 36.6% of incidents, so it's a live flag, not a final outcome. The case
fact takes the last observed value, which is the status when the case finished.

**Q: How did you handle incidents with no resolution time?**
1,556 incidents have no `resolved_at`. A single flag, `has_valid_resolution_time`,
excludes them from duration denominators. Counting them as zero would have made the
process look faster than it is.

---

## Modelling

**Q: Why two fact tables?**
The questions have two natural grains. SLA and cycle-time questions are per incident
(24,918 rows). Bottleneck questions are per state visit (141,707 rows). Counting
incidents at event grain overstates volume by 5.7×, so each question is answered at its
own grain, and both facts share conformed dimensions.

**Q: `dim_state` connects to both facts. How did you avoid ambiguity?**
Two active paths from `dim_state` to the case fact (directly, and via the event fact)
would be ambiguous, and Power BI won't allow it. The event-grain path is the one the
bottleneck analysis needs, so it's active. The case fact's `final_state_key`
relationship is inactive and available through `USERELATIONSHIP` if it's ever needed.
No current measure uses it.

**Q: Why DuckDB, not PostgreSQL or a cloud warehouse?**
It's a static 46 MB extract. DuckDB is a single file with full window-function support
and nothing to install, and the DDL is ANSI SQL that ports to PostgreSQL unchanged. A
cloud warehouse would add cost and setup without changing a single answer.

**Q: Why compute sequence logic in Python, not SQL or DAX?**
Dwell time and variant signatures are procedural: order the events, take the next
timestamp, concatenate the path. I compute them once, upstream, so DAX only aggregates.
Measures use 0/1 flags so conditional counts become `SUM`, and every ratio uses
`DIVIDE`.

---

## Findings

**Q: What's the headline insight?**
Handoffs. With 0 reassignments, attainment is 78.4% and the median resolution is
0.68 h. With 5+, it's 14.3% and 292 h. The gradient is monotonic across all six
buckets, and 45.6% of incidents are reassigned at least once. The first handoff alone
costs 21 points of attainment.

**Q: Critical priority meets SLA only 1.85% of the time. Isn't that a performance failure?**
Partly, but the data points at the targets. Critical and High are worked, but a target
missed 99.5% of the time no longer helps anyone prioritise. The larger point is volume:
Moderate is 94% of incidents and 91% of breaches, so fixing only Critical and High can
address at most about 7% of breaches.

**Q: What does the variant analysis show?**
The top 5 of 188 variants carry 82.9% of volume at 70.5% attainment and a 4.8 h median.
The other 183 paths manage 29.2%. Every major path that includes `Awaiting User Info`
attains 24–35%, and every major path without it attains 58–84%.

**Q: 47% of elapsed time is in "Resolved". Why show it?**
It's administrative closure lag, not work. I label it as terminal rather than drop it,
because a bottleneck chart that quietly removes its largest bar misleads the reader.

---

## Engineering

**Q: How do you know the Power BI model is correct?**
The project is generated as text (PBIP: a TMDL model and a PBIR report). A validator
then checks that every relationship, DAX reference, visual field and CSV column
resolves before the report is opened. Its first run caught 14 measures pointing at
renamed columns.

**Q: Tell me about something that went wrong.**
The validator passed while Power BI still refused to open the model. Column names with
spaces, like `Assignment Group`, must be quoted in TMDL. My generator didn't quote them,
and my validator made the same assumption, so it couldn't catch the problem. The lesson:
a validator that shares the generator's assumptions only proves internal consistency.
I added a check that encodes the rule Power BI itself applies. Separately, I learned
that Power BI can reject a report definition without any error message: a wrong format
version gave a loaded model with no pages at all.

**Q: What would you do next with more time or data?**
- Get cost per incident, so the handoff finding can be expressed as money.
- Run a controlled trial of knowledge-article use, instead of inferring from
  observational data.
- Schedule a refresh if the source became live. It's static today, so I didn't build
  one.
