"""End-to-end integration test for LENS Surveillance v2 (T11).

Exercises the full detection pipeline:

  CSV fixtures  →  LazyFrames
       │
       ▼
  DetectionOrchestrator (STL single-source + cross_source_wiki)
       │
       ▼
  findings.{run_id}.json (+ findings.latest.json symlink)

T11 is scoped to the orchestrator side of the pipeline. RCA + HTML brief land
in T9 / T10 / T11 sibling waves; the failure-path test here mocks an
in-orchestrator detector crash to verify continued execution and logging.

Synthetic data design (see `tests/fixtures/v2/synthetic_data/gen.py`):

  D1 — clean baseline EXCEPT day 60 has a senior_debt spike with no matching
       loan_pool move. The equation breaks (cross_source_wiki fires) AND the
       STL residual on senior_debt blows past z=5 (stl_residual fires). After
       dedup these merge into one Finding listing both detector sources — the
       "bonus dedup case" in the plan.

  D2 — clean EXCEPT day 30 has a wrong recorded advance_rate (0.75 vs 0.80).
       The recorded balances are still internally consistent so STL does NOT
       fire; only the cross-source equation breaks.

  D3 — clean EXCEPT day 60 has BOTH senior_debt and loan_pool spike
       proportionally (50 / 0.80 = 62.5), so the equation holds and only STL
       fires. STL fires on both source frames at the same (entity, field,
       snapshot) key, which deduplicates to one Finding listing only
       `stl_residual` in detector_sources.

Distinct entity_ids in the output: D1, D2, D3 → exactly 3 findings.

Spike magnitude (50.0) was tuned to survive `STL(robust=True)` outlier
absorption — empirically anything ≥ 100 collapses into the seasonal component
and z@target drops below threshold. See `gen.py` for the seed sweep notes.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from pathlib import Path

import polars as pl

from lens.checks.base import BaseCheck
from lens.checks.registry import registry
from lens.orchestrator import DetectionOrchestrator
from lens.types import CheckResult, Severity

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "v2"
DATA_DIR = FIXTURE_ROOT / "synthetic_data"
SEED_WIKI = FIXTURE_ROOT / "seed_wiki"

# Spike day-indices from the generator (kept in sync with `gen.py`). Day 0 is
# 2026-01-01, so day 30 is 2026-01-31 and day 60 is 2026-03-02.
D1_SPIKE_DATE = date(2026, 3, 2)
D2_SPIKE_DATE = date(2026, 1, 31)
D3_SPIKE_DATE = date(2026, 3, 2)

# z_threshold tuned alongside the spike magnitude — see module docstring.
STL_Z_THRESHOLD = 5.0

CROSS_DETECTOR = "cross_source_wiki:senior-debt-equals-pool-x-rate"
STL_DETECTOR = "stl_residual"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _load_csv(name: str) -> pl.LazyFrame:
    """Read a synthetic-data CSV into a LazyFrame with parsed dates."""
    return pl.read_csv(DATA_DIR / name, try_parse_dates=True).lazy()


def _sources() -> dict[str, pl.LazyFrame]:
    return {
        "loan_pool": _load_csv("loan_pool.csv"),
        "deal_terms": _load_csv("deal_terms.csv"),
        "senior_debt": _load_csv("senior_debt.csv"),
    }


def _build_orchestrator() -> DetectionOrchestrator:
    return (
        DetectionOrchestrator(entity_col="entity_id", snapshot_col="snapshot_date")
        .add_single("stl_residual", field="balance", z_threshold=STL_Z_THRESHOLD)
        .add_cross("cross_source_wiki")
    )


# A check that always blows up. Registered once at module scope so multiple
# tests can include it without re-registration warnings.
@registry.register
class _CrashingCheck(BaseCheck):
    name = "integration_v2_crashing_check"
    description = "Always raises in .run() — integration-test scaffold."
    default_severity = Severity.WARNING

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        raise RuntimeError("integration_v2_crashing_check: intentional failure")


# ---------------------------------------------------------------------------
# Happy path — full pipeline
# ---------------------------------------------------------------------------


def test_happy_path_end_to_end(tmp_path):
    orch = _build_orchestrator()

    findings = orch.run(
        sources=_sources(),
        wiki_root=SEED_WIKI,
        output_dir=tmp_path,
        run_id="happy-001",
    )

    # --- file output -------------------------------------------------------
    run_file = tmp_path / "findings.happy-001.json"
    latest = tmp_path / "findings.latest.json"
    assert run_file.exists(), "findings.{run_id}.json must be written"
    assert latest.is_symlink(), "findings.latest.json must be a symlink"
    assert os.readlink(latest) == "findings.happy-001.json"

    payload = json.loads(run_file.read_text())
    assert isinstance(payload, list)
    assert len(payload) == len(findings) == 3, (
        "Expected exactly three deduplicated findings (D1, D2, D3); "
        f"got findings={findings!r}"
    )

    # --- coverage: all three seeded entities appear ------------------------
    entity_ids = {f.issue.entity_id for f in findings}
    assert entity_ids == {"D1", "D2", "D3"}, (
        f"Expected findings for D1, D2, D3; got {sorted(entity_ids)}"
    )

    by_entity = {f.issue.entity_id: f for f in findings}

    # --- D2: cross-source only --------------------------------------------
    d2 = by_entity["D2"]
    assert d2.issue.snapshot_date == D2_SPIKE_DATE, (
        f"D2 should flag at the seeded date {D2_SPIKE_DATE}, got {d2.issue.snapshot_date}"
    )
    assert CROSS_DETECTOR in d2.detector_sources
    assert STL_DETECTOR not in d2.detector_sources, (
        "D2's anomaly is in advance_rate; balance is unchanged, STL should not fire"
    )

    # --- D3: STL only -----------------------------------------------------
    d3 = by_entity["D3"]
    assert d3.issue.snapshot_date == D3_SPIKE_DATE
    assert STL_DETECTOR in d3.detector_sources
    assert CROSS_DETECTOR not in d3.detector_sources, (
        "D3's spike is balanced (pool also spiked) so the equation holds; "
        "cross_source_wiki should not fire"
    )

    # --- D1: BOTH detectors (the dedup case) ------------------------------
    d1 = by_entity["D1"]
    assert d1.issue.snapshot_date == D1_SPIKE_DATE
    assert STL_DETECTOR in d1.detector_sources
    assert CROSS_DETECTOR in d1.detector_sources, (
        f"D1's dedup-case finding must list BOTH detectors; got {d1.detector_sources}"
    )
    # Severity should be the higher of the two — both detectors produce raw
    # scores past the CRITICAL threshold for this synthetic case.
    assert d1.issue.severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# Failure path — orchestrator must keep going when one detector crashes
# ---------------------------------------------------------------------------


def test_failure_path_orchestrator_continues_on_detector_crash(tmp_path, caplog):
    orch = (
        DetectionOrchestrator(entity_col="entity_id", snapshot_col="snapshot_date")
        .add_single("integration_v2_crashing_check")
        .add_single("stl_residual", field="balance", z_threshold=STL_Z_THRESHOLD)
        .add_cross("cross_source_wiki")
    )

    with caplog.at_level(logging.ERROR, logger="lens.orchestrator"):
        findings = orch.run(
            sources=_sources(),
            wiki_root=SEED_WIKI,
            output_dir=tmp_path,
            run_id="failure-001",
        )

    # The crash was logged on the orchestrator logger.
    crash_logged = any(
        "integration_v2_crashing_check" in rec.message for rec in caplog.records
    )
    assert crash_logged, (
        "Detector failure must be logged via the orchestrator logger; "
        f"got records={[r.message for r in caplog.records]}"
    )

    # Despite the crash, the rest of the run produced the normal three findings.
    run_file = tmp_path / "findings.failure-001.json"
    assert run_file.exists(), "findings file must be written even after a detector crash"
    entity_ids = {f.issue.entity_id for f in findings}
    assert entity_ids == {"D1", "D2", "D3"}, (
        f"Healthy detectors should still emit D1/D2/D3 findings; got {sorted(entity_ids)}"
    )


# ---------------------------------------------------------------------------
# No wiki — cross-source quietly emits nothing; single-source still runs
# ---------------------------------------------------------------------------


def test_no_wiki_runs_with_empty_cache(tmp_path):
    """With `wiki_root=None`, cross_source_wiki sees zero rules and produces
    no issues. STL still runs against every source, so we expect findings
    only from D1 and D3 (the two entities with balance spikes)."""
    orch = _build_orchestrator()

    findings = orch.run(
        sources=_sources(),
        wiki_root=None,
        output_dir=tmp_path,
        run_id="no-wiki-001",
    )

    entity_ids = {f.issue.entity_id for f in findings}
    assert "D2" not in entity_ids, (
        "Without the wiki rule, D2's rate-only anomaly is invisible to STL "
        "and must not show up in findings."
    )
    assert entity_ids == {"D1", "D3"}, (
        f"Expected STL-only findings for D1 and D3; got {sorted(entity_ids)}"
    )

    for f in findings:
        assert STL_DETECTOR in f.detector_sources
        for src in f.detector_sources:
            assert not src.startswith("cross_source_wiki"), (
                f"Empty wiki must not produce cross-source findings; got {src}"
            )

    # And the findings file is still written.
    assert (tmp_path / "findings.no-wiki-001.json").exists()


# Also verify that pointing at a non-existent path is graceful (WikiCache
# logs a warning and returns an empty cache).
def test_nonexistent_wiki_root_is_graceful(tmp_path):
    orch = _build_orchestrator()
    missing = tmp_path / "does-not-exist"
    findings = orch.run(
        sources=_sources(),
        wiki_root=missing,
        output_dir=tmp_path,
        run_id="missing-wiki",
    )
    # Same as the no-wiki path: STL-only findings for D1 and D3.
    entity_ids = {f.issue.entity_id for f in findings}
    assert entity_ids == {"D1", "D3"}
    assert (tmp_path / "findings.missing-wiki.json").exists()


# ---------------------------------------------------------------------------
# Performance budget — happy path under 30 seconds
# ---------------------------------------------------------------------------


def test_run_completes_in_under_30_seconds(tmp_path):
    orch = _build_orchestrator()
    start = time.perf_counter()
    orch.run(
        sources=_sources(),
        wiki_root=SEED_WIKI,
        output_dir=tmp_path,
        run_id="perf-001",
    )
    elapsed = time.perf_counter() - start
    assert elapsed < 30.0, (
        f"Happy path must complete in under 30s; took {elapsed:.2f}s"
    )
