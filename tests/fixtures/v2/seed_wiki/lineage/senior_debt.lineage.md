---
name: senior_debt.lineage
description: Upstream and downstream lineage for senior_debt (integration test seed)
table: senior_debt
upstream:
  - table: loan_pool
    via: lens/transforms/senior_debt.sql
    relationship: aggregation
  - table: deal_terms
    via: lens/transforms/senior_debt.sql
    relationship: one-to-one
downstream: []
producing_code:
  - lens/transforms/senior_debt.sql
source_commit: HEAD
last_updated: 2026-05-11
---

# Lineage: senior_debt

## Upstream
Loan-pool balance is multiplied by the advance rate from `deal_terms`.

## Producing code
`lens/transforms/senior_debt.sql`.

## Downstream
None in this fixture wiki.
