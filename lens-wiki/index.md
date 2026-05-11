---
name: lens-wiki index
description: Entry point and table of contents for the LENS knowledge wiki
last_updated: 2026-05-10
---

# LENS Wiki

This wiki is the source of truth for what LENS detectors check and how the RCA agent reasons about findings. See `README.md` for structure and update mechanics.

## Sections

- [`datasets/`](datasets/) — one page per dataset under surveillance. Entity grain, segment dimensions, snapshot cadence, lineage link.
- [`rules/`](rules/) — derived cross-source rules. Each page encodes one equation (e.g., `senior_debt.balance == sum(loan_pool.balance) * advance_rate`) in structured frontmatter.
- [`lineage/`](lineage/) — per-table upstream/downstream paths, with links to producing code.
- [`changes/`](changes/) — commit-level summaries on producing code paths, populated by the ingestion worker on each detected change.

## T2.5 spike decision

`tests/eval/spike_extract_rule.py` is the gate. Run with `LENS_RUN_EVAL=1` to actually call the LLM.

- **Mode:** *default = proceed with full auto-extraction*. The spike script is in place; if it returns FAIL when first run, the wiki ingestion worker (T4) is rescoped to "load hand-authored rules from this directory" and the auto-extraction is deferred to v2.1.
- **Date:** 2026-05-10 (script written; LLM run deferred until the project has API credentials configured)
- **Fallback path:** `lens-wiki/rules/senior-debt-equals-pool-x-rate.md` is hand-authored and serves as both the spike's ground truth and the v1 fallback rule. The cross-source detector (T5) reads from this directory regardless of extraction mode.

## Conventions

- Every page has YAML frontmatter; bodies are free-form markdown.
- Cross-page links use relative paths (`../rules/<slug>.md`).
- Page slugs are canonical names (lowercase, hyphenated) and stable.
- Pages are git-versioned; the `source_commit` frontmatter on derived pages records the producing-code commit they reflect.
