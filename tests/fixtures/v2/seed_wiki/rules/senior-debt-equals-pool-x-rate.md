---
name: senior-debt-equals-pool-x-rate
description: Senior-debt balance equals loan-pool balance multiplied by the deal-level advance rate
tables:
  - senior_debt
  - loan_pool
  - deal_terms
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
        group_by: null
      - table: deal_terms
        field: advance_rate
        agg: null
        group_by: null
  tolerance: 0.001
  tolerance_type: relative
source_commit: HEAD
confidence: high
last_verified: 2026-05-11
---

# Rule: senior-debt-equals-pool-x-rate

## What this asserts

For every deal × snapshot pair, the recorded `senior_debt.balance` must equal
the loan-pool balance multiplied by the recorded `deal_terms.advance_rate`:

```
senior_debt.balance == SUM(loan_pool.balance per entity_id) * deal_terms.advance_rate
```

A relative tolerance of 0.1% absorbs floating-point rounding noise; anything
beyond that is a real mismatch.

## When it might break

- Advance-rate input missing or stale.
- Loan pool composition change not reflected in `senior_debt`.
- Schema migration upstream.

## Investigation hints

When this rule fires:
1. Check the lineage page for `senior_debt`.
2. Compare `deal_terms.advance_rate` to the prior snapshot — staleness is the
   most common cause.
3. Look at `loan_pool.balance` movement vs. the previous snapshot.
