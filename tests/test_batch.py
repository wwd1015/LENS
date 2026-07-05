"""Tests for the batch run (lens.batch) — detect → group RCA → brief."""

from __future__ import annotations

import os

import pytest

from lens.batch import run_batch
from lens.run_config import load_run_config
from lens.types import Severity
from lens.wiki.ingest import CallCost

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


class CostStubLLM:
    """Stub that also reports a per-call cost, the way ClaudeCodeClient does.

    Sets ``last_call`` on each ``complete`` so the RCA agent can attribute the
    call's cost to the result — exercising the cost-tracking path end to end.
    """

    def __init__(self, response: str = RCA_RESPONSE, cost_usd: float = 0.02) -> None:
        self.response = response
        self.prompts: list[str] = []
        self._cost_usd = cost_usd
        self.last_call: CallCost | None = None

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.last_call = CallCost(
            cost_usd=self._cost_usd, input_tokens=1000, output_tokens=200
        )
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


def test_batch_max_investigations_caps_llm_calls(tmp_path):
    # Two fields with nulls → two null_check Finding Groups, both ERROR.
    (tmp_path / "data.csv").write_text(
        "entity_id,snapshot_date,status,note\na,2026-01-01,ok,hi\nb,2026-01-01,,\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "lens-run.yaml"
    cfg_path.write_text(
        """
        sources: {loans: data.csv}
        output_dir: out
        checks:
          - {name: null_check, params: {fields: [status, note]}}
        rca: {severity_floor: error, max_investigations: 1, repo_root: .}
        """,
        encoding="utf-8",
    )
    cfg = load_run_config(cfg_path)
    stub = StubLLM()

    result = run_batch(cfg, run_id="cap", llm_client=stub)

    # Two eligible groups, capped to 1 → exactly one LLM call.
    assert result.rca_groups_investigated == 1
    assert result.rca_groups_skipped_over_cap == 1
    assert len(stub.prompts) == 1


def test_rca_model_routing_escalates_critical(tmp_path):
    from lens.batch import _rca_model_for
    from lens.types import Severity

    cfg = _write_config(tmp_path)
    cfg.rca.model = "sonnet"
    cfg.rca.escalate_model = "opus"
    cfg.rca.escalate_severity = Severity.CRITICAL

    # Bulk severities use the balanced model; CRITICAL escalates.
    assert _rca_model_for(cfg, Severity.ERROR) == "sonnet"
    assert _rca_model_for(cfg, Severity.WARNING) == "sonnet"
    assert _rca_model_for(cfg, Severity.CRITICAL) == "opus"

    # Escalation disabled → everything on the bulk model.
    cfg.rca.escalate_model = None
    assert _rca_model_for(cfg, Severity.CRITICAL) == "sonnet"


def test_client_for_model_builds_pinned_client_and_caches(tmp_path):
    from lens.batch import _client_for_model
    from lens.wiki.ingest import ClaudeCodeClient

    cache: dict = {}
    # No injected client + a model → ClaudeCodeClient pinned to it.
    c1 = _client_for_model("sonnet", None, cache)
    assert isinstance(c1, ClaudeCodeClient)
    assert c1.extra_args == ["--model", "sonnet"]
    # Cached: same model returns the same instance.
    assert _client_for_model("sonnet", None, cache) is c1
    # None model → session default (no pinned client).
    assert _client_for_model(None, None, cache) is None
    # Injected client always wins.
    stub = StubLLM()
    assert _client_for_model("sonnet", stub, cache) is stub


def test_batch_reuses_prior_rca_for_ongoing_findings(tmp_path):
    cfg = _write_config(tmp_path)  # reuse_prior_rca defaults True
    stub1 = StubLLM()
    first = run_batch(cfg, run_id="r1", llm_client=stub1)
    assert first.rca_groups_investigated == 1
    assert first.rca_groups_reused == 0
    assert len(stub1.prompts) == 1

    # Same data → same finding_id → second run reuses, no new LLM call.
    stub2 = StubLLM()
    second = run_batch(cfg, run_id="r2", llm_client=stub2)
    assert second.rca_groups_investigated == 0
    assert second.rca_groups_reused == 1
    assert stub2.prompts == []  # zero LLM calls
    # The reused hypothesis is still attached to the findings + persisted.
    assert second.rcas
    assert (cfg.output_dir / "rca" / "r2").exists()


def test_batch_reuses_prior_rca_keyed_on_any_member(tmp_path):
    """Representative churn must not defeat reuse: a prior RCA keyed by a
    NON-representative member of the group still counts as ongoing."""
    import json

    cfg = _write_config(tmp_path)
    first = run_batch(cfg, run_id="m1", llm_client=StubLLM())
    assert first.rca_groups_investigated == 1

    # The persisted RCA is keyed by the representative's finding_id. Re-key
    # it to the OTHER group member to simulate the rep changing between runs.
    member_ids = sorted(f.finding_id for f in first.findings)
    rca_dir = cfg.output_dir / "rca" / "m1"
    (old_file,) = rca_dir.glob("*.json")
    payload = json.loads(old_file.read_text(encoding="utf-8"))
    other_id = member_ids[0] if payload["finding_id"] != member_ids[0] else member_ids[1]
    payload["finding_id"] = other_id
    old_file.unlink()
    (rca_dir / f"{other_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    stub2 = StubLLM()
    second = run_batch(cfg, run_id="m2", llm_client=stub2)
    assert second.rca_groups_reused == 1
    assert second.rca_groups_investigated == 0
    assert stub2.prompts == []
    # The reused result records where the hypothesis came from.
    assert all(r.reused_from == other_id for r in second.rcas.values())


def test_batch_cost_survives_save_failure(tmp_path, monkeypatch):
    """The LLM call already happened — a failed persist must not erase the
    spend from the run report (or drop the hypothesis from the brief)."""
    from lens.rca.agent import RCAAgent

    def _boom(self, rca, run_id):
        raise OSError("disk full")

    monkeypatch.setattr(RCAAgent, "save", _boom)
    cfg = _write_config(tmp_path)
    result = run_batch(cfg, run_id="badsave", llm_client=CostStubLLM(cost_usd=0.02))

    assert result.rca_groups_investigated == 1
    assert result.total_cost_usd == pytest.approx(0.02)
    assert result.rcas  # hypothesis still attached to the findings
    assert result.brief_html_path.exists()


def test_batch_reuse_disabled_reinvestigates(tmp_path):
    cfg = _write_config(tmp_path)
    cfg.rca.reuse_prior_rca = False
    run_batch(cfg, run_id="r1", llm_client=StubLLM())
    stub2 = StubLLM()
    second = run_batch(cfg, run_id="r2", llm_client=stub2)
    assert second.rca_groups_investigated == 1
    assert second.rca_groups_reused == 0
    assert len(stub2.prompts) == 1


def test_batch_tracks_llm_cost(tmp_path):
    cfg = _write_config(tmp_path)
    stub = CostStubLLM(cost_usd=0.02)

    result = run_batch(cfg, run_id="cost1", llm_client=stub)

    # One fresh investigation → its cost rolls up to the run total.
    assert result.rca_groups_investigated == 1
    assert result.total_cost_usd == pytest.approx(0.02)
    assert result.total_input_tokens == 1000
    assert result.total_output_tokens == 200

    # Cost + model attached to every member's RCAResult.
    assert all(r.cost_usd == pytest.approx(0.02) for r in result.rcas.values())
    assert all(r.model == "sonnet" for r in result.rcas.values())  # ERROR → bulk model

    # Brief surfaces the run cost and the per-card caption, clearly labelled
    # as an estimate.
    html = result.brief_html_path.read_text(encoding="utf-8")
    assert "estimated LLM cost this run" in html
    assert "not authoritative billing" in html  # tooltip disclaimer
    assert "$0.0200" in html
    assert "Investigated with sonnet" in html
    # Digest footer carries the cost too, labelled as an estimate.
    assert "Estimated LLM cost this run" in result.markdown_digest


def test_batch_reused_rca_costs_zero_new_but_keeps_prior_cost(tmp_path):
    cfg = _write_config(tmp_path)
    first = run_batch(cfg, run_id="c1", llm_client=CostStubLLM(cost_usd=0.05))
    assert first.total_cost_usd == pytest.approx(0.05)

    # Second run reuses the prior hypothesis → zero NEW spend, even though this
    # stub would have charged 0.99 had it actually re-investigated.
    stub2 = CostStubLLM(cost_usd=0.99)
    second = run_batch(cfg, run_id="c2", llm_client=stub2)
    assert second.rca_groups_reused == 1
    assert second.rca_groups_investigated == 0
    assert stub2.prompts == []
    assert second.total_cost_usd == pytest.approx(0.0)

    # The reused result still carries its ORIGINAL cost (loaded from disk), so
    # the per-card caption shows what the investigation cost when first run.
    assert all(r.cost_usd == pytest.approx(0.05) for r in second.rcas.values())
    html = second.brief_html_path.read_text(encoding="utf-8")
    assert "$0.0500" in html


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
    assert all(f.issue.severity is Severity.INFO for f in result.findings)
    # Brief renders the suppressed section.
    html = result.brief_html_path.read_text(encoding="utf-8")
    assert "set aside earlier as false alarms" in html


def test_batch_delta_uses_pre_run_latest(tmp_path):
    cfg = _write_config(tmp_path)
    stub = StubLLM()
    run_batch(cfg, run_id="first", llm_client=stub)
    result2 = run_batch(cfg, run_id="second", llm_client=stub)
    html = result2.brief_html_path.read_text(encoding="utf-8")
    # Same data twice → all findings "still open" vs prior run.
    assert "2 still open" in html


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
    assert "All clear" in html


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
