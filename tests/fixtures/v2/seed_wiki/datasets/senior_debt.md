---
name: senior_debt
description: Senior-tranche debt balance per entity (deal) per snapshot
entity_grain: entity_id
segments: []
snapshot_cadence: daily
lineage_page: ../lineage/senior_debt.lineage.md
source_commit: HEAD
last_updated: 2026-05-11
---

# Dataset: senior_debt

## Purpose
One row per (entity_id, snapshot_date) capturing the senior-tranche balance
derived from the underlying loan pool times the deal-level advance rate.

## Entity grain
`entity_id`.

## Snapshot cadence
Daily.

## Schema
- `entity_id` (str) — deal-grain identity.
- `snapshot_date` (date) — point-in-time snapshot of the balance.
- `balance` (float) — senior-tranche outstanding balance.
