# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LENS (Longitudinal & Entity-level Normative Surveillance) is a data quality control tool for commercial lending data. It detects anomalies across time (longitudinal), across data sources (cross-source reconciliation), and at point-in-time (snapshot checks). Built on Polars for performance.

## Commands

```bash
python3 -m pip install -e ".[dev]"          # install for development
python3 -m pip install -e ".[snowflake]"    # with Snowflake connector
python3 -m pytest tests/ -v                 # run all tests
python3 -m pytest tests/ -q -m "not eval"   # CI run: skip real-LLM eval tests
python3 -m pytest tests/test_checks.py::test_null_check -v  # run single test
ruff check src/ tests/                      # lint
ruff format src/ tests/                     # format

# Brief CLIs
python -m lens.brief.markdown <findings.json>          # render Slack-pasteable digest
python -m lens.brief.feedback <finding_id> <label>     # append {real|false_positive|needs_more} to feedback.jsonl
```

The `eval` pytest marker (registered in `pyproject.toml`) gates real-LLM tests; they require `LENS_RUN_EVAL=1` and are excluded from the default CI command above. Run them explicitly with `pytest -m eval` when validating ingestion / RCA quality.

## Architecture

```
src/lens/
├── types.py             # Issue, Finding, RCAResult, CheckResult, SuiteResult, Severity, compute_finding_id
├── engine.py            # Suite — runs a list of checks against one DataSource
├── orchestrator.py      # DetectionOrchestrator — composes Suite + cross-source list, scores, dedupes, writes findings.json
├── config.py            # YAML config loader → Suite
├── scoring.py           # score_to_severity(raw_score, detector) → (Severity, confidence)
├── io/                  # Data source connectors (DataSource ABC)
│   ├── base.py          # Abstract DataSource with read/read_snapshot/read_history
│   ├── polars_source.py # In-memory / CSV / Parquet
│   └── snowflake_source.py # Snowflake via ConnectorX
├── checks/              # Detection framework + built-in detectors
│   ├── base.py          # BaseCheck ABC
│   ├── registry.py      # Global CheckRegistry — @registry.register
│   ├── snapshot.py      # NullCheck, RangeCheck
│   ├── temporal.py      # StaleDataCheck, MonotonicityCheck, VolatilityCheck
│   ├── temporal_stl.py  # STLResidualCheck — classical seasonal-trend residual
│   ├── drill_down.py    # HierarchicalDrillDownCheck — top-down per-segment z-score; emits deepest-leaf paths
│   ├── crosssource.py   # CrossSourceMatchCheck (two-source positional)
│   ├── crosssource_wiki.py # CrossSourceWikiCheck — reads structured rules from lens-wiki/
│   ├── equation.py      # Structured-equation evaluator (no string-eval)
│   └── tabpfn_anomaly.py # TabPFNAnomalyCheck — zero-shot TS (optional [tabpfn] extra)
├── wiki/                # lens-wiki/ machinery
│   ├── reader.py        # YAML-frontmatter parser → RulePage / DatasetPage / LineagePage
│   ├── cache.py         # WikiCache — eager in-memory snapshot of all pages
│   ├── ingest.py        # ClaudeCodeClient + IngestionWorker (LLM-driven)
│   ├── prompts.py       # Ingestion prompt templates
│   └── safety.py        # Path + content guards before sending files to the LLM
├── rca/                 # Root-cause investigator
│   ├── agent.py         # RCAAgent — per-finding, writes rca/<run_id>/<finding_id>.json
│   ├── prompts.py       # RCA prompt template
│   └── git_links.py     # GitHub/GitLab commit-URL helpers
└── brief/               # Rendering
    ├── html.py          # render_brief() — Jinja2 + autoescape, self-contained HTML
    ├── markdown.py      # render_brief_summary() — Slack-pasteable top-5 digest
    ├── feedback.py      # CLI: append a label to feedback.jsonl
    └── templates/       # Jinja2 templates + styles.css
```

The pipeline:

```
lens-wiki/  →  DetectionOrchestrator  →  RCAAgent  →  HTML / markdown brief
```

