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

## Rule-extraction spike

`tests/eval/spike_extract_rule.py` is the gate that decides whether the wiki ingestion worker ships with full LLM-driven auto-extraction or scopes down to a loader for hand-authored rules. Run with `LENS_RUN_EVAL=1` to actually call the LLM.

- **Default:** proceed with full auto-extraction. The spike script is in place; if it returns FAIL when first run, the ingestion worker (`lens.wiki.ingest.IngestionWorker`) is rescoped to use `load_hand_authored(...)` instead and auto-extraction is deferred.
- **Status:** spike script written; LLM run deferred until the project is exercised end-to-end with Claude Code on real production code.
- **Fallback path:** `lens-wiki/rules/senior-debt-equals-pool-x-rate.md` is hand-authored and serves as both the spike's ground truth and the canonical fallback rule. `CrossSourceWikiCheck` reads from this directory regardless of which extraction mode is active.

## Conventions

- Every page has YAML frontmatter; bodies are free-form markdown.
- Cross-page links use relative paths (`../rules/<slug>.md`).
- Page slugs are canonical names (lowercase, hyphenated) and stable.
- Pages are git-versioned; the `source_commit` frontmatter on derived pages records the producing-code commit they reflect.
