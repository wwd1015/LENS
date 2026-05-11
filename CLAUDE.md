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

# v2 CLIs
python -m lens.brief.markdown <findings.json>          # render Slack-pasteable digest
python -m lens.brief.feedback <finding_id> <label>     # append {real|false_positive|needs_more} to feedback.jsonl
```

The `eval` pytest marker (registered in `pyproject.toml`) gates real-LLM tests; they require `LENS_RUN_EVAL=1` and are excluded from the default CI command above. Run them explicitly with `pytest -m eval` when validating ingestion / RCA quality.

## Architecture

```
src/lens/
├── types.py          # Core data types: Issue, CheckResult, SuiteResult, Severity
├── engine.py         # Suite — orchestrates running multiple checks against data
├── config.py         # YAML config loader → Suite
├── io/               # Data source connectors (DataSource ABC)
│   ├── base.py       # Abstract DataSource with read/read_snapshot/read_history
│   ├── polars_source.py   # In-memory / CSV / Parquet
│   └── snowflake_source.py # Snowflake via ConnectorX
└── checks/           # Check framework + built-in checks
    ├── base.py       # BaseCheck ABC — all checks implement run()
    ├── registry.py   # Global CheckRegistry — @registry.register decorator
    ├── temporal.py   # StaleDataCheck, MonotonicityCheck, VolatilityCheck
    ├── crosssource.py # CrossSourceMatchCheck (uses run_cross() for two sources)
    ├── snapshot.py   # NullCheck, RangeCheck
    └── tabpfn_anomaly.py # TabPFNAnomalyCheck — zero-shot TS anomaly detection (optional [tabpfn] extra)
