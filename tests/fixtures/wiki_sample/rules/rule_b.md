---
name: rule_b
description: Mezzanine balance equals deal total minus senior debt
tables:
  - mezz_debt
  - deal_totals
fields:
  - mezz_debt.balance
equation:
  lhs:
    table: mezz_debt
    field: balance
    agg: null
    group_by: null
  rhs:
    op: sub
    args:
      - table: deal_totals
        field: total_balance
        agg: null
        group_by: null
      - table: senior_debt
        field: balance
        agg: null
        group_by: null
  tolerance: 0.01
  tolerance_type: absolute
source_commit: HEAD
confidence: medium
last_verified: 2026-05-10
---

# Rule: rule_b

## What this asserts
`mezz_debt.balance == deal_totals.total_balance - senior_debt.balance`

## When it might break
Deal-totals refresh lags behind senior-debt refresh.
