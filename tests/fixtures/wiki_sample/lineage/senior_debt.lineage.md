---
name: senior_debt.lineage
description: Upstream and downstream lineage for senior_debt
table: senior_debt
upstream:
  - table: loan_pool
    via: lens/transforms/senior_debt.sql
    relationship: aggregation
  - table: deal_terms
    via: lens/transforms/senior_debt.sql
    relationship: one-to-one
downstream:
  - table: deal_totals
    via: lens/transforms/deal_totals.sql
producing_code:
  - lens/transforms/senior_debt.sql
source_commit: HEAD
last_updated: 2026-05-10
---

# Lineage: senior_debt

## Upstream
Loan-pool balances are aggregated to the deal grain, then multiplied by
the advance rate from `deal_terms`.

## Downstream
Feeds into `deal_totals` and downstream investor reporting.

## Producing code
`lens/transforms/senior_debt.sql`.
