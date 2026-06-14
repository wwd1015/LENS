---
name: senior-debt.lineage
description: Upstream and downstream lineage for senior_debt
table: senior_debt
repo_url: https://github.com/northwind-capital/lending-data-pipeline
upstream:
  - table: loan_pool
    via: models/senior_debt.sql
    relationship: aggregation
  - table: deal_terms
    via: models/senior_debt.sql
    relationship: one-to-one
downstream: []
producing_code:
  - models/senior_debt.sql
recent_changes:
  - commit: 7f3c1a9e4b2d8c016a5f90e3d2147b8c6e0a9f12
    date: 2026-06-08
    message: "TICKET-4821 Q2 true-up: override Sterling MMF II advance rate to 0.84"
  - commit: 1b9d44e0aa72c3f5e681907c4d2b5a3f8e6c0d11
    date: 2026-05-12
    message: "Add deal_terms join so advance rate is sourced per deal"
source_commit: 7f3c1a9e4b2d8c016a5f90e3d2147b8c6e0a9f12
last_updated: 2026-06-08
---

# Lineage: senior-debt

## Producing repo

`senior_debt` is built by the Northwind Capital data pipeline
(`github.com/northwind-capital/lending-data-pipeline`), model
`models/senior_debt.sql`. LENS itself does not produce this table — it only
watches it.

## Upstream

`senior_debt` is computed from `loan_pool` (balances aggregated per deal) and
`deal_terms` (the deal-level advance rate), joined on deal and snapshot date.

## Recent changes

The most recent change (commit `7f3c1a9e`, 2026-06-08) added a temporary Q2
true-up that hard-codes a 0.84 advance rate for one deal on the 2026-06-30
snapshot — the override that breaks the reconciliation rule.

## Downstream

Nothing consumes `senior_debt` in this demo.
