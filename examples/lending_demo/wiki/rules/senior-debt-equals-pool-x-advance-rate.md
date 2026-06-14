---
name: senior-debt-equals-pool-x-advance-rate
description: Senior debt balance must equal sum of loan-pool balances multiplied by the deal-level advance rate
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
        group_by: deal_id
      - table: deal_terms
        field: advance_rate
        agg: null
        group_by: null
  tolerance: 0.001
  tolerance_type: relative
source_commit: HEAD
confidence: high
last_verified: 2026-06-10
---

# Rule: senior-debt-equals-pool-x-advance-rate

## What this asserts

For every deal, the senior-debt balance on a snapshot date must equal the sum
of all loan balances in the deal's pool, multiplied by the deal's advance rate:

```
senior_debt.balance == SUM(loan_pool.balance GROUP BY deal_id) * deal_terms.advance_rate
```

A relative tolerance of 0.1% absorbs rounding; anything beyond is a real mismatch.

## Where it lives in production

Built by the Northwind Capital data pipeline, model `models/senior_debt.sql`
— see `../lineage/senior-debt.lineage.md` for the producing repo and its
recent changes.

## When it might break

- **Advance-rate input missing or stale** — deal_terms refreshed late.
- **Loan pool composition change** the senior-debt computation hadn't re-run for.
- **Schema migration upstream** — a column rename in loan_pool.
