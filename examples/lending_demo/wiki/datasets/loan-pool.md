---
name: loan-pool
description: Loan-level balances and status per deal, snapshotted monthly
entity_grain: deal_id
segments:
  - deal_id
snapshot_cadence: monthly
lineage_page: ../lineage/senior-debt.lineage.md
source_commit: HEAD
last_updated: 2026-06-10
---

# Dataset: loan-pool

## Purpose

Synthetic loan-level data for the demo: one row per (deal, loan, snapshot)
with an amortizing balance and a servicing status.

## Entity grain

Detection runs at `deal_id` grain — the column shared with `senior_debt` and
`deal_terms` so cross-source findings dedupe onto the same entity.

## Related rules

- `../rules/senior-debt-equals-pool-x-advance-rate.md`
