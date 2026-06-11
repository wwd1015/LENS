"""Tests for the batch run (lens.batch) — detect → group RCA → brief."""

from __future__ import annotations

import os

from lens.batch import run_batch
from lens.run_config import load_run_config
from lens.types import Severity

RCA_RESPONSE = """\
```json
{
  "hypothesis": "The upstream loader shipped nulls for the whole portfolio.",
  "evidence": ["status is null for entities a and c on 2026-01-02"],
  "confidence": 0.7,
  "references": []
}
```
"""


class StubLLM:
    """Deterministic LLMClient stub; records every prompt."""

    def __init__(self, response: str = RCA_RESPONSE) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _write_config(tmp_path, *, rca_floor="error", rca_enabled=True, feedback=False):
    (tmp_path / "data.csv").write_text(
        "entity_id,snapshot_date,status,balance\n"
        "a,2026-01-01,ok,100\n"
        "b,2026-01-01,ok,200\n"
        "a,2026-01-02,,110\n"
        "c,2026-01-02,,300\n",
        encoding="utf-8",
    )
    fb_block = "feedback:\n          path: feedback.jsonl" if feedback else ""
    cfg_path = tmp_path / "lens-run.yaml"
    cfg_path.write_text(
        f"""
        sources:
          loans: data.csv
        output_dir: out
        checks:
          - name: null_check
            params: {{fields: [status]}}
        rca:
          enabled: {str(rca_enabled).lower()}
          severity_floor: {rca_floor}
          repo_root: .
        {fb_block}
        brief:
          dataset_label: "test"
        """,
        encoding="utf-8",
    )
    return load_run_config(cfg_path)


def test_batch_end_to_end_with_group_rca(tmp_path):
    cfg = _write_config(tmp_path)
    stub = StubLLM()

    result = run_batch(cfg, run_id="batch1", llm_client=stub)

    # Two null findings → one Finding Group (null_check, status) → ONE RCA.
    assert len(result.findings) == 2
    assert result.rca_groups_investigated == 1
    assert len(stub.prompts) == 1

    # Group context made it into the prompt (ADR 0003).
    assert "group_size: 2 findings" in stub.prompts[0]
    assert "group_entities" in stub.prompts[0]

    # The shared hypothesis is attached to EVERY member finding.
    assert set(result.rcas) == {f.finding_id for f in result.findings}
    hypotheses = {r.hypothesis for r in result.rcas.values()}
    assert hypotheses == {"The upstream loader shipped nulls for the whole portfolio."}

    # Artifacts on disk.
    assert result.findings_path.exists()
    assert result.brief_html_path == cfg.output_dir / "brief.batch1.html"
    assert result.brief_html_path.exists()
    latest = cfg.output_dir / "brief.latest.html"
    assert latest.is_symlink()
    assert os.readlink(latest) == "brief.batch1.html"

    # One persisted RCA JSON, keyed by the representative finding.
    rca_files = list((cfg.output_dir / "rca" / "batch1").glob("*.json"))
    assert len(rca_files) == 1

    # Digest mentions the hypothesis.
    assert "upstream loader" in result.markdown_digest


def test_batch_floor_skips_groups(tmp_path):
    cfg = _write_config(tmp_path, rca_floor="critical")
    stub = StubLLM()

    result = run_batch(cfg, run_id="floored", llm_client=stub)

    assert result.rca_groups_investigated == 0
    assert result.rca_groups_skipped_below_floor == 1
    assert stub.prompts == []
    assert result.rcas == {}


def test_batch_rca_disabled(tmp_path):
    cfg = _write_config(tmp_path, rca_enabled=False)
    stub = StubLLM()
    result = run_batch(cfg, run_id="norca", llm_client=stub)
    assert stub.prompts == []
    assert result.brief_html_path.exists()


def test_batch_rca_failure_does_not_kill_brief(tmp_path):
    cfg = _write_config(tmp_path)

    class ExplodingLLM:
        def complete(self, prompt: str) -> str:
            raise RuntimeError("boom")

    result = run_batch(cfg, run_id="boom", llm_client=ExplodingLLM())
    # RCAAgent catches LLM errors and returns a low-confidence RCA, so the
    # group still counts as investigated — and the brief still renders.
    assert result.brief_html_path.exists()
    assert all(r.confidence == 0.0 for r in result.rcas.values())


def test_batch_suppressed_findings_get_no_rca(tmp_path):
    cfg = _write_config(tmp_path, feedback=True)
    # FP both entities' (entity, field) pairs so the whole group is suppressed.
    from lens.brief.feedback import append_feedback

    for entity in ("a", "c"):
        append_feedback(
            "whatever",
            "false_positive",
            cfg.feedback.path,
            entity_id=entity,
            field_name="status",
            detector_sources=["null_check"],
        )
    stub = StubLLM()

    result = run_batch(cfg, run_id="suppressed", llm_client=stub)

    assert stub.prompts == []  # suppressed findings never trigger RCA
    assert all(
        f.issue.severity is Severity.INFO for f in result.findings
    )
    # Brief renders the suppressed section.
    html = result.brief_html_path.read_text(encoding="utf-8")
    assert "suppressed by analyst feedback" in html


def test_batch_delta_uses_pre_run_latest(tmp_path):
    cfg = _write_config(tmp_path)
    stub = StubLLM()
    run_batch(cfg, run_id="first", llm_client=stub)
    result2 = run_batch(cfg, run_id="second", llm_client=stub)
    html = result2.brief_html_path.read_text(encoding="utf-8")
    # Same data twice → all findings "ongoing" vs prior run.
    assert "=2 ongoing" in html


def test_empty_run_still_writes_brief(tmp_path):
    (tmp_path / "data.csv").write_text(
        "entity_id,snapshot_date,status\na,2026-01-01,ok\n", encoding="utf-8"
    )
    cfg_path = tmp_path / "lens-run.yaml"
    cfg_path.write_text(
        """
        sources: {loans: data.csv}
        output_dir: out
        checks:
          - name: null_check
            params: {fields: [status]}
        """,
        encoding="utf-8",
    )
    cfg = load_run_config(cfg_path)
    result = run_batch(cfg, run_id="quiet", llm_client=StubLLM())
    assert result.findings == []
    assert result.run_id == "quiet"
    html = result.brief_html_path.read_text(encoding="utf-8")
    assert "All quiet" in html


def test_empty_run_autogenerated_id_recovered(tmp_path):
    (tmp_path / "data.csv").write_text(
        "entity_id,snapshot_date,status\na,2026-01-01,ok\n", encoding="utf-8"
    )
    cfg_path = tmp_path / "lens-run.yaml"
    cfg_path.write_text(
        """
        sources: {loans: data.csv}
        output_dir: out
        checks:
          - name: null_check
            params: {fields: [status]}
        """,
        encoding="utf-8",
    )
    cfg = load_run_config(cfg_path)
    result = run_batch(cfg, llm_client=StubLLM())
    assert result.run_id not in ("", "unknown")
    assert (cfg.output_dir / f"findings.{result.run_id}.json").exists()
