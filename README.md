# LENS: Longitudinal & Entity-level Normative Surveillance

A data-quality surveillance tool for commercial lending data, built on Polars.

LENS layers above whatever rule-based DQ your production pipelines already enforce. It catches the harder class of issues those rules miss:

- **Longitudinal anomalies** — values in-range on a snapshot but the trajectory over an entity's history is wrong. STL-residual and TabPFN-TS detectors.
- **Cross-source mismatches** — derived from lineage + production-code logic. E.g. *senior debt balance = sum(loan-pool balances) × advance rate* — that relationship lives in production SQL, not in any DQ rulebook. The `cross_source_wiki` detector reads structured equations from `lens-wiki/rules/`.

Every flagged finding gets a **severity** + **confidence**, plus a structured **root-cause hypothesis** (lineage walked, recent commits inspected, contrast rows sampled). Output is an interactive HTML brief, a Slack-pasteable markdown digest, or an ad-hoc per-finding investigation via the `/lens-rca` Claude Code skill.

## Install

```bash
pip install -e ".[dev]"          # development install
pip install -e ".[snowflake]"    # with Snowflake DataSource
pip install -e ".[tabpfn]"       # with TabPFN-TS zero-shot anomaly detection
```

## Quick start — inline rule-based suite

For pipelines that just need a few DQ checks in-line:

```python
import polars as pl
from lens.engine import Suite
from lens.io import PolarsSource

source = PolarsSource(path="loans.parquet",
                      entity_col="loan_id", snapshot_col="as_of_date")

suite = Suite(entity_col="loan_id", snapshot_col="as_of_date")
suite.add("null_check", fields=["balance", "status"])
suite.add("stale_data", field="balance", max_unchanged=30)
suite.add("monotonicity", field="cumulative_payments", direction="increasing")
suite.add("volatility", field="balance", max_pct_change=0.5)

result = suite.run(source)
print(result.summary)
```

Suites can also be defined in YAML — see `lens.config.load_suite`.

## Surveillance pipeline

The full pipeline: `lens-wiki/` → `DetectionOrchestrator` → `RCAAgent` → HTML / markdown brief.

```python
from pathlib import Path
from lens.orchestrator import DetectionOrchestrator
from lens.rca.agent import RCAAgent
from lens.brief import render_brief

# 1. Configure the orchestrator with pluggable detectors.
orch = (
    DetectionOrchestrator(entity_col="loan_id", snapshot_col="as_of_date")
    .add_single("stl_residual", field="balance", period=30, z_threshold=3.0)
    .add_single("tabpfn_anomaly", field="balance")   # optional [tabpfn] extra
    .add_cross("cross_source_wiki")                  # reads rules from lens-wiki/
)

# 2. Run detection. Findings are scored, deduped, and written to disk.
findings = orch.run(
    sources={"loan_pool": loan_pool_lf, "senior_debt": senior_debt_lf},
    wiki_root=Path("lens-wiki"),
    output_dir=Path("out"),
)

# 3. RCA each finding above a severity gate.
rca = RCAAgent(repo_root=Path("."))
rcas = {f.finding_id: rca.investigate(f, wiki, sources) for f in findings
        if f.issue.severity.value in {"error", "critical"}}

# 4. Render the morning brief.
render_brief(findings, rcas, Path("out/LENS_brief.html"),
             dataset_label="Q2 lending",
             prior_findings_path=Path("out/findings.latest.json"))
```

### Ad-hoc investigation

`/lens-rca` is a Claude Code slash command for investigating a single flag or a fresh question, off the morning cadence:

```bash
/lens-rca --finding-id <id>
/lens-rca --investigate-entity DEAL-42 --field balance --date 2026-05-01
```

### Other delivery channels

- **Slack-pasteable digest:** `python -m lens.brief.markdown out/findings.latest.json` prints a top-5 markdown summary.
- **Feedback capture:** `python -m lens.brief.feedback <finding_id> real|false_positive|needs_more` appends to `feedback.jsonl` for future severity calibration.

## The `lens-wiki/` convention

Cross-source rules and dataset metadata live as markdown pages at the repo root:

```
lens-wiki/
├── index.md          # human-curated TOC
├── datasets/         # one page per table — grain, segments, lineage pointer
├── rules/            # one page per cross-source equation (structured frontmatter)
├── lineage/          # upstream/downstream paths
└── changes/          # changelog entries
```

Each rule page declares its equation as **structured frontmatter** — no string-eval:

```yaml
equation:
  lhs: {table: senior_debt, field: balance, agg: null}
  rhs:
    op: mul
    args:
      - {table: loan_pool, field: balance, agg: sum, group_by: deal_id}
      - {table: deal_terms, field: advance_rate, agg: null}
  tolerance: 0.001
  tolerance_type: relative
```

`CrossSourceWikiCheck` reads these pages and evaluates the structured spec via Polars expressions. Pages can be **hand-authored** by analysts or **auto-extracted** from production code via `lens.wiki.ingest.IngestionWorker` (which shells out to Claude Code in headless mode — see below).

## LLM access

All runtime LLM calls go through **Claude Code headless mode** — `ClaudeCodeClient` shells out to `claude -p "<prompt>" --output-format text`. The `anthropic` Python SDK is **not** used; LENS environments authenticate via Claude Code SSO. Tests inject deterministic stubs through the `LLMClient` Protocol and never touch the network.

## Tests

```bash
python3 -m pytest tests/ -v                  # full suite (default markers)
python3 -m pytest tests/ -q -m "not eval"    # CI run — skip real-LLM evals (default)
LENS_RUN_EVAL=1 pytest -m eval               # real-LLM evals: rule extraction, TS ensemble
```

The `eval` marker is registered in `pyproject.toml`. Tests marked `@pytest.mark.eval` either need the real LLM (skip without `LENS_RUN_EVAL=1`) or run computational comparisons that are slow enough to defer.

## Roadmap — not yet implemented

These were considered for the initial build and explicitly deferred. The wiki schema reserves space for them so the data model doesn't have to change later.

- **Top-down hierarchical drill-down** — start at the portfolio aggregate, recursively narrow into segments (deal type, origination quarter, etc.) when the aggregate looks anomalous, surface only the leaf finding plus the path. Today, detectors run entity-by-entity in parallel and the orchestrator emits one Finding per `(entity_id, field_name, snapshot_date)`; nothing aggregates upward or drills downward. `DatasetPage.segments` in the wiki captures the dimensions a drill-down detector *would* search, but no detector reads them yet.
- **Severity calibration loop** — `feedback.jsonl` records analyst `[real | false_positive | needs_more]` labels, but nothing reads it back into `scoring.py` thresholds. Calibration is a follow-up job, not part of the surveillance pipeline today.
- **LLM-judged ensemble for the TS detector pool** — the orchestrator runs detectors independently and dedupes overlaps; it does not score-combine TS detectors. `tests/eval/test_ts_ensemble.py` exercises a vote-based ensemble outside the orchestrator to validate the design, but the runtime ships without it.
- **Auto-extraction of rule pages** — `IngestionWorker` exists and the rule-extraction spike script is in place, but until the `LENS_RUN_EVAL=1` gate is run against your real production code, `lens-wiki/rules/*.md` should be treated as hand-authored.

## Architecture

See `CLAUDE.md` for the full architecture, module layout, key patterns, and conventions for adding new detectors.