Two entry points:
- **Scheduled batch** — `DetectionOrchestrator.run(...)` → RCA loop → `render_brief()` → HTML on disk. Designed for cron / morning brief.
- **Ad-hoc per-finding** — `.claude/commands/lens-rca.md` slash command. Given `--finding-id <id>`, looks it up in `findings.latest.json` and runs `RCAAgent.investigate(...)`. Given `--investigate-entity <id> --field <f> --date <d>`, synthesizes a finding-like wrapper and runs RCA directly — for investor questions or post-incident work, no orchestrator run needed.

The older `/triage-data` skill (`.claude/commands/triage-data.md`) is a slimmer single-trace alternative built around `TabPFNAnomalyCheck` + a `LINEAGE.yaml` (schema in `docs/LINEAGE.md`). It coexists with the orchestrator pipeline — use it when you want one LLM trace over detection + RCA fused; use the orchestrator pipeline when you want pluggable detectors, dedup across them, severity/confidence scoring, and the HTML brief.

**Key patterns:**
- All data flows as `pl.LazyFrame` for deferred execution
- Checks are registered via `@registry.register` decorator and instantiated by name from YAML or `Suite.add("check_name", **params)`
- Every check returns `CheckResult` containing `Issue` objects with entity_id, field, severity, snapshot_date
- `CrossSourceMatchCheck` is special — it uses `run_cross()` instead of `run()` since it needs two data sources
- Data sources implement `DataSource` ABC with `read()`, `read_snapshot()`, and `read_history()`
- Convention: `entity_col` and `snapshot_col` are configurable column names passed throughout
- **Wiki is the source of truth for cross-source rules**, distilled from production code into structured frontmatter equations. Each `rules/*.md` page carries an `equation` block with `lhs`, `rhs`, and `tolerance`, where each operand specifies `op ∈ {add, sub, mul, div}` and `agg ∈ {None, sum, min, max, mean}`. NO string-eval — `CrossSourceWikiCheck` evaluates the structured spec via Polars expressions (`src/lens/checks/equation.py`).
- **All LLM calls go via subprocess `claude` CLI**, never the Anthropic SDK directly. See the "LLM Access Pattern" section.

### lens-wiki/ convention

`lens-wiki/` is the source-of-truth directory of markdown pages (data, not code), distilled from production lineage and transformation code:

```
lens-wiki/
├── index.md          # human-curated table of contents
├── datasets/         # one page per table — grain, segments, lineage pointer
├── rules/            # one page per cross-source equation (structured frontmatter)
├── lineage/          # one page per producing-code path / dataflow node
└── changes/          # changelog entries describing wiki updates
```

Every page is YAML frontmatter (fenced by `---`) followed by a markdown body. `src/lens/wiki/reader.py` parses pages into `RulePage`, `DatasetPage`, or `LineagePage` based on the parent directory. Malformed pages are logged and skipped, never raised.

Update modes:
- **Auto-extracted** — `src/lens/wiki/ingest.py` reads `(dataset, code_path)` and asks the LLM (via the headless-mode pattern below) to draft the page. Incremental — re-runs only touch pages whose source changed.
- **Hand-authored** — analysts hand-write or edit pages directly. The reader treats both modes identically.

### Orchestrator

`DetectionOrchestrator` composes the `Suite` for single-source checks plus a separate list of cross-source checks. On each `.run(...)`:
1. Materializes every input source as `pl.LazyFrame`.
2. Builds one `WikiCache` from `wiki_root` (empty cache if `None`) and shares it across every cross-source detector — wiki I/O happens once per run.
3. Runs single-source checks against each source, then runs cross-source checks once over the full source dict via `run_cross(sources, *, wiki, entity_col, snapshot_col)`. Per-check failures are logged and skipped; the rest of the run continues.
4. Scores every Issue and dedupes on `(entity_id, field_name, snapshot_date)` — the dedup key is the uuid5 `finding_id` computed by `compute_finding_id(...)`. The representative Issue is the highest-severity / highest-confidence member of the group; `Finding.detector_sources` lists every detector that flagged the point.
5. Writes `findings.{run_id}.json` and atomically repoints `findings.latest.json` at it via `os.replace` on a temp symlink (safe under concurrent runs).

