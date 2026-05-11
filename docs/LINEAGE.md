# LINEAGE.yaml — schema reference

## Scope

`LINEAGE.yaml` declares dataset knowledge for the **`/triage-data` Claude Code skill** — TabPFN-TS-based anomaly detection plus a single LLM reasoning trace that walks lineage and git history. It is read only by that skill.

The **`DetectionOrchestrator` / `/lens-rca`** workflow does not read this file. It reads `lens-wiki/` (markdown pages) instead — different format, different consumer, different data model.

### Which should I use?

| | `LINEAGE.yaml` + `/triage-data` | `lens-wiki/` + orchestrator + `/lens-rca` |
|---|---|---|
| **Best for** | A quick spot-check of one dataset; "is this partition anomalous and why?" answered in one LLM trace | Scheduled / batch surveillance; multi-detector dedup; an HTML morning brief; ad-hoc per-finding RCA |
| **Detection** | TabPFN-TS only | TabPFN-TS + STL + cross-source rule equations + pluggable detector pool |
| **Cross-source rules** | Not modeled (this file describes lineage and producing code, not equations) | Yes — `lens-wiki/rules/*.md` with structured `equation` frontmatter |
| **Output** | `ROOT_CAUSE.md` written in-place | `findings.{run_id}.json` + `LENS_brief.html` + optional `feedback.jsonl` |
| **Knowledge source** | One YAML file at repo root | A tree of markdown pages under `lens-wiki/` |

Both formats coexist — they answer different questions. Pick by workflow, not by recency.

## Location

`LINEAGE.yaml` at the repo root.

## Schema

```yaml
# Required: column conventions used by every dataset unless overridden per-entry
defaults:
  entity_col: loan_id          # default for every dataset
  snapshot_col: as_of_date
  context_window: 90           # TabPFN-TS history length
  score_threshold: 3.0         # |z-score| flag threshold
  min_history: 20

# Required: one entry per dataset that /triage-data can monitor
datasets:

  loans_daily:
    # Where to read the dataset (PolarsSource path or Snowflake table)
    source:
      kind: parquet            # parquet | csv | snowflake
      path: data/loans_daily.parquet

    # Optional per-dataset overrides of defaults
    entity_col: loan_id
    snapshot_col: as_of_date

    # Required: numeric fields TabPFN-TS should monitor
    monitored_fields:
      - balance
      - cumulative_payments
      - days_past_due

    # Required: producing code path (single path or list). Used for git archaeology.
    producing_code:
      - src/etl/loans_daily.sql
      - src/etl/loans_daily_post.py

    # Required: upstream tables, in dependency order (closest first).
    # Each upstream entry references another `datasets:` key so the skill
    # can recursively run TabPFN-TS on the same window.
    upstream:
      - dataset: loan_master
        relationship: 1:1 by loan_id
      - dataset: payments_daily
        relationship: many-to-1 aggregated to loan_id

    # Optional: free-text owner / runbook pointer the skill includes
    # in ROOT_CAUSE.md
    owner: lending-data-platform
    runbook: docs/runbooks/loans_daily.md

  loan_master:
    source:
      kind: snowflake
      table: PROD.LENDING.LOAN_MASTER
    monitored_fields:
      - origination_balance
      - interest_rate
    producing_code:
      - src/etl/loan_master.sql
    upstream: []                # leaf — no upstream we can walk into

  payments_daily:
    source:
      kind: parquet
      path: data/payments_daily.parquet
    monitored_fields:
      - payment_amount
    producing_code:
      - src/etl/payments_daily.sql
    upstream:
      - dataset: payments_raw
        relationship: 1:1 by payment_id

  payments_raw:
    source:
      kind: snowflake
      table: PROD.PAYMENTS.RAW_EVENTS
    monitored_fields:
      - amount
    producing_code: []          # external — we don't own the producer
    upstream: []
    owner: payments-platform
```

## Field reference

| Key | Required | Notes |
|---|---|---|
| `defaults.entity_col` | yes | Default entity column. Override per-dataset if needed. |
| `defaults.snapshot_col` | yes | Default snapshot column. |
| `defaults.context_window` | yes | History rows per entity passed to TabPFN-TS. |
| `defaults.score_threshold` | yes | |z-score| flag threshold. |
| `defaults.min_history` | yes | Skip entities with fewer prior snapshots than this. |
| `datasets.<name>.source.kind` | yes | `parquet` / `csv` / `snowflake`. |
| `datasets.<name>.source.path` | conditional | Required for `parquet`/`csv`. |
| `datasets.<name>.source.table` | conditional | Required for `snowflake`. |
| `datasets.<name>.entity_col` | no | Override `defaults.entity_col`. |
| `datasets.<name>.snapshot_col` | no | Override `defaults.snapshot_col`. |
| `datasets.<name>.monitored_fields` | yes | List of numeric columns. |
| `datasets.<name>.producing_code` | yes (may be empty list) | Repo paths for git archaeology. Empty list = external producer; skill notes this and skips code archaeology. |
| `datasets.<name>.upstream` | yes (may be empty list) | Each entry: `dataset:` (must exist as another key) and `relationship:` (free text). |
| `datasets.<name>.owner` | no | Free-text. Included in `ROOT_CAUSE.md`. |
| `datasets.<name>.runbook` | no | Free-text path. Included in `ROOT_CAUSE.md`. |

## Validation rules

`/triage-data` reads `LINEAGE.yaml` and stops with a clear error if:

- The requested dataset isn't in `datasets:`.
- An `upstream.dataset:` reference points to a key that isn't defined.
- `monitored_fields` is missing or empty.
- `source.kind` isn't recognized.

These checks are intentionally strict: the skill should never invent lineage when the YAML is wrong.

## Adding a new dataset

1. Add a `datasets.<name>:` entry following the schema above.
2. List every upstream — even if the relationship is "external, can't walk further" (`producing_code: []`, `upstream: []`). The skill needs the explicit terminator.
3. Run `/triage-data <name>` against a known-clean window first, to confirm no false positives, before relying on it.
