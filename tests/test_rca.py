"""Tests for `lens.rca` — commit URL resolution and the RCA agent.

All subprocess interactions (git config / git log / Claude headless) are
mocked. No real LLM calls; no real git invocations against the working
copy. The agent's LLM call is exercised through a stub `LLMClient` that
records the prompt and returns canned responses.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import polars as pl
import pytest

from lens.rca import RCAAgent, commit_url
from lens.rca.agent import _parse_rca_response
from lens.types import Finding, Issue, Severity, compute_finding_id
from lens.wiki.cache import WikiCache

REPO_ROOT = Path(__file__).resolve().parents[1]
WIKI_SAMPLE = REPO_ROOT / "tests/fixtures/wiki_sample"


# ---------------------------------------------------------------------------
# git_links.commit_url
# ---------------------------------------------------------------------------


def _git_config_completed(url: str) -> subprocess.CompletedProcess:
    """Build a CompletedProcess that looks like `git config remote.origin.url`."""
    return subprocess.CompletedProcess(
        args=["git", "-C", ".", "config", "remote.origin.url"],
        returncode=0,
        stdout=url + "\n",
        stderr="",
    )


def test_commit_url_github_ssh(tmp_path):
    """SSH-style GitHub remote → https URL with /commit/<sha>."""
    with mock.patch(
        "lens.rca.git_links.subprocess.run",
        return_value=_git_config_completed("git@github.com:org/repo.git"),
    ):
        url = commit_url("abc123", tmp_path)
    assert url == "https://github.com/org/repo/commit/abc123"


def test_commit_url_github_https(tmp_path):
    """HTTPS-style GitHub remote → /commit/<sha>."""
    with mock.patch(
        "lens.rca.git_links.subprocess.run",
        return_value=_git_config_completed("https://github.com/org/repo.git"),
    ):
        url = commit_url("abc123", tmp_path)
    assert url == "https://github.com/org/repo/commit/abc123"


def test_commit_url_gitlab_uses_dash_slash(tmp_path):
    """GitLab uses the `-/commit/` path segment, not `/commit/`."""
    with mock.patch(
        "lens.rca.git_links.subprocess.run",
        return_value=_git_config_completed("git@gitlab.com:org/repo.git"),
    ):
        url = commit_url("def456", tmp_path)
    assert url == "https://gitlab.com/org/repo/-/commit/def456"


def test_commit_url_unknown_remote_returns_none(tmp_path):
    """Bitbucket and other hosts we don't recognize return None."""
    with mock.patch(
        "lens.rca.git_links.subprocess.run",
        return_value=_git_config_completed("git@bitbucket.org:org/repo.git"),
    ):
        url = commit_url("abc123", tmp_path)
    assert url is None


def test_commit_url_git_failure_returns_none(tmp_path):
    """A subprocess failure (no .git dir, no git binary, etc.) returns None."""
    with mock.patch(
        "lens.rca.git_links.subprocess.run",
        side_effect=subprocess.CalledProcessError(128, "git"),
    ):
        url = commit_url("abc123", tmp_path)
    assert url is None


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _StubLLM:
    """Stub LLMClient — records prompts, returns canned responses."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _make_finding(
    *,
    entity_id: str = "deal-1",
    field_name: str = "balance",
    snapshot_date: datetime | None = None,
    description: str = "value deviates from expected",
    detector_source: str = "stl_residual",
) -> Finding:
    """Build a Finding with a real compute_finding_id."""
    snap = snapshot_date or datetime(2026, 5, 8)
    fid = compute_finding_id(entity_id, field_name, snap)
    issue = Issue(
        check_name="stl_residual",
        severity=Severity.WARNING,
        entity_id=entity_id,
        field_name=field_name,
        snapshot_date=snap,
        description=description,
        details={"score": 3.5, "z_score": 3.5, "__source__": "senior_debt"},
        confidence=0.8,
        detector_source=detector_source,
        finding_id=fid,
    )
    return Finding(
        issue=issue,
        detector_sources=[detector_source],
        detected_at=datetime(2026, 5, 10),
        run_id="20260510T120000-aaaabbbb",
    )


def _make_sources() -> dict[str, pl.LazyFrame]:
    """Synthetic 5-day senior_debt LazyFrame keyed by `deal-1`."""
    df = pl.DataFrame(
        {
            "entity_id": ["deal-1"] * 5,
            "snapshot_date": [
                datetime(2026, 5, 4),
                datetime(2026, 5, 5),
                datetime(2026, 5, 6),
                datetime(2026, 5, 7),
                datetime(2026, 5, 8),
            ],
            "balance": [100.0, 101.0, 102.0, 101.5, 1500.0],  # last is the anomaly
        }
    )
    return {"senior_debt": df.lazy()}


def _fenced_json_response(payload: dict[str, Any]) -> str:
    """Wrap a dict in the prompt's expected ```json fence."""
    return (
        "Here is my analysis:\n\n"
        "```json\n"
        + json.dumps(payload)
        + "\n```\n"
    )


