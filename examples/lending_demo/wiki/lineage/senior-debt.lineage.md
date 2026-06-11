---
name: senior-debt.lineage
description: Upstream and downstream lineage for senior_debt
table: senior_debt
upstream:
  - table: loan_pool
    via: examples/lending_demo/transforms/build_senior_debt.sql
    relationship: aggregation
  - table: deal_terms
    via: examples/lending_demo/transforms/build_senior_debt.sql
    relationship: one-to-one
downstream: []
producing_code:
  - examples/lending_demo/transforms/build_senior_debt.sql
source_commit: HEAD
last_updated: 2026-06-10
---

# Lineage: senior-debt

## Upstream

`senior_debt` is computed from `loan_pool` (balances aggregated per deal) and
`deal_terms` (the deal-level advance rate), joined on deal and snapshot date.

## Downstream

Nothing consumes `senior_debt` in this demo.

## Producing code

`examples/lending_demo/transforms/build_senior_debt.sql` — the RCA agent walks
`git log` on this path when hunting change-correlated anomalies.
