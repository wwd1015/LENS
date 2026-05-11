"""Tests for ``lens.brief.feedback``."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lens.brief.feedback import (
    FeedbackLabel,
    append_feedback,
    format_button_url,
    main,
)


def _read_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def test_append_feedback_writes_valid_jsonl(tmp_path: Path) -> None:
    out = tmp_path / "feedback.jsonl"
    expected = [
        ("finding-a", FeedbackLabel.REAL),
        ("finding-b", FeedbackLabel.FALSE_POSITIVE),
        ("finding-c", FeedbackLabel.NEEDS_MORE),
    ]
    for fid, lbl in expected:
        entry = append_feedback(fid, lbl, out, analyst="alice", note="ok")
        assert entry["finding_id"] == fid
        assert entry["label"] == lbl.value

    lines = _read_lines(out)
    assert len(lines) == 3
    for line, (fid, lbl) in zip(lines, expected):
        parsed = json.loads(line)
        assert parsed["finding_id"] == fid
        assert parsed["label"] == lbl.value
        assert parsed["analyst"] == "alice"
        assert parsed["note"] == "ok"
        # ts should be ISO 8601 parseable
        datetime.fromisoformat(parsed["ts"])


def test_append_feedback_accepts_string_label(tmp_path: Path) -> None:
    out = tmp_path / "feedback.jsonl"
    entry = append_feedback("f1", "real", out)
    assert entry["label"] == "real"
    assert json.loads(_read_lines(out)[0])["label"] == "real"


def test_append_feedback_uses_explicit_ts(tmp_path: Path) -> None:
    out = tmp_path / "feedback.jsonl"
    fixed = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    entry = append_feedback("f1", FeedbackLabel.REAL, out, ts=fixed)
    assert entry["ts"] == fixed.isoformat()


def test_unknown_label_raises_value_error(tmp_path: Path) -> None:
    out = tmp_path / "feedback.jsonl"
    with pytest.raises(ValueError, match="Unknown feedback label"):
        append_feedback("f1", "garbage", out)
    # file must not have been created with a partial/invalid entry
    assert not out.exists() or _read_lines(out) == []


def test_concurrent_appends_dont_corrupt(tmp_path: Path) -> None:
    out = tmp_path / "feedback.jsonl"
    n = 10

    def _write(i: int) -> dict:
        return append_feedback(
            f"finding-{i}",
            FeedbackLabel.REAL,
            out,
            analyst=f"analyst-{i}",
            note=f"note-{i}",
        )

    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = [ex.submit(_write, i) for i in range(n)]
        for fut in as_completed(futures):
            fut.result()

    lines = _read_lines(out)
    assert len(lines) == n, f"expected {n} lines, got {len(lines)}"

    parsed = [json.loads(ln) for ln in lines]
    finding_ids = {p["finding_id"] for p in parsed}
    assert finding_ids == {f"finding-{i}" for i in range(n)}
    # All entries had the same label
    assert {p["label"] for p in parsed} == {"real"}


def test_main_cli_writes_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "feedback.jsonl"
    rc = main(
        [
            "finding-id-x",
            "real",
            "--output",
            str(out),
            "--note",
            "looks legit",
            "--analyst",
            "alice@example.com",
        ]
    )
    assert rc == 0

    lines = _read_lines(out)
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["finding_id"] == "finding-id-x"
    assert parsed["label"] == "real"
    assert parsed["note"] == "looks legit"
    assert parsed["analyst"] == "alice@example.com"

    captured = capsys.readouterr()
    stdout_parsed = json.loads(captured.out.strip())
    assert stdout_parsed == parsed


def test_main_cli_unknown_label_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "feedback.jsonl"
    rc = main(["finding-id-x", "garbage", "--output", str(out)])
    assert rc == 2

    captured = capsys.readouterr()
    assert "Unknown feedback label" in captured.err
    # no file written
    assert not out.exists() or _read_lines(out) == []


def test_main_cli_defaults_to_cwd_feedback_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = main(["finding-id-y", "needs_more"])
    assert rc == 0
    out = tmp_path / "feedback.jsonl"
    assert out.exists()
    parsed = json.loads(_read_lines(out)[0])
    assert parsed["finding_id"] == "finding-id-y"
    assert parsed["label"] == "needs_more"


def test_format_button_url_encodes_payload() -> None:
    url = format_button_url("abc-123", FeedbackLabel.FALSE_POSITIVE)
    assert url.startswith("mailto:")
    assert "subject=" in url
    assert "body=" in url
    # the JSON payload's keys should be URL-encoded but recoverable
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    body = json.loads(qs["body"][0])
    assert body == {"finding_id": "abc-123", "label": "false_positive"}
    assert "false_positive" in qs["subject"][0]


def test_format_button_url_accepts_string_label() -> None:
    url = format_button_url("abc-123", "real")
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(url).query)
    assert json.loads(qs["body"][0])["label"] == "real"


def test_format_button_url_custom_recipient() -> None:
    url = format_button_url("abc-123", FeedbackLabel.REAL, recipient="ops@bank.com")
    assert url.startswith("mailto:ops@bank.com")
