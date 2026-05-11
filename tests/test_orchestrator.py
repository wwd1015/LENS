"""Tests for :class:`lens.orchestrator.DetectionOrchestrator`."""

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import polars as pl

from lens.checks.base import BaseCheck
from lens.checks.crosssource_wiki import CrossSourceWikiCheck
from lens.checks.registry import registry
from lens.orchestrator import DetectionOrchestrator
from lens.types import CheckResult, Issue, Severity

FIXTURE_WIKI = Path(__file__).parent / "fixtures" / "wiki_sample"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_source_with_nulls() -> pl.LazyFrame:
    """A small frame where two rows have null `status` values."""
    return pl.LazyFrame(
        {
            "entity_id": ["a", "b", "c", "d"],
            "snapshot_date": [
                date(2026, 1, 1),
                date(2026, 1, 1),
                date(2026, 1, 1),
                date(2026, 1, 1),
            ],
            "status": ["ok", None, "ok", None],
        }
    )


def _consistent_cross_sources() -> dict[str, pl.LazyFrame]:
    """The same fixture used in test_crosssource_wiki — equation holds exactly."""
    snap1 = date(2026, 1, 31)
    snap2 = date(2026, 2, 28)
    loan_pool = pl.LazyFrame(
        {
            "deal_id": ["d1", "d1", "d1", "d2", "d2", "d1", "d1", "d2", "d2", "d2"],
            "snapshot_date": [snap1, snap1, snap1, snap1, snap1, snap2, snap2, snap2, snap2, snap2],
            "balance": [100.0, 200.0, 300.0, 400.0, 500.0, 250.0, 250.0, 100.0, 200.0, 300.0],
        }
    )
    deal_terms = pl.LazyFrame(
        {
            "deal_id": ["d1", "d2", "d1", "d2"],
            "snapshot_date": [snap1, snap1, snap2, snap2],
            "advance_rate": [0.8, 0.7, 0.8, 0.75],
        }
    )
    senior_debt = pl.LazyFrame(
        {
            "deal_id": ["d1", "d2", "d1", "d2"],
            "snapshot_date": [snap1, snap1, snap2, snap2],
            "balance": [480.0, 630.0, 400.0, 450.0],
        }
    )
    return {"loan_pool": loan_pool, "deal_terms": deal_terms, "senior_debt": senior_debt}


# ---------------------------------------------------------------------------
# Inline custom detectors for dedup + failure-isolation tests. Defined at
# module scope (not inside test functions) so they only register once across
# the pytest session.
# ---------------------------------------------------------------------------


@registry.register
class _AlwaysFlagsCheckA(BaseCheck):
    name = "always_flags_a"
    description = "Always flags entity 'X' on 2026-01-01."
    default_severity = Severity.WARNING

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        issue = Issue(
            check_name=self.name,
            severity=self.severity,
            entity_id="X",
            field_name="balance",
            snapshot_date=date(2026, 1, 1),
            description="flagged by A",
            detector_source="always_flags_a",
        )
        return CheckResult(check_name=self.name, passed=False, issues=[issue])


@registry.register
class _AlwaysFlagsCheckB(BaseCheck):
    name = "always_flags_b"
    description = "Always flags entity 'X' on 2026-01-01 (same key as A)."
    default_severity = Severity.WARNING

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        issue = Issue(
            check_name=self.name,
            severity=self.severity,
            entity_id="X",
            field_name="balance",
            snapshot_date=date(2026, 1, 1),
            description="flagged by B",
            detector_source="always_flags_b",
        )
        return CheckResult(check_name=self.name, passed=False, issues=[issue])


@registry.register
class _RaisingCheck(BaseCheck):
    name = "raising_check"
    description = "Always raises in run()."
    default_severity = Severity.ERROR

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        raise RuntimeError("intentional failure for test")