# ---------------------------------------------------------------------------
# _parse_rca_response — small unit test that the helper is well-behaved
# ---------------------------------------------------------------------------


def test_parse_rca_response_extracts_fenced_json():
    """Sanity: parser pulls fields out of a fenced JSON block."""
    payload = {
        "hypothesis": "Upstream advance_rate drift.",
        "evidence": ["row at 2026-05-08", "rule_a inverted"],
        "confidence": 0.62,
        "references": ["wiki:rule_a"],
    }
    raw = _fenced_json_response(payload)
    result = _parse_rca_response(raw, finding_id="fid-1", fallback_references=["url-x"])
    assert result.finding_id == "fid-1"
    assert result.hypothesis == "Upstream advance_rate drift."
    assert "row at 2026-05-08" in result.evidence
    assert result.confidence == pytest.approx(0.62)
    # Fallback URL must still appear in references.
    assert "url-x" in result.references


# ---------------------------------------------------------------------------
# RCAAgent.investigate
# ---------------------------------------------------------------------------


def test_rca_investigate_with_stub_llm_returns_parsed_result(tmp_path):
    """A fenced-JSON LLM response produces a fully-parsed RCAResult."""
    payload = {
        "hypothesis": "Recent commit on senior_debt.sql changed the aggregation key.",
        "evidence": ["spike at 2026-05-08", "prior 4 snapshots flat ~100"],
        "confidence": 0.75,
        "references": ["wiki:rule_a"],
    }
    stub = _StubLLM(_fenced_json_response(payload))
    agent = RCAAgent(repo_root=tmp_path, client=stub)
    wiki = WikiCache()  # empty wiki → no producing-code paths, no git work
    finding = _make_finding()

    # Ensure git log is not called when there are no producing-code paths;
    # patch defensively in case the test path changes.
    with mock.patch("lens.rca.agent.subprocess.run") as run_mock:
        result = agent.investigate(finding, wiki, _make_sources())

    assert result.finding_id == finding.finding_id
    assert "aggregation key" in result.hypothesis
    assert result.confidence == pytest.approx(0.75)
    assert "wiki:rule_a" in result.references
    # No producing-code paths → no git log subprocess calls.
    run_mock.assert_not_called()
    # The prompt should mention the anomalous snapshot date.
    assert len(stub.prompts) == 1
    assert "2026-05-08" in stub.prompts[0]


def test_rca_investigate_handles_non_json_response_gracefully(tmp_path):
    """Plain prose (no JSON fence) → hypothesis=prose, confidence=0.0."""
    prose = "I am not following the schema; the issue is probably advance_rate."
    stub = _StubLLM(prose)
    agent = RCAAgent(repo_root=tmp_path, client=stub)
    wiki = WikiCache()
    finding = _make_finding()

    with mock.patch("lens.rca.agent.subprocess.run"):
        result = agent.investigate(finding, wiki, _make_sources())

    assert result.finding_id == finding.finding_id
    assert prose in result.hypothesis
    assert result.confidence == 0.0
    assert result.evidence == []


