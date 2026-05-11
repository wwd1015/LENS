# LINEAGE.yaml — schema reference

> **Status:** v1 — used by the `/triage-data` skill. For v2 LENS Surveillance, the structured knowledge moved into the `lens-wiki/` markdown tree (`datasets/`, `rules/`, `lineage/`). Both formats coexist: `/triage-data` reads `LINEAGE.yaml`; the v2 orchestrator + `/lens-rca` skill read `lens-wiki/`. New work should target `lens-wiki/`; this doc remains the authoritative reference for the v1 skill.

`LINEAGE.yaml` declares the datasets that `/triage-data` knows about, their upstreams, and the code that produces them. The skill is project-agnostic; **all dataset-specific knowledge lives here**, not in the skill prompt.

This mirrors Deputy's `projects/<name>.yaml` separation: agents/skills are project-agnostic prompts; project knowledge is data the skill reads at runtime.

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
