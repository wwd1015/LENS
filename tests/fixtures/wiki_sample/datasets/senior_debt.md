---
name: senior_debt
description: Senior-tranche debt balance per deal per snapshot
entity_grain: deal_id
segments:
  - deal_type
snapshot_cadence: daily
lineage_page: ../lineage/senior_debt.lineage.md
source_commit: HEAD
last_updated: 2026-05-10
---

# Dataset: senior_debt

## Purpose
One row per deal per snapshot capturing the senior-tranche balance derived
from the underlying loan pool times the deal-level advance rate.

## Entity grain
`deal_id`.

## Snapshot cadence
Daily.
