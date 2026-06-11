---
name: senior-debt
description: Deal-level senior tranche balance, snapshotted monthly
entity_grain: deal_id
segments:
  - deal_id
snapshot_cadence: monthly
lineage_page: ../lineage/senior-debt.lineage.md
source_commit: HEAD
last_updated: 2026-06-10
---

# Dataset: senior-debt

## Purpose

The senior tranche balance per deal, derived from `loan_pool` ×
`deal_terms.advance_rate` by `build_senior_debt.sql`.

## Entity grain

One row per (deal_id, snapshot_date).

## Related rules

- `../rules/senior-debt-equals-pool-x-advance-rate.md`

## Related lineage

- `../lineage/senior-debt.lineage.md`
