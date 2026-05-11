"""Tests for :mod:`lens.brief.markdown` — top-5 markdown digest renderer."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from lens.brief.markdown import main, render_brief_summary
from lens.types import Finding, Issue, RCAResult, Severity, compute_finding_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_finding(
    *,
    entity_id: str = "e1",
    field_name: str = "balance",
    snapshot_date: datetime | date | None = None,
    severity: Severity = Severity.WARNING,
    confidence: float = 0.9,
    description: str = "",
    detector_source: str = "test_detector",
    finding_id: str | None = None,
) -> Finding:
    """Build a :class:`Finding` with sensible defaults for these tests.

    Always passes a real ``finding_id`` so the order-stable tie-breaker test
    can rely on lexicographic ordering of UUIDs (computed from the input
    coordinates).
    """
    if snapshot_date is None:
        snapshot_date = date(2026, 1, 15)
    fid = finding_id if finding_id is not None else compute_finding_id(
        entity_id, field_name, snapshot_date
    )
    issue = Issue(
        check_name=detector_source,
        severity=severity,
        entity_id=entity_id,
        field_name=field_name,
        snapshot_date=snapshot_date,
        description=description,
        confidence=confidence,
        detector_source=detector_source,
        finding_id=fid,
    )
    return Finding(
        issue=issue,
        detector_sources=[detector_source],
        detected_at=datetime(2026, 1, 15, 9, 0, 0),
        run_id="run_test",
    )


def make_rca(
    finding_id: str,
    *,
    hypothesis: str = "Hypothesis text.",
    references: list[str] | None = None,
    confidence: float = 0.8,
) -> RCAResult:
    return RCAResult(
        finding_id=finding_id,
        hypothesis=hypothesis,
        evidence=["evidence one", "evidence two"],
        confidence=confidence,
        references=list(references or []),
    )


# ---------------------------------------------------------------------------
# render_brief_summary tests
# ---------------------------------------------------------------------------


def test_renders_header_and_summary_line() -> None:
    """Header line + summary count appear; dataset label propagates."""
    findings = [make_finding(entity_id=f"e{i}") for i in range(5)]
    out = render_brief_summary(
        findings, rcas=None, top_n=5, dataset_label="Q1 lending", date_iso="2026-05-10"
    )
    assert "## LENS Brief — Q1 lending — 2026-05-10" in out
    assert "5 total findings; top 5 shown." in out


def test_top_5_selected_by_severity_then_confidence() -> None:
    """7 findings with mixed severity + confidence → top-5 in correct order."""
    findings = [
        # Two CRITICAL, different confidences — CRITICAL/0.95 should be #1.
        make_finding(entity_id="critA", severity=Severity.CRITICAL, confidence=0.90),
        make_finding(entity_id="critB", severity=Severity.CRITICAL, confidence=0.95),
        # One ERROR
        make_finding(entity_id="errA", severity=Severity.ERROR, confidence=0.80),
        # Two WARNING, different confidences
        make_finding(entity_id="warnA", severity=Severity.WARNING, confidence=0.70),
        make_finding(entity_id="warnB", severity=Severity.WARNING, confidence=0.85),
        # Two INFO — these should fall off the bottom (top 5).
        make_finding(entity_id="infoA", severity=Severity.INFO, confidence=0.99),
        make_finding(entity_id="infoB", severity=Severity.INFO, confidence=0.30),
    ]
    out = render_brief_summary(findings, rcas=None, top_n=5, dataset_label="t")

    # Both INFO entities must be absent from the top-5 digest.
    assert "infoA" not in out
    assert "infoB" not in out

    # The remaining 5 should appear in this exact order:
    # critB (CRIT 0.95) > critA (CRIT 0.90) > errA (ERR 0.80)
    # > warnB (WARN 0.85) > warnA (WARN 0.70)
    expected_order = ["critB", "critA", "errA", "warnB", "warnA"]
    positions = [out.index(e) for e in expected_order]
    assert positions == sorted(positions), (
        f"top-5 ordering wrong: {expected_order} positions were {positions}"
    )


def test_no_rcas_uses_placeholder_text() -> None:
    """When rcas=None, every finding line carries the (no RCA yet) placeholder."""
    findings = [make_finding(entity_id=f"e{i}") for i in range(3)]
    out = render_brief_summary(findings, rcas=None, top_n=5, dataset_label="t")
    # Exactly N occurrences — one per finding line.
    assert out.count("(no RCA yet)") == 3


def test_rca_hypothesis_truncated_to_200_chars() -> None:
    """A 300-char hypothesis renders as the first 200 chars + ellipsis."""
    long_text = "x" * 300
    finding = make_finding(entity_id="ent1")
    rcas = {finding.finding_id: make_rca(finding.finding_id, hypothesis=long_text)}
    out = render_brief_summary([finding], rcas=rcas, top_n=5, dataset_label="t")

    # The first 200 chars must appear; the full 300 must NOT.
    assert ("x" * 200) in out
    assert ("x" * 201) not in out
    # The truncation marker is the literal horizontal ellipsis.
    assert "…" in out


def test_first_http_reference_appears_as_link() -> None:
    """First http(s) reference is the link; non-http and later URLs are not."""
    finding = make_finding(entity_id="ent1")
    refs = [
        "row 42 differs",
        "https://github.com/x/y/commit/abc123",
        "https://other.example",
    ]
    rcas = {
        finding.finding_id: make_rca(
            finding.finding_id, hypothesis="hyp", references=refs
        )
    }
    out = render_brief_summary([finding], rcas=rcas, top_n=5, dataset_label="t")
    assert "https://github.com/x/y/commit/abc123" in out
    assert "https://other.example" not in out


def test_deterministic_tie_break() -> None:
    """Identical severity + confidence → ordered by finding_id ascending."""
    # Two findings with same severity/confidence but explicit finding_ids
    # that we control directly so we can predict the tie-break order.
    f_a = make_finding(
        entity_id="alpha",
        severity=Severity.WARNING,
        confidence=0.5,
        finding_id="aaa",
    )
    f_b = make_finding(
        entity_id="beta",
        severity=Severity.WARNING,
        confidence=0.5,
        finding_id="bbb",
    )

    out_ab = render_brief_summary([f_a, f_b], rcas=None, top_n=5, dataset_label="t")
    out_ba = render_brief_summary([f_b, f_a], rcas=None, top_n=5, dataset_label="t")
    # Same output regardless of input order.
    assert out_ab == out_ba
    # And alpha (finding_id="aaa") appears before beta (finding_id="bbb").
    assert out_ab.index("alpha") < out_ab.index("beta")


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _write_findings_json(path: Path, n: int) -> list[Finding]:
    """Build N synthetic findings, serialize them to ``path``, and return them.

    We hand-build the JSON shape that ``_finding_to_jsonable`` produces so this
    test does not depend on importing the orchestrator (keeps the test fast and
    independent of the heavier pipeline).
    """
    findings = []
    records = []
    for i in range(n):
        f = make_finding(
            entity_id=f"e{i}",
            severity=Severity.CRITICAL if i == 0 else Severity.WARNING,
            confidence=0.9 - 0.05 * i,
        )
        findings.append(f)
        records.append(
            {
                "finding_id": f.finding_id,
                "issue": {
                    "check_name": f.issue.check_name,
                    "severity": f.issue.severity.value,
                    "entity_id": f.issue.entity_id,
                    "field_name": f.issue.field_name,
                    "snapshot_date": f.issue.snapshot_date.isoformat(),
                    "description": f.issue.description,
                    "details": f.issue.details,
                    "confidence": f.issue.confidence,
                    "detector_source": f.issue.detector_source,
                    "finding_id": f.issue.finding_id,
                },
                "detector_sources": list(f.detector_sources),
                "detected_at": "2026-01-15T09:00:00+00:00",
                "run_id": f.run_id,
            }
        )
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return findings


def test_main_cli_renders_from_findings_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI reads findings.json, prints header + exactly --top finding lines."""
    findings_path = tmp_path / "findings.run1.json"
    _write_findings_json(findings_path, n=6)

    rc = main([str(findings_path), "--top", "3", "--dataset", "demo"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "## LENS Brief — demo —" in captured

    # Count the enumerated finding lines: ``1.``, ``2.``, ``3.`` at line start.
    # No ``4.`` should appear because --top 3.
    numbered_lines = [
        line
        for line in captured.splitlines()
        if line.startswith(("1. ", "2. ", "3. ", "4. "))
    ]
    assert len(numbered_lines) == 3
    assert any(line.startswith("1. ") for line in numbered_lines)
    assert any(line.startswith("3. ") for line in numbered_lines)
    assert not any(line.startswith("4. ") for line in numbered_lines)


def test_main_cli_missing_file_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-existent findings path → exit code 2 (graceful, not a traceback)."""
    bogus = tmp_path / "does_not_exist.json"
    rc = main([str(bogus)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_main_cli_subprocess_smoke(tmp_path: Path) -> None:
    """End-to-end via subprocess — verifies the ``python -m`` invocation works.

    The render-from-stdin path is already covered by the in-process CLI test;
    this one guards against import-time regressions in the ``__main__`` block.
    """
    findings_path = tmp_path / "findings.run2.json"
    _write_findings_json(findings_path, n=2)

    result = subprocess.run(
        [sys.executable, "-m", "lens.brief.markdown", str(findings_path), "--top", "2"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "## LENS Brief" in result.stdout
    assert "2 total findings; top 2 shown." in result.stdout
