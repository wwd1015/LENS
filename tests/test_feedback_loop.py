"""Tests for the feedback consumer (lens.feedback_loop) — ADR 0001."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from lens.feedback_loop import apply_feedback, is_suppressed, load_entries
from lens.types import Finding, Issue, Severity, compute_finding_id

NOW = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)


def _finding(
    entity="LN-1",
    field="balance",
    severity=Severity.ERROR,
    detectors=("stl_residual",),
    snapshot=datetime(2026, 6, 9, tzinfo=UTC),
):
    fid = compute_finding_id(entity, field, snapshot)
    issue = Issue(
        check_name=detectors[0].split(":")[0],
        severity=severity,
        entity_id=entity,
        field_name=field,
        snapshot_date=snapshot,
        confidence=0.9,
        detector_source=detectors[0],
        finding_id=fid,
    )
    return Finding(issue=issue, detector_sources=list(detectors), run_id="r1")


def _write_feedback(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _fp_entry(
    entity="LN-1", field="balance", detectors=("stl_residual",), ts=None, label="false_positive"
):
    return {
        "finding_id": "ignored-when-inline",
        "label": label,
        "ts": (ts or (NOW - timedelta(days=1))).isoformat(),
        "entity_id": entity,
        "field_name": field,
        "detector_sources": list(detectors),
    }


def test_fp_verdict_downgrades_to_info_never_drops(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(fb, [_fp_entry()])
    findings = [_finding()]

    out = apply_feedback(findings, feedback_path=fb, expiry_days=90, now=NOW)

    assert len(out) == 1  # never dropped
    assert out[0].issue.severity is Severity.INFO
    meta = out[0].issue.details["suppressed_by_feedback"]
    assert meta["original_severity"] == "error"
    assert is_suppressed(out[0])


def test_expired_fp_verdict_does_not_suppress(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(fb, [_fp_entry(ts=NOW - timedelta(days=120))])

    out = apply_feedback([_finding()], feedback_path=fb, expiry_days=90, now=NOW)

    assert out[0].issue.severity is Severity.ERROR
    assert not is_suppressed(out[0])
    # The verdict is still displayed as history.
    assert out[0].issue.details["prior_feedback"]["label"] == "false_positive"


def test_fp_verdict_without_ts_does_not_suppress(tmp_path, caplog):
    """A verdict with no parseable ts could never expire — it must not
    suppress (and must be called out at WARNING)."""
    import logging

    entry = _fp_entry()
    entry["ts"] = None
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(fb, [entry])

    with caplog.at_level(logging.WARNING, logger="lens.feedback_loop"):
        out = apply_feedback([_finding()], feedback_path=fb, expiry_days=90, now=NOW)

    assert out[0].issue.severity is Severity.ERROR
    assert not is_suppressed(out[0])
    assert any("unparseable ts" in rec.message for rec in caplog.records)


def test_new_detector_family_breaks_through(tmp_path):
    """A finding co-flagged by a family never judged FP must NOT be suppressed."""
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(fb, [_fp_entry(detectors=["stl_residual"])])
    finding = _finding(detectors=("stl_residual", "cross_source_wiki:rule_a"))

    out = apply_feedback([finding], feedback_path=fb, expiry_days=90, now=NOW)

    assert out[0].issue.severity is Severity.ERROR
    assert not is_suppressed(out[0])


def test_subset_of_fp_families_is_suppressed(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(
        fb, [_fp_entry(detectors=["stl_residual", "tabpfn_anomaly"])]
    )
    finding = _finding(detectors=("tabpfn_anomaly",))

    out = apply_feedback([finding], feedback_path=fb, expiry_days=90, now=NOW)

    assert is_suppressed(out[0])


def test_later_real_verdict_clears_suppression(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(
        fb,
        [
            _fp_entry(ts=NOW - timedelta(days=5)),
            _fp_entry(ts=NOW - timedelta(days=2), label="real"),
        ],
    )

    out = apply_feedback([_finding()], feedback_path=fb, expiry_days=90, now=NOW)

    assert not is_suppressed(out[0])
    assert out[0].issue.details["prior_feedback"]["label"] == "real"


def test_other_entity_field_untouched(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(fb, [_fp_entry(entity="LN-1", field="balance")])
    other = _finding(entity="LN-2", field="balance")

    out = apply_feedback([other], feedback_path=fb, expiry_days=90, now=NOW)

    assert not is_suppressed(out[0])
    assert "prior_feedback" not in (out[0].issue.details or {})


def test_legacy_entry_resolved_from_prior_findings_files(tmp_path):
    """Entries without inline entity/field resolve via findings.*.json."""
    finding = _finding()
    findings_file = tmp_path / "findings.run1.json"
    findings_file.write_text(
        json.dumps(
            [
                {
                    "finding_id": finding.finding_id,
                    "issue": {
                        "entity_id": "LN-1",
                        "field_name": "balance",
                    },
                    "detector_sources": ["stl_residual"],
                }
            ]
        ),
        encoding="utf-8",
    )
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(
        fb,
        [
            {
                "finding_id": finding.finding_id,
                "label": "false_positive",
                "ts": (NOW - timedelta(days=1)).isoformat(),
            }
        ],
    )

    out = apply_feedback(
        [_finding()], feedback_path=fb, output_dir=tmp_path, expiry_days=90, now=NOW
    )

    assert is_suppressed(out[0])


def test_unresolvable_entry_skipped(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(
        fb,
        [{"finding_id": "no-such-id", "label": "false_positive", "ts": NOW.isoformat()}],
    )

    out = apply_feedback(
        [_finding()], feedback_path=fb, output_dir=tmp_path, expiry_days=90, now=NOW
    )

    assert not is_suppressed(out[0])


def test_missing_feedback_file_is_noop(tmp_path):
    findings = [_finding()]
    out = apply_feedback(
        findings, feedback_path=tmp_path / "absent.jsonl", expiry_days=90, now=NOW
    )
    assert out == findings


def test_load_entries_skips_malformed_lines(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    fb.write_text(
        '{"finding_id": "a", "label": "real"}\nnot json\n{"label": "real"}\n',
        encoding="utf-8",
    )
    entries = load_entries(fb)
    assert len(entries) == 1
    assert entries[0]["finding_id"] == "a"


def test_already_info_finding_not_double_suppressed(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(fb, [_fp_entry()])
    finding = _finding(severity=Severity.INFO)

    out = apply_feedback([finding], feedback_path=fb, expiry_days=90, now=NOW)

    assert not is_suppressed(out[0])
    assert out[0].issue.severity is Severity.INFO


@pytest.mark.parametrize("label", ["real", "needs_more"])
def test_non_fp_verdicts_only_annotate(tmp_path, label):
    fb = tmp_path / "feedback.jsonl"
    _write_feedback(fb, [_fp_entry(label=label)])

    out = apply_feedback([_finding()], feedback_path=fb, expiry_days=90, now=NOW)

    assert not is_suppressed(out[0])
    assert out[0].issue.details["prior_feedback"]["label"] == label
