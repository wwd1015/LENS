"""Tests for the plain-language layer of the HTML brief."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from lens.brief.html import (
    _classify_references,
    _detector_friendly,
    _finding_view,
    _humanize_date,
    _humanize_field,
    _linkify_segments,
    render_brief,
)
from lens.types import Finding, Issue, RCAResult, Severity


def _finding(
    *,
    entity="D2",
    field="balance",
    detectors=("stl_residual", "cross_source_wiki:rule_a"),
    severity=Severity.CRITICAL,
    snapshot=datetime(2026, 6, 30),
    description="",
):
    issue = Issue(
        check_name=detectors[0].split(":")[0],
        severity=severity,
        entity_id=entity,
        field_name=field,
        snapshot_date=snapshot,
        confidence=0.99,
        detector_source=detectors[0],
        description=description,
        finding_id="fid-1",
    )
    return Finding(issue=issue, detector_sources=list(detectors), run_id="r")


def test_humanize_field():
    assert _humanize_field("advance_rate") == "Advance rate"
    assert _humanize_field("balance") == "Balance"
    assert _humanize_field("") == ""


def test_humanize_date():
    assert _humanize_date(datetime(2026, 6, 30)) == "Jun 30, 2026"
    assert _humanize_date("2026-06-30") == "Jun 30, 2026"
    assert _humanize_date(None) == ""
    assert _humanize_date("not-a-date") == "not-a-date"


def test_detector_friendly_known_and_unknown():
    title, sub = _detector_friendly("cross_source_wiki")
    assert "don't agree" in title.lower()
    assert sub
    # Unknown family falls back, never raises.
    gtitle, gsub = _detector_friendly("some_new_detector")
    assert gtitle and gsub


def test_linkify_segments_splits_urls():
    segs = _linkify_segments("see https://example.com/commit/abc then stop")
    urls = [s["url"] for s in segs if "url" in s]
    texts = [s["text"] for s in segs if "text" in s]
    assert urls == ["https://example.com/commit/abc"]
    assert "see " in texts[0]
    # Plain text with no URL yields a single text segment.
    assert _linkify_segments("no link here") == [{"text": "no link here"}]


def test_classify_references_links_vs_context():
    links, context = _classify_references(
        [
            "https://github.com/x/y/commit/deadbeef",
            "wiki rule: senior-debt-equals-pool-x-advance-rate",
            "lineage layer: loan_pool",
        ]
    )
    assert len(links) == 1
    assert "pipeline" in links[0]["label"]
    assert context == [
        "wiki rule: senior-debt-equals-pool-x-advance-rate",
        "lineage layer: loan_pool",
    ]


def test_finding_view_is_plain_language():
    rca = RCAResult(
        finding_id="fid-1",
        hypothesis="The advance rate was applied as 0.84 instead of 0.75.",
        evidence=["lhs 2,440,200 vs rhs 2,178,750"],
        confidence=0.6,
        references=["https://github.com/x/y/commit/deadbeef", "wiki rule: rule_a"],
    )
    # Lead detector is the cross-source rule → reconciliation headline.
    view = _finding_view(
        _finding(detectors=("cross_source_wiki:rule_a", "stl_residual")),
        {"fid-1": rca},
    )

    # Plain headline + meaning, no raw detector codes leaking into title.
    assert "don't agree" in view["title"].lower()
    assert view["severity_meaning"] == "Needs urgent attention"
    assert view["field_human"] == "Balance"
    assert view["date_human"] == "Jun 30, 2026"
    assert view["confidence_pct"] == 99
    # Two families → agreement signal available to the template.
    assert view["check_count"] == 2
    # Cause is segmented; the commit link is a button, the wiki ref a context chip.
    assert view["rca"]["cause_segments"][0]["text"].startswith("The advance rate")
    assert "pipeline" in view["rca"]["links"][0]["label"]
    assert view["rca"]["context_refs"] == ["wiki rule: rule_a"]


def test_brief_omits_raw_detector_codes_from_prose(tmp_path: Path):
    """The reader-facing copy shouldn't surface internal detector identifiers."""
    rca = RCAResult(
        finding_id="fid-1",
        hypothesis="Something went wrong upstream.",
        evidence=[],
        confidence=0.5,
        references=[],
    )
    out = tmp_path / "brief.html"
    render_brief([_finding()], {"fid-1": rca}, out, dataset_label="Demo")
    html = out.read_text(encoding="utf-8")

    # Friendly title present (lead detector is stl_residual by default order);
    # the raw detector code is only in a data- attribute, never visible prose.
    assert "match its recent trend" in html  # apostrophe is HTML-escaped
    assert ">stl_residual," not in html  # old jargon chip removed
    assert 'data-detectors="stl_residual,cross_source_wiki:rule_a"' in html  # backend intact
    assert "Is this a real problem?" in html  # feedback purpose stated


def test_inline_url_not_duplicated_as_button(tmp_path: Path):
    url = "https://github.com/x/y/commit/abc123"
    rca = RCAResult(
        finding_id="fid-1",
        hypothesis=f"Caused by a commit: {url}",
        evidence=[],
        confidence=0.5,
        references=[url],  # same URL also in references
    )
    view = _finding_view(_finding(), {"fid-1": rca})
    # Shown inline in the cause; suppressed from the button list to avoid repeat.
    assert any(s.get("url") == url for s in view["rca"]["cause_segments"])
    assert view["rca"]["links"] == []
