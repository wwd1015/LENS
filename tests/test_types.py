"""Tests for v2 types extensions: finding_id, Finding, RCAResult."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime

from lens.types import (
    LENS_FINDING_NAMESPACE,
    Finding,
    Issue,
    RCAResult,
    Severity,
    compute_finding_id,
)


def test_namespace_is_stable():
    assert str(LENS_FINDING_NAMESPACE) == "8c4b3a82-9d31-4a4c-8a16-1c2e5b6a0d10"


def test_finding_id_is_deterministic():
    fid_a = compute_finding_id("L1", "balance", datetime(2024, 1, 1))
    fid_b = compute_finding_id("L1", "balance", datetime(2024, 1, 1))
    assert fid_a == fid_b


def test_finding_id_changes_with_each_key_component():
    base = compute_finding_id("L1", "balance", datetime(2024, 1, 1))
    diff_entity = compute_finding_id("L2", "balance", datetime(2024, 1, 1))
    diff_field = compute_finding_id("L1", "status", datetime(2024, 1, 1))
    diff_date = compute_finding_id("L1", "balance", datetime(2024, 1, 2))
    assert base != diff_entity
    assert base != diff_field
    assert base != diff_date


def test_finding_id_excludes_detector_source():
    """Per spec §6, dedup must collapse across detectors — so detector_source
    is NOT in the key. compute_finding_id only takes the three dedup-key
    components."""
    fid = compute_finding_id("L1", "balance", datetime(2024, 1, 1))
    issue_a = Issue(
        check_name="stl_residual",
        severity=Severity.WARNING,
        entity_id="L1",
        field_name="balance",
        snapshot_date=datetime(2024, 1, 1),
        detector_source="stl_residual",
        finding_id=fid,
    )
    issue_b = Issue(
        check_name="tabpfn_anomaly",
        severity=Severity.WARNING,
        entity_id="L1",
        field_name="balance",
        snapshot_date=datetime(2024, 1, 1),
        detector_source="tabpfn_anomaly",
        finding_id=fid,
    )
    assert issue_a.finding_id == issue_b.finding_id


def test_finding_id_handles_none_components():
    # Should not crash
    fid = compute_finding_id(None, None, None)
    assert isinstance(fid, str) and len(fid) == 36


def test_finding_id_date_and_datetime_same_day_collide():
    """Code-review P1 #1: snapshot dates from a CSV source (`date`) and from
    a Polars source (`datetime`) for the same logical day must collapse to
    the same finding_id — otherwise the orchestrator double-emits."""
    as_date = compute_finding_id("L1", "balance", date(2024, 1, 1))
    as_datetime_midnight = compute_finding_id(
        "L1", "balance", datetime(2024, 1, 1, 0, 0, 0)
    )
    as_datetime_midafternoon = compute_finding_id(
        "L1", "balance", datetime(2024, 1, 1, 14, 30, 12)
    )
    assert as_date == as_datetime_midnight == as_datetime_midafternoon


def test_issue_backward_compat_default_fields():
    """Existing constructions (without confidence/detector_source/finding_id)
    must still work — every new field has a default."""
    issue = Issue(check_name="x", severity=Severity.WARNING)
    assert issue.confidence == 1.0
    assert issue.detector_source == ""
    assert issue.finding_id == ""


def test_finding_wraps_issue_with_detector_sources_list():
    issue = Issue(
        check_name="multi",
        severity=Severity.ERROR,
        entity_id="L1",
        field_name="balance",
        snapshot_date=datetime(2024, 1, 1),
        finding_id=compute_finding_id("L1", "balance", datetime(2024, 1, 1)),
    )
    finding = Finding(
        issue=issue,
        detector_sources=["stl_residual", "tabpfn_anomaly"],
        detected_at=datetime(2024, 1, 1),
        run_id="run-001",
    )
    assert finding.finding_id == issue.finding_id
    assert finding.detector_sources == ["stl_residual", "tabpfn_anomaly"]


def test_rca_result_round_trip_via_asdict():
    rca = RCAResult(
        finding_id="abc",
        hypothesis="upstream code changed advance_rate",
        evidence=["row 42 differs by 12%", "commit 8a3f"],
        confidence=0.78,
        references=["https://github.com/x/y/commit/8a3f"],
    )
    d = asdict(rca)
    assert d["hypothesis"].startswith("upstream")
    assert d["evidence"] == ["row 42 differs by 12%", "commit 8a3f"]
    assert d["confidence"] == 0.78
