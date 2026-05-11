---
name: seed_wiki_v2_index
description: Self-contained seed wiki used by the LENS v2 integration test
last_updated: 2026-05-11
---

# Seed wiki — integration test fixture

This wiki is consumed by `tests/test_integration_v2.py` to drive the
`DetectionOrchestrator` end-to-end against `tests/fixtures/v2/synthetic_data/`.

It deliberately mirrors the production `lens-wiki/` shape — `datasets/`,
`rules/`, `lineage/` — but is sized down to one rule and three tables so the
integration test exercises the full pipeline without dragging in production
metadata.

## Contents

- `datasets/loan_pool.md` — loan-level balances per (entity, snapshot).
- `datasets/senior_debt.md` — senior-tranche balance per (entity, snapshot).
- `rules/senior-debt-equals-pool-x-rate.md` — the equation rule the
  cross-source detector evaluates.
- `lineage/senior_debt.lineage.md` — minimal upstream/downstream walk.

Entity grain throughout: `entity_id` (each `entity_id` is one deal).
Snapshot cadence: daily.
