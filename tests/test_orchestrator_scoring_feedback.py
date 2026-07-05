"""Orchestrator-level tests: self-scoring preservation, agreement boost,
feedback suppression wiring."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import polars as pl

from lens.checks.base import BaseCheck
from lens.checks.registry import registry
from lens.orchestrator import DetectionOrchestrator
from lens.types import CheckResult, Issue, Severity

SNAP = datetime(2026, 6, 9, tzinfo=UTC)


def _source():
    return pl.LazyFrame(
        {
            "entity_id": ["a", "b"],
            "snapshot_date": [SNAP, SNAP],
            "status": [None, "ok"],
            "balance": [100.0, 200.0],
        }
    )


def _make_flagging_check(check_name: str, detector: str, details: dict | None = None):
    """A check that always flags (a, balance, SNAP) with the given identity."""

    class _Check(BaseCheck):
        name = check_name
        description = "test"
        default_severity = Severity.WARNING

        def run(self, data, *, entity_col="entity_id", snapshot_col="snapshot_date"):
            issue = Issue(
                check_name=self.name,
                severity=Severity.WARNING,
                entity_id="a",
                field_name="balance",
                snapshot_date=SNAP,
                confidence=0.5,
                detector_source=detector,
                details=dict(details or {}),
            )
            return CheckResult(check_name=self.name, passed=False, issues=[issue])

    _Check.__name__ = f"Check_{check_name}"
    return _Check


def test_self_scoring_detector_keeps_its_severity(tmp_path):
    """null_check has no threshold row — its ERROR must survive rescoring."""
    orch = DetectionOrchestrator().add_single("null_check", fields=["status"])
    findings = orch.run(
        sources={"src": _source()},
        output_dir=tmp_path,
        run_id="selfscore",
    )
    assert len(findings) == 1
    assert findings[0].issue.severity is Severity.ERROR
    assert findings[0].issue.confidence == 1.0  # Issue default, preserved


def test_thresholded_detector_still_rescored(tmp_path):
    cls = _make_flagging_check(
        "fake_stl", "stl_residual", details={"z_score": 4.5}
    )
    orch = DetectionOrchestrator().add_single(cls())
    findings = orch.run(
        sources={"src": _source()}, output_dir=tmp_path, run_id="rescored"
    )
    assert findings[0].issue.severity is Severity.ERROR  # 4.5 ≥ 4.0


def test_agreement_boost_two_families(tmp_path):
    a = _make_flagging_check("fake_stl2", "stl_residual", details={"z_score": 3.5})
    b = _make_flagging_check("fake_tabpfn", "tabpfn_anomaly", details={"score": 0.75})
    orch = DetectionOrchestrator().add_single(a()).add_single(b())
    findings = orch.run(
        sources={"src": _source()}, output_dir=tmp_path, run_id="boost"
    )
    assert len(findings) == 1
    f = findings[0]
    boost = f.issue.details.get("agreement_boost")
    assert boost is not None
    assert sorted(boost["families"]) == ["stl_residual", "tabpfn_anomaly"]
    before = boost["confidence_before"]  # stored rounded to 6 decimals
    expected = before + (1.0 - before) * 0.5
    assert abs(f.issue.confidence - expected) < 1e-5
    assert f.issue.confidence > before


def test_no_boost_single_family(tmp_path):
    orch = DetectionOrchestrator().add_single("null_check", fields=["status"])
    findings = orch.run(
        sources={"src": _source()}, output_dir=tmp_path, run_id="noboost"
    )
    assert "agreement_boost" not in (findings[0].issue.details or {})


def test_no_boost_same_family_namespaced_rules(tmp_path):
    """Two rules of the same family (cross_source_wiki:a / :b) don't boost."""
    a = _make_flagging_check("rule_a_chk", "cross_source_wiki:rule_a", details={"diff": 0.06})
    b = _make_flagging_check("rule_b_chk", "cross_source_wiki:rule_b", details={"diff": 0.06})
    orch = DetectionOrchestrator().add_single(a()).add_single(b())
    findings = orch.run(
        sources={"src": _source()}, output_dir=tmp_path, run_id="samefam"
    )
    assert len(findings) == 1
    assert "agreement_boost" not in (findings[0].issue.details or {})


