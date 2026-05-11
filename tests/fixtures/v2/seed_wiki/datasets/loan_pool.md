---
name: loan_pool
description: Underlying loan-pool balance per entity (deal) per snapshot
entity_grain: entity_id
segments: []
snapshot_cadence: daily
lineage_page: ../lineage/senior_debt.lineage.md
source_commit: HEAD
last_updated: 2026-05-11
---

# Dataset: loan_pool

## Purpose
One row per (entity_id, snapshot_date) holding the aggregate loan-pool
balance for that deal on that date. Multiplied by the advance rate in
`deal_terms` to derive `senior_debt.balance`.

## Entity grain
`entity_id` — pre-aggregated to the deal grain at this layer.

## Snapshot cadence
Daily.

## Schema
- `entity_id` (str) — deal-grain identity.
- `snapshot_date` (date) — point-in-time snapshot.
- `balance` (float) — total loan-pool balance for the deal.