```

The `/triage-data` skill (`.claude/commands/triage-data.md`) drives a two-phase workflow on top of `TabPFNAnomalyCheck`: detect anomalies, then walk lineage + git history for root-cause analysis. Project-agnostic prompt; dataset/lineage knowledge lives in `LINEAGE.yaml` (schema in `docs/LINEAGE.md`). The architectural growth path (single skill → two-agent split when LLM judgment appears upstream) is recorded in `~/.arc/state/projects/lens/decisions/2026-05-09-architecture-growth-path.md`.

**Key patterns:**
- All data flows as `pl.LazyFrame` for deferred execution
- Checks are registered via `@registry.register` decorator and instantiated by name from YAML or `Suite.add("check_name", **params)`
- Every check returns `CheckResult` containing `Issue` objects with entity_id, field, severity, snapshot_date
- `CrossSourceMatchCheck` is special — it uses `run_cross()` instead of `run()` since it needs two data sources
- Data sources implement `DataSource` ABC with `read()`, `read_snapshot()`, and `read_history()`
- Convention: `entity_col` and `snapshot_col` are configurable column names passed throughout
- **Wiki is the source of truth for cross-source rules**, distilled from production code into structured frontmatter equations. Each `rules/*.md` page carries an `equation` block with `lhs`, `rhs`, and `tolerance`, where each operand specifies `op ∈ {add, sub, mul, div}` and `agg ∈ {None, sum, min, max, mean}`. NO string-eval — `CrossSourceWikiCheck` evaluates the structured spec via Polars expressions (`src/lens/checks/equation.py`).
- **All LLM calls go via subprocess `claude` CLI**, never the Anthropic SDK directly. See the "LLM Access Pattern" section.

## v2 Architecture (LENS Surveillance v2)

V2 layers a multi-agent surveillance pipeline above the v1 check framework. The four-component pipeline is:

```
lens-wiki/  →  DetectionOrchestrator  →  RCAAgent  →  HTML / markdown brief
```

There are two entry points:
- **Scheduled morning brief** — `DetectionOrchestrator.run(...)` → loop RCA over findings → `render_brief()` → HTML on disk. Designed for cron / morning batch.
- **Ad-hoc per-finding RCA** — `.claude/commands/lens-rca.md` skill — given a `finding_id`, looks it up in `findings.latest.json` and runs `RCAAgent.investigate(...)` against the live wiki. Use for investor questions, post-incident analysis, anything off the morning cadence.

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

Schema: every page is YAML frontmatter (fenced by `---`) followed by a markdown body. Parsed by `src/lens/wiki/reader.py` into `RulePage`, `DatasetPage`, or `LineagePage` based on the parent directory. Malformed pages are logged and skipped, never raised.

Update modes:
- **Auto-extracted** — `src/lens/wiki/ingest.py` reads `(dataset, code_path)` and asks the LLM (via the headless-mode pattern below) to draft the page. Incremental — re-runs only touch pages whose source changed.
- **Hand-authored** — analysts hand-write or edit pages directly. The reader treats both modes identically.

### Modules

```
src/lens/
├── orchestrator.py   # DetectionOrchestrator — central run loop, dedupe, findings.json writer
├── scoring.py        # score_to_severity(raw_score, detector) → (Severity, confidence)
├── wiki/             # lens-wiki/ machinery
│   ├── reader.py     # YAML-frontmatter parser → RulePage / DatasetPage / LineagePage
│   ├── cache.py      # WikiCache — eager in-memory snapshot of all pages
│   ├── ingest.py     # ClaudeCodeClient + ingestion worker
│   ├── prompts.py    # ingestion prompt templates
│   └── safety.py     # path containment guards for wiki writes
├── rca/              # RCA agent
│   ├── agent.py      # RCAAgent — per-finding investigator, writes rca/<run_id>/<finding_id>.json
│   ├── prompts.py    # RCA prompt template
│   └── git_links.py  # commit-URL helpers for the brief
├── brief/            # rendering
│   ├── html.py       # render_brief() — Jinja2 + autoescape, self-contained HTML
│   ├── markdown.py   # render_brief_summary() — Slack-pasteable top-5 digest
│   ├── feedback.py   # CLI: append a label to feedback.jsonl
│   └── templates/    # Jinja2 templates + styles.css
└── checks/
    ├── temporal_stl.py     # NEW: STLResidualCheck — classical TS baseline
    ├── crosssource_wiki.py # NEW: CrossSourceWikiCheck — reads rules/*.md
    └── equation.py         # structured-equation evaluator (no string-eval)
```

### Detectors (v2 additions)

All detectors remain pluggable via `@registry.register`. The two new ones:
- **`stl_residual`** — classical seasonal-trend decomposition residual; lives in `src/lens/checks/temporal_stl.py`. Runs alongside `tabpfn_anomaly`.
- **`cross_source_wiki`** — reads every `RulePage` from `WikiCache`, evaluates the structured `equation` spec via Polars expressions, emits one Issue per per-row breach. Detector source is stamped as `cross_source_wiki:<rule_slug>` so `scoring.py` can normalize it back to the `cross_source_wiki` threshold table.

### Orchestrator

`DetectionOrchestrator` composes the existing `Suite` for single-source checks and a separate list of cross-source checks. On each `.run(...)`:
1. Materializes every input source as `pl.LazyFrame`.
2. Builds one `WikiCache` from `wiki_root` (empty cache if `None`) and shares it across every cross-source detector — wiki I/O happens once per run.
3. Runs single-source checks against each source, then runs cross-source checks once over the full source dict via `run_cross(sources, *, wiki, entity_col, snapshot_col)`. Per-check failures are logged and skipped; the rest of the run continues.
4. Scores every Issue and dedupes on `(entity_id, field_name, snapshot_date)` — the dedup key is the SHA1 / uuid5 `finding_id` computed by `compute_finding_id(...)`. The representative Issue is the highest-severity / highest-confidence member of the group; `Finding.detector_sources` lists every detector that flagged the point.
5. Writes `findings.{run_id}.json` and atomically repoints `findings.latest.json` at it via `os.replace` on a temp symlink (safe under concurrent runs).

Only the wiki-style cross-check signature is supported. The legacy two-frame `run_cross(source_a, source_b)` shape from `CrossSourceMatchCheck` is intentionally not wired in — cross-source matching belongs in a wiki rule.

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