def test_rca_save_writes_per_run_per_finding_json(tmp_path):
    """save() writes rca/<run_id>/<finding_id>.json; rewrites are idempotent."""
    stub = _StubLLM(_fenced_json_response({
        "hypothesis": "h",
        "evidence": ["e"],
        "confidence": 0.5,
        "references": [],
    }))
    agent = RCAAgent(repo_root=tmp_path, client=stub, output_dir=tmp_path)
    finding = _make_finding()
    with mock.patch("lens.rca.agent.subprocess.run"):
        result = agent.investigate(finding, WikiCache(), _make_sources())

    out1 = agent.save(result, run_id="run-abc")
    assert out1 == tmp_path / "rca" / "run-abc" / f"{finding.finding_id}.json"
    assert out1.exists()
    payload1 = json.loads(out1.read_text())
    assert payload1["finding_id"] == finding.finding_id
    assert payload1["hypothesis"] == "h"

    # Second call with same args overwrites cleanly (no tmp leftovers, no crash).
    out2 = agent.save(result, run_id="run-abc")
    assert out2 == out1
    assert out2.exists()
    # No `.tmp.*` files left over.
    leftovers = list((tmp_path / "rca" / "run-abc").glob("*.tmp.*"))
    assert leftovers == []


def test_rca_uses_wiki_cache_lookups(tmp_path):
    """The prompt must include content from the loaded wiki (rule_a + lineage)."""
    stub = _StubLLM(_fenced_json_response({
        "hypothesis": "h",
        "evidence": [],
        "confidence": 0.5,
        "references": [],
    }))
    agent = RCAAgent(repo_root=tmp_path, client=stub)
    wiki = WikiCache.from_dir(WIKI_SAMPLE)
    # Sanity: the fixture loaded successfully.
    assert any(r.name == "rule_a" for r in wiki.rules)
    assert "senior_debt" in wiki.lineages

    finding = _make_finding(field_name="senior_debt.balance")
    # Patch git to avoid real subprocess calls on producing-code paths.
    with mock.patch(
        "lens.rca.agent.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        ),
    ):
        agent.investigate(finding, wiki, _make_sources())

    assert len(stub.prompts) == 1
    prompt = stub.prompts[0]
    # Rule page reference appears in the rules section.
    assert "rule_a" in prompt
    # Lineage page reference appears in the lineage section.
    assert "senior_debt" in prompt
    assert "lens/transforms/senior_debt.sql" in prompt


def test_rca_collects_commit_urls_from_wiki_producing_code(tmp_path):
    """When the wiki has a producing-code path, git_log → commit_url is in references."""

    def fake_subprocess_run(cmd, *args, **kwargs):
        # Two callers in agent.py:
        #   * `git ... config remote.origin.url`  (via commit_url)
        #   * `git ... log ...`                   (via _git_log_for_path)
        if "log" in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="deadbeef1234567890abcdef | 2026-05-09 | refactor advance_rate\n",
                stderr="",
            )
        if "config" in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="git@github.com:org/repo.git\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    stub = _StubLLM(_fenced_json_response({
        "hypothesis": "h",
        "evidence": [],
        "confidence": 0.5,
        "references": [],
    }))
    agent = RCAAgent(repo_root=tmp_path, client=stub)
    wiki = WikiCache.from_dir(WIKI_SAMPLE)
    finding = _make_finding()

    with (
        mock.patch("lens.rca.agent.subprocess.run", side_effect=fake_subprocess_run),
        mock.patch("lens.rca.git_links.subprocess.run", side_effect=fake_subprocess_run),
    ):
        result = agent.investigate(finding, wiki, _make_sources())

    expected_url = "https://github.com/org/repo/commit/deadbeef1234567890abcdef"
    assert expected_url in result.references
    # The recent-commits section in the prompt must surface the URL too.
    assert expected_url in stub.prompts[0]
