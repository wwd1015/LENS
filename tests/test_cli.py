"""Tests for the `lens` CLI (lens.cli)."""

from __future__ import annotations

import json

import pytest

from lens.cli import main


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A minimal runnable project: data + run config, RCA disabled (no LLM)."""
    (tmp_path / "data.csv").write_text(
        "entity_id,snapshot_date,status\n"
        "a,2026-01-01,ok\n"
        "b,2026-01-01,\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "lens-run.yaml"
    cfg.write_text(
        """
        sources: {loans: data.csv}
        output_dir: out
        checks:
          - name: null_check
            params: {fields: [status]}
        rca: {enabled: false}
        brief: {dataset_label: "cli test"}
        """,
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_lens_run(project, capsys):
    rc = main(["run", "lens-run.yaml", "--run-id", "cli1"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "LENS Brief" in captured.out  # markdown digest on stdout
    assert "1 findings" in captured.err
    assert (project / "out" / "findings.cli1.json").exists()
    assert (project / "out" / "brief.cli1.html").exists()
    assert (project / "out" / "brief.latest.html").exists()


def test_lens_run_missing_config(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["run", "absent.yaml"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_lens_feedback_delegates(project, capsys):
    rc = main(
        [
            "feedback",
            "fid-9",
            "real",
            "--output",
            "out/feedback.jsonl",
            "--entity",
            "LN-9",
            "--field",
            "balance",
        ]
    )
    assert rc == 0
    entry = json.loads((project / "out" / "feedback.jsonl").read_text().strip())
    assert entry["finding_id"] == "fid-9"
    assert entry["entity_id"] == "LN-9"


def test_lens_brief_delegates(project, capsys):
    assert main(["run", "lens-run.yaml", "--run-id", "cli2"]) == 0
    capsys.readouterr()
    rc = main(["brief", "out/findings.cli2.json", "--top", "3"])
    assert rc == 0
    assert "LENS Brief" in capsys.readouterr().out