@registry.register
class _ScoredCheck(BaseCheck):
    """A check that emits an Issue whose `details["score"]` should drive scoring."""

    name = "scored_check"
    description = "Emits a single issue with a controllable raw score."
    default_severity = Severity.INFO

    def __init__(self, raw_score: float = 0.9, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.raw_score = raw_score

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        issue = Issue(
            check_name=self.name,
            severity=self.severity,
            entity_id="Z",
            field_name="anomaly_field",
            snapshot_date=date(2026, 3, 1),
            description="scored issue",
            details={"score": self.raw_score},
            # Use a known detector key so score_to_severity has thresholds.
            detector_source="tabpfn_anomaly",
        )
        return CheckResult(check_name=self.name, passed=False, issues=[issue])


# ---------------------------------------------------------------------------
# Builder API
# ---------------------------------------------------------------------------


def test_add_single_chains_via_suite():
    orch = DetectionOrchestrator()
    returned = orch.add_single("null_check", fields=["status"])
    assert returned is orch
    # The suite should hold exactly one check, of the right type.
    assert len(orch._suite._checks) == 1
    assert orch._suite._checks[0].name == "null_check"


def test_add_cross_chains():
    orch = DetectionOrchestrator()
    returned = orch.add_cross("cross_source_wiki")
    assert returned is orch
    assert len(orch._cross_checks) == 1
    assert isinstance(orch._cross_checks[0], CrossSourceWikiCheck)


# ---------------------------------------------------------------------------
# Run + file output
# ---------------------------------------------------------------------------


def test_run_writes_findings_json_with_run_id(tmp_path):
    orch = DetectionOrchestrator().add_single("null_check", fields=["status"])
    findings = orch.run(
        sources={"loans": _sample_source_with_nulls()},
        wiki_root=None,
        output_dir=tmp_path,
        run_id="testrun-001",
    )
    out = tmp_path / "findings.testrun-001.json"
    assert out.exists()
    data = json.loads(out.read_text())
    # Two null rows in fixture → two findings (different entity_ids).
    assert len(data) == 2
    assert len(findings) == 2
    entity_ids = sorted(f.issue.entity_id for f in findings)
    assert entity_ids == ["b", "d"]
    # File content should match in-memory list.
    file_entity_ids = sorted(d["issue"]["entity_id"] for d in data)
    assert file_entity_ids == ["b", "d"]


def test_run_writes_latest_symlink(tmp_path):
    orch = DetectionOrchestrator().add_single("null_check", fields=["status"])
    orch.run(
        sources={"loans": _sample_source_with_nulls()},
        wiki_root=None,
        output_dir=tmp_path,
        run_id="first",
    )
    latest = tmp_path / "findings.latest.json"
    assert latest.is_symlink()
    assert os.readlink(latest) == "findings.first.json"

    # Second run repoints.
    orch.run(
        sources={"loans": _sample_source_with_nulls()},
        wiki_root=None,
        output_dir=tmp_path,
        run_id="second",
    )
    assert os.readlink(latest) == "findings.second.json"
    # Both run files still exist.
    assert (tmp_path / "findings.first.json").exists()
    assert (tmp_path / "findings.second.json").exists()


def test_dedup_merges_detectors(tmp_path):
    orch = (
        DetectionOrchestrator()
        .add_single("always_flags_a")
        .add_single("always_flags_b")
    )
    findings = orch.run(
        sources={"src": _sample_source_with_nulls()},
        wiki_root=None,
        output_dir=tmp_path,
        run_id="dedup-test",
    )
    # Both detectors flag the same (X, balance, 2026-01-01) → one finding.
    assert len(findings) == 1
    detector_sources = findings[0].detector_sources
    assert sorted(detector_sources) == ["always_flags_a", "always_flags_b"]


def test_scoring_applied(tmp_path):
    # raw_score 0.9 ≥ 0.85 → tabpfn_anomaly ERROR.
    orch = DetectionOrchestrator().add_single("scored_check", raw_score=0.9)
    findings = orch.run(
        sources={"src": _sample_source_with_nulls()},
        wiki_root=None,
        output_dir=tmp_path,
        run_id="score-test",
    )
    assert len(findings) == 1
    issue = findings[0].issue
    assert issue.severity == Severity.ERROR
    assert 0.0 < issue.confidence <= 1.0
    # And a stable finding_id should be set.
    assert issue.finding_id


def test_cross_source_wiki_round_trip(tmp_path):
    orch = DetectionOrchestrator(
        entity_col="deal_id", snapshot_col="snapshot_date"
    ).add_cross("cross_source_wiki")

    # Consistent sources: should pass with no findings.
    sources = _consistent_cross_sources()
    findings = orch.run(
        sources=sources,
        wiki_root=FIXTURE_WIKI,
        output_dir=tmp_path,
        run_id="cross-clean",
    )
    assert findings == []

    # Perturb d2@snap2 senior_debt — should now produce exactly one finding.
    perturbed = dict(sources)
    perturbed["senior_debt"] = pl.LazyFrame(
        {
            "deal_id": ["d1", "d2", "d1", "d2"],
            "snapshot_date": [
                date(2026, 1, 31), date(2026, 1, 31),
                date(2026, 2, 28), date(2026, 2, 28),
            ],
            "balance": [480.0, 630.0, 400.0, 500.0],  # was 450, now 500 → ~11% off
        }
    )
    findings = orch.run(
        sources=perturbed,
        wiki_root=FIXTURE_WIKI,
        output_dir=tmp_path,
        run_id="cross-broken",
    )
    assert len(findings) == 1
    issue = findings[0].issue
    assert issue.entity_id == "d2"
    assert issue.snapshot_date == date(2026, 2, 28)
    # detector_source carries the rule slug; detector_sources list has it too.
    assert findings[0].detector_sources == ["cross_source_wiki:rule_a"]


def test_detector_failure_logged_and_continues(tmp_path, caplog):
    orch = (
        DetectionOrchestrator()
        .add_single("raising_check")
        .add_single("null_check", fields=["status"])
    )
    with caplog.at_level("ERROR"):
        findings = orch.run(
            sources={"src": _sample_source_with_nulls()},
            wiki_root=None,
            output_dir=tmp_path,
            run_id="failure-test",
        )
    # null_check still produced its 2 findings.
    assert len(findings) == 2
    # The raising check's failure was logged.
    assert any("raising_check" in rec.message for rec in caplog.records)


def test_run_id_autogenerated_unique(tmp_path):
    orch = DetectionOrchestrator().add_single("null_check", fields=["status"])
    orch.run(
        sources={"src": _sample_source_with_nulls()},
        wiki_root=None,
        output_dir=tmp_path,
    )
    # Sleep briefly so the second timestamp isn't guaranteed to match (the
    # hex suffix should already make them unique, but defensive).
    time.sleep(0.01)
    orch.run(
        sources={"src": _sample_source_with_nulls()},
        wiki_root=None,
        output_dir=tmp_path,
    )
    findings_files = sorted(
        p.name
        for p in tmp_path.iterdir()
        if p.name.startswith("findings.")
        and p.name.endswith(".json")
        and p.name != "findings.latest.json"
    )
    assert len(findings_files) == 2
    assert findings_files[0] != findings_files[1]


def test_empty_sources_writes_empty_findings_file(tmp_path):
    orch = DetectionOrchestrator().add_single("null_check", fields=["status"])
    findings = orch.run(
        sources={},
        wiki_root=None,
        output_dir=tmp_path,
        run_id="empty-test",
    )
    assert findings == []
    out = tmp_path / "findings.empty-test.json"
    assert out.exists()
    assert json.loads(out.read_text()) == []
    latest = tmp_path / "findings.latest.json"
    assert latest.is_symlink()
    assert os.readlink(latest) == "findings.empty-test.json"
