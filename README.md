# LENS: Longitudinal & Entity-level Normative Surveillance

A data-quality surveillance tool for commercial lending data, built on Polars.

LENS layers above whatever rule-based DQ your production pipelines already enforce. It catches the harder class of issues those rules miss:

- **Longitudinal anomalies** — values in-range on a snapshot but the trajectory over an entity's history is wrong. STL-residual and TabPFN-TS detectors.
- **Cross-source mismatches** — derived from lineage + production-code logic. E.g. *senior debt balance = sum(loan-pool balances) × advance rate* — that relationship lives in production SQL, not in any DQ rulebook. The `cross_source_wiki` detector reads structured equations from `lens-wiki/rules/`.
- **Top-down hierarchical drill-down** — start at the portfolio aggregate, narrow into the segment that's actually driving the anomaly. The `hierarchical_drill_down` detector computes z-scores at every segment depth (portfolio → asset class → vintage → …) and emits only the deepest still-anomalous path, so 800 entity-level flags become one finding pinned to *asset_class=commercial > vintage=2024Q3*.

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

Suites can also be defined in YAML. A worked example lives at [`examples/suite.yaml`](examples/suite.yaml); load it with `lens.config.load_suite(path)`.

```python
from lens.config import load_suite
suite = load_suite("examples/suite.yaml")
result = suite.run(source)
```

## How rules are defined

LENS detects on **structured rules**, not natural-language descriptions. An LLM never reads "balance must not drop more than 50% week-over-week" at detection time and translates it on the fly. The LLM appears at exactly two places: (a) one-time, to translate production SQL into structured cross-source equations; (b) per-finding, to write a root-cause hypothesis for analyst review.

| Rule surface | Format | Who writes it | When the LLM is involved |
|---|---|---|---|
| Inline DQ checks (`null_check`, `range_check`, `stale_data`, `monotonicity`, `volatility`, `stl_residual`, `tabpfn_anomaly`, `hierarchical_drill_down`) | Python (`suite.add("name", **params)`) or YAML (`examples/suite.yaml`) | Engineer or analyst | Never |
| Cross-source equations | Structured YAML frontmatter in `lens-wiki/rules/*.md` (the `equation` block — see below) | Analyst by hand, or `lens.wiki.ingest.IngestionWorker` extracting from production SQL/Python | At ingestion time only (one-shot per code change) |
| RCA hypothesis text | Free-form prose | LLM at runtime, per finding above the severity gate | Per finding, post-detection |

If you want a brand-new kind of rule that doesn't fit any of these — say, "balance and pre-payment must move together with rolling correlation > 0.6" — write it as a new `BaseCheck` subclass and register it with `@registry.register`. See `## Adding a New Check` in `CLAUDE.md`. Adding rules is a code change, by design — detection logic is pinned, auditable, and version-controlled, not interpreted from prompts at runtime.

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

## Drill-down example

```python
suite.add(
    "hierarchical_drill_down",
    field="balance",
    segments=["asset_class", "vintage"],   # ordered, coarsest first
    agg="sum",                              # sum / mean / count / min / max
    z_threshold=3.0,                        # |z| over the segment's own history
    min_segment_size=10,                    # skip slices with too few entities
    min_history=14,                         # skip segments with short history
    max_depth=2,                            # optional; defaults to len(segments)
)
```

The detector computes the aggregate at every depth (portfolio → asset_class → asset_class+vintage), z-scores each (segment, snapshot) against the slice's own history, and emits one Issue per "leaf" — the deepest still-anomalous path. A spike that's only visible at `commercial > 2024Q3` produces one Issue at depth 2 (suppressing the portfolio and asset_class-level findings as ancestors). A spike at the `commercial` level with no anomalous descendant produces a depth-1 Issue. The `details["segment_path"]` field carries the structured path for the brief to render.

## Known gaps from the original design

These were named in the original surveillance vision and did not land in the initial build.

- **Severity calibration loop.** `feedback.jsonl` records analyst `[real | false_positive | needs_more]` labels, but no job reads it back into `scoring.py` thresholds today. The capture path is shipped; the consumer is the work.
- **LLM-judged ensemble for the TS detector pool.** The orchestrator runs detectors independently and dedupes overlaps; it does not score-combine TS detectors. `tests/eval/test_ts_ensemble.py` validates a vote-based ensemble outside the runtime; promoting it to the orchestrator is the work.
- **Auto-extraction of rule pages, validated.** `IngestionWorker` exists and the rule-extraction spike script is in place, but until `LENS_RUN_EVAL=1 python3 tests/eval/spike_extract_rule.py` is run against real production code and passes, `lens-wiki/rules/*.md` should be treated as hand-authored.

## Architecture

See `CLAUDE.md` for the full architecture, module layout, key patterns, and conventions for adding new detectors.