def test_run_applies_feedback_suppression(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    fb.write_text(
        json.dumps(
            {
                "finding_id": "x",
                "label": "false_positive",
                "ts": datetime.now(UTC).isoformat(),
                "entity_id": "a",
                "field_name": "status",
                "detector_sources": ["null_check"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    orch = DetectionOrchestrator().add_single("null_check", fields=["status"])
    findings = orch.run(
        sources={"src": _source()},
        output_dir=tmp_path,
        run_id="fbwired",
        feedback_path=fb,
    )
    assert len(findings) == 1  # downgraded, never dropped
    assert findings[0].issue.severity is Severity.INFO
    # And the written findings.json reflects the downgrade (ADR 0001).
    data = json.loads((tmp_path / "findings.fbwired.json").read_text())
    assert data[0]["issue"]["severity"] == "info"
    assert data[0]["issue"]["details"]["suppressed_by_feedback"]


def test_single_check_source_scoping(tmp_path):
    """A check scoped to one source never runs against the others."""
    no_status = pl.LazyFrame(
        {"entity_id": ["z"], "snapshot_date": [SNAP], "balance": [1.0]}
    )
    orch = DetectionOrchestrator().add_single(
        "null_check", fields=["status"], sources=["with_status"]
    )
    findings = orch.run(
        sources={"with_status": _source(), "without_status": no_status},
        output_dir=tmp_path,
        run_id="scoped",
    )
    # One null in with_status; without_status (no column) raises nothing
    # because the check never touched it.
    assert len(findings) == 1
    assert findings[0].issue.entity_id == "a"


def test_negative_z_score_is_scored_by_magnitude(tmp_path):
    """A 6-sigma DROP must be CRITICAL — signed scores previously fell below
    every one-sided threshold and buried downside breaks at (INFO, ~0)."""
    cls = _make_flagging_check("fake_stl_neg", "stl_residual", details={"z_score": -6.0})
    orch = DetectionOrchestrator().add_single(cls())
    findings = orch.run(
        sources={"src": _source()}, output_dir=tmp_path, run_id="negz"
    )
    assert findings[0].issue.severity is Severity.CRITICAL
    assert findings[0].issue.confidence > 0.9


def test_failed_source_emits_critical_finding(tmp_path):
    """An unreadable source is an incident, not a silent 'all clear'."""
    from lens.io.base import DataSource

    class _DeadSource(DataSource):
        def read(self):
            raise OSError("connection refused")

        def read_snapshot(self, snapshot_date):
            raise OSError("connection refused")

        def read_history(self, entity_id):
            raise OSError("connection refused")

    orch = DetectionOrchestrator().add_single("null_check", fields=["status"])
    findings = orch.run(
        sources={"good": _source(), "dead": _DeadSource()},
        output_dir=tmp_path,
        run_id="deadsrc",
    )
    unavailable = [f for f in findings if f.issue.check_name == "source_unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].issue.severity is Severity.CRITICAL
    assert unavailable[0].issue.field_name == "dead"
    assert "connection refused" in unavailable[0].issue.description
    # The healthy source's checks still ran.
    assert any(f.issue.check_name == "null_check" for f in findings)


def test_no_boost_across_different_sources(tmp_path):
    """Two families flagging the same (entity, field, date) in DIFFERENT
    tables looked at different data — that is not corroboration."""
    a = _make_flagging_check("fake_stl3", "stl_residual", details={"z_score": 3.5})
    b = _make_flagging_check("fake_tabpfn2", "tabpfn_anomaly", details={"score": 3.5})
    orch = (
        DetectionOrchestrator()
        .add_single(a(), sources=["src_one"])
        .add_single(b(), sources=["src_two"])
    )
    findings = orch.run(
        sources={"src_one": _source(), "src_two": _source()},
        output_dir=tmp_path,
        run_id="xsrc",
    )
    assert len(findings) == 1
    f = findings[0]
    assert "agreement_boost" not in (f.issue.details or {})
    # But the merge across tables is recorded.
    assert f.issue.details.get("sources") == ["src_one", "src_two"]


def test_boost_with_cross_source_detector(tmp_path):
    """A cross-source detector (no __source__) corroborates any source."""

    class _FakeCross(BaseCheck):
        name = "fake_cross"
        description = "test"
        default_severity = Severity.WARNING

        def run(self, data, *, entity_col="entity_id", snapshot_col="snapshot_date"):
            return CheckResult(check_name=self.name, passed=True)

        def run_cross(self, sources, *, wiki, entity_col, snapshot_col):
            issue = Issue(
                check_name=self.name,
                severity=Severity.WARNING,
                entity_id="a",
                field_name="balance",
                snapshot_date=SNAP,
                confidence=0.5,
                detector_source="cross_source_wiki:rule_x",
                details={"diff": 0.06},
            )
            return CheckResult(check_name=self.name, passed=False, issues=[issue])

    single = _make_flagging_check("fake_stl4", "stl_residual", details={"z_score": 3.5})
    orch = DetectionOrchestrator().add_single(single()).add_cross(_FakeCross())
    findings = orch.run(
        sources={"src": _source()}, output_dir=tmp_path, run_id="crossboost"
    )
    assert len(findings) == 1
    boost = findings[0].issue.details.get("agreement_boost")
    assert boost is not None
    assert sorted(boost["families"]) == ["cross_source_wiki", "stl_residual"]


# Register the dynamic classes once at import so registry.create-by-name in
# other tests is unaffected (we pass instances directly above).
_ = registry
