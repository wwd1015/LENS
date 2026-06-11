"""Acceptance test: the shipped lending demo runs end-to-end.

This is the completion gate agreed in the design review: synthetic CSVs +
hand-authored wiki + one batch run produce a brief with group RCA, a
cross-source breach, a null_check hit, and detector-family agreement.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from lens.batch import run_batch
from lens.run_config import load_run_config
from lens.types import Severity

DEMO_DIR = Path(__file__).parent.parent / "examples" / "lending_demo"

RCA_RESPONSE = """\
```json
{"hypothesis": "Advance-rate input went stale for D2.",
 "evidence": ["senior_debt.balance 12% above pool x rate on the final snapshot"],
 "confidence": 0.6,
 "references": []}
```
"""


class StubLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return RCA_RESPONSE


@pytest.fixture()
def demo_cfg(tmp_path):
    """The shipped demo config, with output redirected outside the repo."""
    cfg = load_run_config(DEMO_DIR / "lens-run.yaml")
    return dataclasses.replace(
        cfg,
        output_dir=tmp_path / "out",
        feedback=dataclasses.replace(cfg.feedback, path=tmp_path / "out" / "feedback.jsonl"),
    )


def test_demo_end_to_end(demo_cfg):
    stub = StubLLM()
    result = run_batch(demo_cfg, run_id="demo", llm_client=stub)

    by_detector: dict[str, list] = {}
    for f in result.findings:
        for fam in f.detector_families:
            by_detector.setdefault(fam, []).append(f)

    # Planted anomaly 1: D3 null statuses → null_check ERROR.
    null_hits = by_detector.get("null_check", [])
    assert null_hits, "null_check should fire on D3's null statuses"
    assert {f.issue.entity_id for f in null_hits} == {"D3"}
    assert all(f.issue.severity is Severity.ERROR for f in null_hits)

    # Planted anomaly 2: D2 senior-debt inflation → cross-source rule breach.
    cross_hits = by_detector.get("cross_source_wiki", [])
    assert cross_hits, "the wiki rule should fire on D2's inflated senior debt"
    assert {f.issue.entity_id for f in cross_hits} == {"D2"}

    # The same inflated point also spikes the STL residual → two independent
    # families on one finding → agreement boost.
    boosted = [
        f
        for f in result.findings
        if (f.issue.details or {}).get("agreement_boost")
    ]
    assert boosted, "cross_source_wiki + stl_residual should agree on D2"
    assert any(f.issue.entity_id == "D2" for f in boosted)

    # Group RCA ran (floor=error) and attached hypotheses.
    assert result.rca_groups_investigated >= 2  # null_check group + D2 group(s)
    assert result.rcas
    for prompt in stub.prompts:
        assert "# Finding" in prompt

    # Artifacts: findings.json, brief HTML + latest symlink, markdown digest.
    assert result.findings_path.exists()
    data = json.loads(result.findings_path.read_text())
    assert len(data) == len(result.findings)
    html = result.brief_html_path.read_text(encoding="utf-8")
    assert "Lending demo portfolio" in html
    assert "feedback-bar" in html  # one-click buttons render
    assert (demo_cfg.output_dir / "brief.latest.html").is_symlink()
    assert "LENS Brief" in result.markdown_digest


def test_demo_feedback_suppression_round_trip(demo_cfg):
    """FP the null_check findings, re-run: downgraded to INFO, no RCA for them."""
    from lens.brief.feedback import append_feedback

    stub = StubLLM()
    first = run_batch(demo_cfg, run_id="fb1", llm_client=stub)
    null_findings = [
        f for f in first.findings if "null_check" in f.detector_families
    ]
    assert null_findings
    for f in null_findings:
        append_feedback(
            f.finding_id,
            "false_positive",
            demo_cfg.feedback.path,
            entity_id=f.issue.entity_id,
            field_name=f.issue.field_name,
            detector_sources=f.detector_sources,
        )

    second = run_batch(demo_cfg, run_id="fb2", llm_client=StubLLM())
    suppressed = [
        f
        for f in second.findings
        if (f.issue.details or {}).get("suppressed_by_feedback")
    ]
    assert suppressed
    assert all(f.issue.severity is Severity.INFO for f in suppressed)
    assert {f.issue.entity_id for f in suppressed} == {"D3"}
    html = second.brief_html_path.read_text(encoding="utf-8")
    assert "suppressed by analyst feedback" in html
