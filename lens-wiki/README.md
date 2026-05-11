# lens-wiki

A curated tree of markdown that LENS agents navigate natively (via `Read` + `grep`) — in the spirit of Karpathy's `llm.wiki` / `llm.txt` proposal. Not a database. Not a vector store. Just files.

## Structure

```
lens-wiki/
  index.md              # entry point — table of contents
  datasets/             # one file per dataset
  rules/                # one file per derived cross-source rule
  lineage/              # upstream/downstream paths
  changes/              # commit-level summaries on producing code paths
```

## Page schema

Every page uses YAML frontmatter + markdown body. See `_template.md` in each subdirectory for the exact schema. Common fields:

- `name`, `description` — required on every page
- `source_commit` — the git SHA the page reflects (so consumers can detect drift)
- `last_updated` — ISO 8601 timestamp

## How it's updated

Two modes, configured by the T2.5 spike-gate decision:

1. **Auto-extracted** (if rule-extraction quality passes T2.5 threshold). A code-change hook / poller invokes `lens.wiki.ingest.IngestionWorker`, which calls an LLM to regenerate just the affected pages. Updates committed back to this directory.
2. **Hand-authored** (fallback if extraction quality is below threshold). Pages are written by humans; `lens.wiki.ingest.load_hand_authored` validates them against the schema.

The decision is documented in `index.md` once T2.5 runs.

## How it's consumed

- Detectors read it via `lens.wiki.cache.WikiCache` (loaded once per orchestrator run).
- The RCA agent reads it the same way.
- The HTML brief embeds rule and lineage links so analysts can click through to the source.

If `Read` + `grep` ever stops scaling, layer a structured index on top of the same markdown — never replace it.
