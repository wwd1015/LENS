---
name: rule_a
description: Senior debt balance equals sum of loan-pool balance times advance rate
tables:
  - senior_debt
  - loan_pool
fields:
  - senior_debt.balance
equation:
  lhs:
    table: senior_debt
    field: balance
    agg: null
    group_by: null
  rhs:
    op: mul
    args:
      - table: loan_pool
        field: balance
        agg: sum
        group_by: deal_id
      - table: deal_terms
        field: advance_rate
        agg: null
        group_by: null
  tolerance: 0.001
  tolerance_type: relative
source_commit: HEAD
confidence: high
last_verified: 2026-05-10
---

# Rule: rule_a

## What this asserts
`senior_debt.balance == SUM(loan_pool.balance GROUP BY deal_id) * deal_terms.advance_rate`

## When it might break
Stale advance-rate input or schema drift in loan_pool.
