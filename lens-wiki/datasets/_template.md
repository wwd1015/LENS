---
name: <dataset-slug>
description: <one-line description of what this dataset represents>
entity_grain: <e.g., loan_id, deal_id>
segments:
  - <segment-dimension-1>
  - <segment-dimension-2>
snapshot_cadence: <daily | monthly | quarterly>
lineage_page: ../lineage/<dataset-slug>.lineage.md
source_commit: <git-sha-of-producing-code>
last_updated: <YYYY-MM-DD>
---

# Dataset: <dataset-slug>

## Purpose
<What this dataset captures, who produces it, who consumes it.>

## Entity grain
<The unique key — `loan_id` or `deal_id`, etc. — and what "one row" means.>

## Segments
<Dimensions analysts cut on (deal type, origination quarter, etc.). The `hierarchical_drill_down` detector takes a `segments=[...]` list at construction; you can mirror those columns here so the wiki page documents the same drill-down dimensions used at detection time.>

## Snapshot cadence
<How often new partitions land.>

## Related rules
<Links to `../rules/*.md` that involve this dataset.>

## Related lineage
<Link to the lineage page.>
