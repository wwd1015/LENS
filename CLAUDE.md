# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LENS (Longitudinal & Entity-level Normative Surveillance) is a data quality control tool for commercial lending data. It detects anomalies across time (longitudinal), across data sources (cross-source reconciliation), and at point-in-time (snapshot checks). Built on Polars for performance.

## Commands

```bash
python3 -m pip install -e ".[dev]"          # install for development
python3 -m pip install -e ".[snowflake]"    # with Snowflake connector
python3 -m pytest tests/ -v                 # run all tests
python3 -m pytest tests/test_checks.py::test_null_check -v  # run single test
ruff check src/ tests/                      # lint
ruff format src/ tests/                     # format
```

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

## Adding a New Check

1. Create a class extending `BaseCheck` in the appropriate module (or new file under `checks/`)
2. Set `name`, `description`, `default_severity` class attributes
3. Implement `run(data, *, entity_col, snapshot_col) -> CheckResult`
4. Decorate with `@registry.register`
5. Add tests in `tests/`