Only the wiki-style cross-check signature is supported by the orchestrator. The two-frame `run_cross(source_a, source_b)` shape from `CrossSourceMatchCheck` is intentionally not wired in — cross-source matching belongs in a wiki rule.

### Detectors

All detectors register via `@registry.register`. The cross-source / classical-TS / drill-down additions:
- **`stl_residual`** — classical seasonal-trend decomposition residual; lives in `src/lens/checks/temporal_stl.py`. Runs alongside `tabpfn_anomaly`. Guards short-history and constant-value series — both skip silently rather than crashing.
- **`cross_source_wiki`** — reads every `RulePage` from `WikiCache`, evaluates the structured `equation` spec via Polars expressions (`src/lens/checks/equation.py`), emits one Issue per per-row breach. Detector source is stamped as `cross_source_wiki:<rule_slug>` so `scoring.py` can normalize it back to the `cross_source_wiki` threshold table.
- **`hierarchical_drill_down`** — aggregates a numeric `field` over every prefix of an ordered `segments=[...]` list (depth 0 = portfolio, depth N = all segments). At each depth, every segment-combination's aggregate time series is z-scored against its own history; anomalies are computed INDEPENDENTLY at every level. The detector then emits only the deepest still-anomalous path for each (snapshot, root chain) — ancestor anomalies whose descendants are also anomalous on the same date are suppressed. `details["segment_path"]` carries the structured path. Guards `min_history`, `min_segment_size`, constant series. Lives in `src/lens/checks/drill_down.py`.

### RCA agent

`RCAAgent` is a per-finding investigator. For each `Finding` it gathers a structured context bundle:
- The wiki entry / lineage / rule pages referenced by the finding's field
- `git log -- <code_path>` for recent commits on each producing-code path
- A sampled contrast set of anomalous-vs-prior rows from the underlying data

It then calls the LLM via the headless-mode pattern (below) and persists an `RCAResult` to `output_dir/rca/<run_id>/<finding_id>.json`. Same `LLMClient` Protocol as the wiki ingestion worker — tests substitute a stub client.

### Brief

- `render_brief(findings, rcas, ...)` produces a self-contained HTML page using Jinja2 with `autoescape=select_autoescape` — LLM-authored hostile content in descriptions / hypotheses is escaped, not rendered. Findings are grouped by upstream / field and a "what changed since previous run" header diffs against the last brief.
- `render_brief_summary(findings, ...)` produces a Slack-pasteable markdown digest of the top-5; expose via `python -m lens.brief.markdown`.
- Feedback capture — the HTML brief has one-click `[real] / [false positive] / [needs more]` buttons that post to a local handler; `python -m lens.brief.feedback <finding_id> <label>` is the CLI equivalent. Both append a JSON line to `feedback.jsonl`.

## LLM Access Pattern

All runtime LLM calls go through Claude Code headless mode — `ClaudeCodeClient` shells out to `claude -p "<prompt>" --output-format text` as a subprocess. NO `anthropic` SDK is used; LENS environments authenticate via Claude Code SSO only. Both `src/lens/wiki/ingest.py` and `src/lens/rca/agent.py` accept any object conforming to the `LLMClient` Protocol, so unit tests inject deterministic stubs and never hit the network.

## Adding a New Check

1. Create a class extending `BaseCheck` in the appropriate module (or new file under `checks/`)
2. Set `name`, `description`, `default_severity` class attributes
3. Implement `run(data, *, entity_col, snapshot_col) -> CheckResult`
4. Decorate with `@registry.register`
5. Add tests in `tests/`

## Adding a new TS detector

Same as adding a regular check (BaseCheck + `@registry.register` + tests). For time-series detectors specifically:
1. Guard short-history series — define a minimum points threshold and emit zero issues (not a crash) below it.
2. Guard constant-value series — zero-variance windows blow up STL / z-scores; detect and skip.
3. Register a threshold row in `lens/scoring.py::DEFAULT_THRESHOLDS` so the orchestrator can map your raw score → `(Severity, confidence)`.
4. See `STLResidualCheck` in `src/lens/checks/temporal_stl.py` for the canonical short-history + constant-value guard pattern — both are real crash modes on lending-data shape.
