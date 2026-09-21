# Presenting the project

## LinkedIn post

> After about 4 years building BPM workflows, I wanted to see what the process data
> itself says. So I built an end-to-end BI project on an IT incident event log:
> 24,918 incidents and 141,707 state changes.
>
> What the data showed:
> 🔁 Handoffs are the biggest SLA driver. With no reassignment, 78% of incidents meet
> SLA. With 5 or more, 14% do, and the median resolution goes from 0.7 h to 292 h.
> 🧭 Conformance predicts the outcome. The top 5 process paths carry 83% of volume at
> 70% attainment. The other 183 paths manage 29%.
> ⏳ One wait step, "Awaiting User Info", separates the fast paths from the slow ones.
> 📊 94% of volume and 91% of breaches sit in Moderate priority. That's where the SLA
> problem actually is.
>
> How it's built: Python (pandas) for event sequencing and variants, a DuckDB star
> schema with two fact grains, and a Power BI model with 34 DAX measures across seven
> pages. The Power BI project is generated as code and statically validated before it
> ever opens.
>
> I've also written down what the data *can't* support: no trend after May 2016, and
> a knowledge-article gap I treat as selection bias, not cause.
>
> Repo: https://github.com/NidhishNade/bpm-process-intelligence-sla-analytics
>
> #PowerBI #BusinessIntelligence #ProcessMining #DAX #SQL #DataAnalytics

## CV entry (Projects section)

**BPM Process Intelligence & SLA Analytics Platform** | Python, SQL (DuckDB), Power BI, DAX
- Modelled an incident-management event log (24.9K cases, 141.7K events) as a process-mining
  dataset. Derived dwell time, event sequence and 188 process variants in Python.
- Designed a star schema with case-grain and event-grain facts over 10 conformed dimensions,
  backed by 18 analytical SQL queries and 50+ automated data-quality and integrity checks.
- Built a 7-page Power BI report on 34 DAX measures, generated as a PBIP project and statically
  validated (431 checks) so broken fields surface before the report opens.
- Found that reassignments are the strongest SLA driver (attainment falls from 78% to 14%
  between 0 and 5+ handoffs), and documented the data's limits alongside the findings.

## One-line version (headline or summary)

BPM developer (about 4.3 years in workflow automation, BPMN, Java and SQL) moving into BI.
I built an end-to-end process-intelligence project in Python, SQL and Power BI.
