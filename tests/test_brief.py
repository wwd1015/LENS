"""Tests for :func:`lens.brief.render_brief`.

The XSS escape test (:func:`test_xss_escapes_script_in_description`) is the
primary acceptance test for T10 — it proves that LLM-authored hostile content
in descriptions and RCA hypotheses is escaped rather than rendered as live
markup.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

from lens.brief import render_brief
from lens.types import Finding, Issue, RCAResult, Severity, compute_finding_id


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_finding(
    *,
    entity_id: str = "e1",
    field_name: str = "balance",
    snapshot_date: datetime | None = None,
    severity: Severity = Severity.WARNING,
    description: str = "fixture finding",
    detector_source: str = "null_check",
    confidence: float = 0.7,
    detector_sources: list[str] | None = None,
) -> Finding:
    snap = snapshot_date or datetime(2026, 5, 1, tzinfo=UTC)
    fid = compute_finding_id(entity_id, field_name, snap)
    issue = Issue(
        check_name=detector_source.split(":", 1)[0],
        severity=severity,
        entity_id=entity_id,
        field_name=field_name,
        snapshot_date=snap,
        description=description,
        confidence=confidence,
        detector_source=detector_source,
        finding_id=fid,
    )
    return Finding(
        issue=issue,
        detector_sources=detector_sources or [detector_source],
        detected_at=datetime(2026, 5, 11, 9, 0, tzinfo=UTC),
        run_id="test-run",
    )


def _write_findings_file(path: Path, finding_ids: list[str]) -> None:
    """Write a minimal findings.json with just the ids the loader needs."""
    payload = [{"finding_id": fid} for fid in finding_ids]
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. PRIMARY ACCEPTANCE TEST — XSS escape
# ---------------------------------------------------------------------------


def test_xss_escapes_script_in_description(tmp_path: Path) -> None:
    """Hostile content in description + RCA must be escaped, not rendered.

    This is the P0 eng-review requirement: Jinja2's default autoescape is off,
    so if `render_brief` ever forgets to pass `autoescape=select_autoescape`,
    this test catches it.
    """
    hostile_desc = "<script>alert('xss')</script>"
    hostile_rca = "<img src=x onerror=alert(1)>"

    f = _make_finding(description=hostile_desc)
    rca = RCAResult(
        finding_id=f.finding_id,
        hypothesis=hostile_rca,
        evidence=["<svg onload=alert(2)>"],
        confidence=0.9,
        references=["http://example.com/safe-link"],
    )

    out = tmp_path / "brief.html"
    render_brief([f], {f.finding_id: rca}, out)
    html = out.read_text(encoding="utf-8")

    # Escaped forms must appear.
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html
    assert "&lt;svg" in html

    # Raw hostile markup must NOT appear anywhere in the body. We allow
    # `<script>` from our own inline filter JS, so we search only for the
    # specific hostile fragments rather than the bare `<script>` tag.
    assert "<script>alert('xss')</script>" not in html
    assert "<img src=x onerror=alert(1)>" not in html
    assert "<svg onload=alert(2)>" not in html


# ---------------------------------------------------------------------------
# 2. Empty state
# ---------------------------------------------------------------------------


def test_renders_empty_state_when_no_findings(tmp_path: Path) -> None:
    out = tmp_path / "brief.html"
    render_brief([], {}, out)
    html = out.read_text(encoding="utf-8")
    assert "All quiet" in html


# ---------------------------------------------------------------------------
# 3. Grouping reduces visible card count
# ---------------------------------------------------------------------------


def test_groups_reduce_card_count(tmp_path: Path) -> None:
    """30 findings across 3 detectors x 3 fields → at most 10 groups."""
    detectors = ["null_check", "stale_data", "cross_source_wiki:rule_a"]
    fields = ["balance", "advance_rate", "status"]

    findings: list[Finding] = []
    for i in range(30):
        det = detectors[i % len(detectors)]
        field = fields[(i // 3) % len(fields)]
        findings.append(
            _make_finding(
                entity_id=f"e{i}",
                field_name=field,
                detector_source=det,
                detector_sources=[det],
                # Slight confidence variation so sort order is meaningful.
                confidence=0.5 + (i % 5) * 0.05,
            )
        )

    out = tmp_path / "brief.html"
    render_brief(findings, {}, out)
    html = out.read_text(encoding="utf-8")

    group_headings = re.findall(r'class="group-heading"', html)
    assert 1 <= len(group_headings) <= 10, (
        f"expected 1-10 groups, got {len(group_headings)}"
    )


# ---------------------------------------------------------------------------
# 4. "What changed" header vs. prior run
# ---------------------------------------------------------------------------


def test_what_changed_header_renders_with_prior(tmp_path: Path) -> None:
    """Prior run has F1+F2; current has F2+F3 → +1 new, -1 resolved, =1 ongoing."""
    f1 = _make_finding(entity_id="e1", field_name="balance")
    f2 = _make_finding(entity_id="e2", field_name="balance")
    f3 = _make_finding(entity_id="e3", field_name="balance")

    prior_path = tmp_path / "findings.prior.json"
    _write_findings_file(prior_path, [f1.finding_id, f2.finding_id])

    out = tmp_path / "brief.html"
    render_brief([f2, f3], {}, out, prior_findings_path=prior_path)
    html = out.read_text(encoding="utf-8")

    assert "+1 new" in html
    assert "-1 resolved" in html
    assert "=1 ongoing" in html


# ---------------------------------------------------------------------------
# 5. Cap-applied warning
# ---------------------------------------------------------------------------


def test_cap_applied_warning_when_findings_exceed_max(tmp_path: Path) -> None:
    findings = [_make_finding(entity_id=f"e{i}") for i in range(501)]
    out = tmp_path / "brief.html"
    render_brief(findings, {}, out, max_findings=500)
    html = out.read_text(encoding="utf-8")
    assert "showing top 500 of 501" in html


# ---------------------------------------------------------------------------
# 6. CSP meta tag present
# ---------------------------------------------------------------------------


def test_csp_meta_tag_present(tmp_path: Path) -> None:
    out = tmp_path / "brief.html"
    render_brief([_make_finding()], {}, out)
    html = out.read_text(encoding="utf-8")
    assert '<meta http-equiv="Content-Security-Policy"' in html


# ---------------------------------------------------------------------------
# 7. Severity ordering — critical group precedes error group
# ---------------------------------------------------------------------------


def test_severity_ordering_in_render(tmp_path: Path) -> None:
    """Pass findings in mixed order; CRITICAL group must render before ERROR."""
    f_info = _make_finding(
        entity_id="e_info", field_name="f_info", severity=Severity.INFO,
        detector_source="det_a",
    )
    f_warning = _make_finding(
        entity_id="e_warn", field_name="f_warn", severity=Severity.WARNING,
        detector_source="det_b",
    )
    f_critical = _make_finding(
        entity_id="e_crit", field_name="f_crit", severity=Severity.CRITICAL,
        detector_source="det_c",
    )
    f_error = _make_finding(
        entity_id="e_err", field_name="f_err", severity=Severity.ERROR,
        detector_source="det_d",
    )

    # Intentionally mixed input order.
    out = tmp_path / "brief.html"
    render_brief([f_info, f_warning, f_critical, f_error], {}, out)
    html = out.read_text(encoding="utf-8")

    # The first severity badge in the rendered card stream must be CRITICAL.
    badges = re.findall(r'badge badge-(critical|error|warning|info)">', html)
    assert badges, "expected at least one severity badge in output"
    assert badges[0] == "critical", f"first badge was {badges[0]!r}, expected critical"

    # And critical should precede error in the rendered HTML.
    idx_critical = html.find('badge badge-critical">')
    idx_error = html.find('badge badge-error">')
    assert idx_critical != -1 and idx_error != -1
    assert idx_critical < idx_error


# ---------------------------------------------------------------------------
# 8. Atomic write — no .tmp left behind
# ---------------------------------------------------------------------------


def test_atomic_write_via_tmp(tmp_path: Path) -> None:
    out = tmp_path / "brief.html"
    render_brief([_make_finding()], {}, out)

    # The output file exists ...
    assert out.exists()
    # ... and no .tmp sibling remains in the directory.
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == [], f"expected no .tmp leftovers, found {leftovers}"
