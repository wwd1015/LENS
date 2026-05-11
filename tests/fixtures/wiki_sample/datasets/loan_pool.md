---
name: loan_pool
description: Underlying loan-level balances for each deal
entity_grain: loan_id
segments:
  - deal_id
  - origination_quarter
snapshot_cadence: daily
lineage_page: ../lineage/loan_pool.lineage.md
source_commit: HEAD
last_updated: 2026-05-10
---

# Dataset: loan_pool

## Purpose
One row per loan per snapshot date. Used as the base for aggregating to the
deal level when computing senior-debt balance.

## Entity grain
`loan_id` — every loan in every deal under surveillance.

## Snapshot cadence
Daily.
