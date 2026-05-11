---
name: senior-debt-equals-pool-x-rate
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
last_verified: 2026-05-10
---

# Rule: senior-debt-equals-pool-x-rate

## What this asserts

For every structured-finance deal, the senior-debt tranche's balance on a given snapshot date must equal the sum of all loan balances in the underlying loan pool for that deal, multiplied by the deal's advance rate.

Stated as an equation:

```
senior_debt.balance == SUM(loan_pool.balance GROUP BY deal_id) * deal_terms.advance_rate
```

A relative tolerance of 0.1% is allowed to absorb floating-point and rounding artifacts; anything beyond that is a real mismatch.

## Where it lives in production

This relationship is implemented in the production transformation SQL (path TBD — to be filled by the lineage page). See `../lineage/senior_debt.lineage.md` for the producing-code path.

## When it might break

- **Advance-rate input missing or stale.** The deal_terms table is sometimes refreshed late; if a snapshot lands before deal_terms updates, the multiplier is wrong.
- **Loan pool composition change.** A loan paid off or charged off mid-month, but the senior-debt computation hadn't re-run.
- **Schema migration upstream.** A column rename in loan_pool that the transformation hadn't picked up.

## Investigation hints

When this rule fires:
1. Check `git log --follow lens/transforms/senior_debt.sql` for recent commits.
2. Compare `deal_terms.last_updated` to the snapshot date — staleness is the most common cause.
3. Look at `loan_pool` row count for the deal vs. previous snapshot — composition change is the second most common.
