"""Tests for `lens.wiki.safety` and `lens.wiki.ingest`.

Covers the secret-file allowlist (path-name patterns, containment, content
regexes) and the LLM-driven ingestion worker (stub LLM, retry behavior,
safety integration, idempotence, hand-authored fallback).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lens.wiki.ingest import (
    CallCost,
    IngestionWorker,
    _parse_claude_json,
    load_hand_authored,
)
from lens.wiki.safety import (
    UnsafePathError,
    assert_safe_to_send,
    is_safe_to_send,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests/fixtures/ingest_sample"
WIKI_SOURCE = REPO_ROOT / "lens-wiki"
SCHEMA_EXAMPLE = WIKI_SOURCE / "rules/senior-debt-equals-pool-x-rate.md"


# ---------------------------------------------------------------------------
# _parse_claude_json — cost/usage extraction from `claude -p --output-format json`
# ---------------------------------------------------------------------------


def test_parse_claude_json_extracts_text_and_cost():
    """A well-formed result envelope yields the text plus cost/token usage."""
    stdout = json.dumps(
        {
            "type": "result",
            "result": "the model's answer",
            "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 340,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 0,
            },
            "session_id": "abc",
        }
    )
    text, cost = _parse_claude_json(stdout)
    assert text == "the model's answer"
    assert isinstance(cost, CallCost)
    assert cost.cost_usd == pytest.approx(0.0123)
    assert cost.input_tokens == 1200
    assert cost.output_tokens == 340
    assert cost.cache_read_input_tokens == 100


def test_parse_claude_json_non_json_degrades_to_text_zero_cost():
    """Plain-text stdout (older CLI / error banner) → passthrough, zero cost."""
    text, cost = _parse_claude_json("just some plain text, not json")
    assert text == "just some plain text, not json"
    assert cost == CallCost()
    assert cost.cost_usd == 0.0


def test_parse_claude_json_missing_usage_keeps_cost():
    """A result without a usage block still surfaces total_cost_usd."""
    stdout = json.dumps({"result": "hi", "total_cost_usd": 0.5})
    text, cost = _parse_claude_json(stdout)
    assert text == "hi"
    assert cost.cost_usd == pytest.approx(0.5)
    assert cost.input_tokens == 0
    assert cost.output_tokens == 0


# ---------------------------------------------------------------------------
# is_safe_to_send / assert_safe_to_send
# ---------------------------------------------------------------------------


def test_is_safe_to_send_rejects_dotenv():
    """`.env*`-named files trip the path-name pattern."""
    p = FIXTURES / ".env.test"
    assert p.exists(), "fixture missing"
    assert is_safe_to_send(p, REPO_ROOT) is False


def test_is_safe_to_send_rejects_credentials_file():
    """`credentials*` files trip the path-name pattern."""
    p = FIXTURES / "credentials.json"
    assert p.exists()
    assert is_safe_to_send(p, REPO_ROOT) is False


def test_is_safe_to_send_rejects_secrets_pattern(tmp_path):
    """`secrets*` files trip the path-name pattern."""
    p = tmp_path / "secrets.yaml"
    p.write_text("k: v\n")
    assert is_safe_to_send(p, tmp_path) is False


def test_is_safe_to_send_rejects_pem(tmp_path):
    """`*.pem` files trip the path-name pattern."""
    p = tmp_path / "server.pem"
    p.write_text("-----BEGIN CERTIFICATE-----\n")
    assert is_safe_to_send(p, tmp_path) is False


def test_is_safe_to_send_rejects_key(tmp_path):
    """`*.key` files trip the path-name pattern."""
    p = tmp_path / "private.key"
    p.write_text("-----BEGIN PRIVATE KEY-----\n")
    assert is_safe_to_send(p, tmp_path) is False


def test_is_safe_to_send_rejects_outside_repo_root(tmp_path):
    """A path outside repo_root is rejected even if name is fine."""
    outside = tmp_path / "code.sql"
    outside.write_text("SELECT 1;")
    assert is_safe_to_send(outside, REPO_ROOT) is False


def test_is_safe_to_send_rejects_aws_key_content():
    """Content-regex layer catches AWS-key-shaped strings even when path is fine."""
    p = FIXTURES / "leaky.txt"
    assert p.exists()
    assert is_safe_to_send(p, REPO_ROOT) is False


def test_is_safe_to_send_accepts_normal_sql():
    """A benign SQL file with no secret-shaped name or content is accepted."""
    p = FIXTURES / "safe_query.sql"
    assert p.exists()
    assert is_safe_to_send(p, REPO_ROOT) is True


def test_is_safe_to_send_rejects_modern_credential_shapes(tmp_path):
    """Code-review P1 #2: content-regex layer must catch modern token shapes
    beyond AWS keys — OpenAI sk-, GitHub PATs (ghp_/gho_/...), Slack
    xox[abprs]-, and JWTs (eyJ...)."""
    cases = {
        "openai.txt": "openai_token = sk-abcdefghij1234567890ABCDEFGHIJ",
        "github.txt": "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "slack.txt": "SLACK=xoxb-1234567890-1234567890-abcdef",
        "jwt.txt": (
            "Authorization: Bearer "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abc123signature"
        ),
    }
    for fname, content in cases.items():
        p = tmp_path / fname
        p.write_text(content)
        assert is_safe_to_send(p, tmp_path) is False, f"{fname} should be rejected"


def test_assert_safe_to_send_raises_on_unsafe():
    """`assert_safe_to_send` raises `UnsafePathError` (not returns False)."""
    p = FIXTURES / ".env.test"
    with pytest.raises(UnsafePathError):
        assert_safe_to_send(p, REPO_ROOT)


def test_assert_safe_to_send_noop_on_safe():
    """`assert_safe_to_send` returns None for safe paths."""
    p = FIXTURES / "safe_query.sql"
    assert assert_safe_to_send(p, REPO_ROOT) is None


# ---------------------------------------------------------------------------
# IngestionWorker — stub LLM
# ---------------------------------------------------------------------------


_STUB_RESPONSE = """\
---
name: senior-debt-equals-pool-x-rate
description: Senior debt balance must equal sum of loan-pool balances x advance rate
tables:
  - senior_debt
  - loan_pool
  - deal_terms
fields:
  - senior_debt.balance
equation:
  lhs:
    table: senior_debt
    field: balance
    agg: null
    group_by: null
  rhs:
    op: mul
    args:
      - table: loan_pool
        field: balance
        agg: sum
        group_by: deal_id
      - table: deal_terms
        field: advance_rate
        agg: null
        group_by: null
  tolerance: 0.001
  tolerance_type: relative
source_commit: HEAD
confidence: high
last_verified: 2026-05-10
---

# Rule: senior-debt-equals-pool-x-rate

Body prose.
"""


class _StubLLM:
    """LLM stub that returns a pre-canned response and records calls."""

    def __init__(self, response: str = _STUB_RESPONSE):
        self.response = response
        self.calls: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.response


class _FlakyLLM:
    """LLM stub that fails N times then returns a canned response."""

    def __init__(self, fail_n: int, response: str = _STUB_RESPONSE):
        self.remaining_failures = fail_n
        self.response = response
        self.calls: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise RuntimeError("simulated transient LLM failure")
        return self.response


class _AlwaysFailLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        raise RuntimeError("permanent LLM failure")


def _seed_wiki(tmp_path: Path) -> Path:
    """Create a minimal wiki_root with the schema example page."""
    wiki = tmp_path / "lens-wiki"
    (wiki / "rules").mkdir(parents=True)
    (wiki / "rules" / "senior-debt-equals-pool-x-rate.md").write_text(
        SCHEMA_EXAMPLE.read_text()
    )
    return wiki


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Make retry back-off instant in tests."""
    monkeypatch.setattr("lens.wiki.ingest.time.sleep", lambda _s: None)


def test_ingest_happy_path(tmp_path):
    """Stub LLM → parse → write to wiki_root/rules/<slug>.md."""
    wiki = _seed_wiki(tmp_path)
    stub = _StubLLM()
    worker = IngestionWorker(
        repo_root=REPO_ROOT, wiki_root=wiki, client=stub, max_retries=3
    )

    written = worker.ingest(
        dataset_name="senior_debt",
        code_paths=[FIXTURES / "safe_query.sql"],
    )

    assert len(written) == 1
    out = written[0]
    assert out.name == "senior-debt-equals-pool-x-rate.md"
    assert out.parent == wiki / "rules"
    assert "senior_debt" in out.read_text()
    assert len(stub.calls) == 1


def test_ingest_retries_then_succeeds(tmp_path):
    """LLM fails twice, succeeds on third attempt; ingest completes."""
    wiki = _seed_wiki(tmp_path)
    flaky = _FlakyLLM(fail_n=2)
    worker = IngestionWorker(
        repo_root=REPO_ROOT, wiki_root=wiki, client=flaky, max_retries=3
    )

    written = worker.ingest(
        dataset_name="senior_debt",
        code_paths=[FIXTURES / "safe_query.sql"],
    )

    assert len(written) == 1
    assert len(flaky.calls) == 3


def test_ingest_raises_after_max_retries(tmp_path):
    """LLM always fails → ingest raises after `max_retries` attempts."""
    wiki = _seed_wiki(tmp_path)
    bad = _AlwaysFailLLM()
    worker = IngestionWorker(
        repo_root=REPO_ROOT, wiki_root=wiki, client=bad, max_retries=3
    )

    with pytest.raises(RuntimeError, match="permanent LLM failure"):
        worker.ingest(
            dataset_name="senior_debt",
            code_paths=[FIXTURES / "safe_query.sql"],
        )

    assert bad.calls == 3


def test_ingest_aborts_on_unsafe_path(tmp_path):
    """A `.env`-named code_path aborts the ingest call before any LLM call."""
    wiki = _seed_wiki(tmp_path)
    stub = _StubLLM()
    worker = IngestionWorker(
        repo_root=REPO_ROOT, wiki_root=wiki, client=stub, max_retries=3
    )

    with pytest.raises(UnsafePathError):
        worker.ingest(
            dataset_name="senior_debt",
            code_paths=[FIXTURES / ".env.test"],
        )

    assert stub.calls == [], "LLM must not be called when safety check fails"


def test_ingest_allows_secrets_override(tmp_path):
    """`allow_secrets=True` bypasses the safety check; LLM is called."""
    wiki = _seed_wiki(tmp_path)
    stub = _StubLLM()
    worker = IngestionWorker(
        repo_root=REPO_ROOT, wiki_root=wiki, client=stub, max_retries=3
    )

    written = worker.ingest(
        dataset_name="senior_debt",
        code_paths=[FIXTURES / ".env.test"],
        allow_secrets=True,
    )
    assert len(written) == 1
    assert len(stub.calls) == 1


def test_ingest_retries_on_missing_frontmatter_field(tmp_path):
    """If response is parseable but missing equation.lhs.table, retry."""
    wiki = _seed_wiki(tmp_path)
    bad_response = """\
---
name: bad-rule
description: missing equation.lhs.table on purpose
---

body
"""

    class _MissingThenGood:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt: str) -> str:
            self.calls += 1
            if self.calls == 1:
                return bad_response
            return _STUB_RESPONSE

    client = _MissingThenGood()
    worker = IngestionWorker(
        repo_root=REPO_ROOT, wiki_root=wiki, client=client, max_retries=3
    )

    written = worker.ingest(
        dataset_name="senior_debt",
        code_paths=[FIXTURES / "safe_query.sql"],
    )
    assert len(written) == 1
    assert client.calls == 2


def test_ingest_idempotent(tmp_path):
    """Same inputs twice → same on-disk content."""
    wiki = _seed_wiki(tmp_path)
    stub = _StubLLM()
    worker = IngestionWorker(
        repo_root=REPO_ROOT, wiki_root=wiki, client=stub, max_retries=3
    )

    a = worker.ingest(
        dataset_name="senior_debt",
        code_paths=[FIXTURES / "safe_query.sql"],
    )
    content_a = a[0].read_text()
    b = worker.ingest(
        dataset_name="senior_debt",
        code_paths=[FIXTURES / "safe_query.sql"],
    )
    content_b = b[0].read_text()

    assert a[0] == b[0]
    assert content_a == content_b


def test_ingest_missing_schema_example_raises(tmp_path):
    """If the schema-example page is absent, ingest raises before LLM call."""
    wiki = tmp_path / "lens-wiki"
    (wiki / "rules").mkdir(parents=True)
    # Deliberately NOT writing the schema example.
    stub = _StubLLM()
    worker = IngestionWorker(
        repo_root=REPO_ROOT, wiki_root=wiki, client=stub, max_retries=3
    )
    with pytest.raises(FileNotFoundError):
        worker.ingest(
            dataset_name="senior_debt",
            code_paths=[FIXTURES / "safe_query.sql"],
        )
    assert stub.calls == []


# ---------------------------------------------------------------------------
# load_hand_authored — T2.5 fallback path
# ---------------------------------------------------------------------------


def test_load_hand_authored_copies_valid_pages(tmp_path):
    """Valid hand-authored pages are copied; returned paths exist."""
    src = tmp_path / "hand_rules"
    src.mkdir()
    (src / "good.md").write_text(SCHEMA_EXAMPLE.read_text())

    wiki = tmp_path / "lens-wiki"
    written = load_hand_authored(src, wiki)

    assert len(written) == 1
    assert written[0] == wiki / "rules" / "good.md"
    assert written[0].exists()


def test_load_hand_authored_rejects_missing_name(tmp_path):
    """A page without a `name` field raises ValueError."""
    src = tmp_path / "hand_rules"
    src.mkdir()
    (src / "bad.md").write_text(
        "---\n"
        "description: no name\n"
        "equation:\n"
        "  lhs:\n"
        "    table: senior_debt\n"
        "    field: balance\n"
        "---\n\nbody\n"
    )
    wiki = tmp_path / "lens-wiki"
    with pytest.raises(ValueError, match="name"):
        load_hand_authored(src, wiki)


def test_load_hand_authored_rejects_missing_equation_table(tmp_path):
    """A page without `equation.lhs.table` raises ValueError."""
    src = tmp_path / "hand_rules"
    src.mkdir()
    (src / "bad.md").write_text(
        "---\n"
        "name: incomplete-rule\n"
        "description: no equation.lhs.table\n"
        "---\n\nbody\n"
    )
    wiki = tmp_path / "lens-wiki"
    with pytest.raises(ValueError, match="equation.lhs.table"):
        load_hand_authored(src, wiki)


def test_load_hand_authored_missing_dir_raises(tmp_path):
    """A missing source directory raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_hand_authored(tmp_path / "does-not-exist", tmp_path / "lens-wiki")


# ---------------------------------------------------------------------------
# Headless-client hardening: stdin prompt, is_error envelope, no-retry on
# missing binary, provenance overwrite guard.
# ---------------------------------------------------------------------------


def test_claude_client_sends_prompt_via_stdin(monkeypatch):
    """The prompt must go over stdin, not argv — argv has a ~128KiB ceiling
    and is world-readable via /proc/<pid>/cmdline."""
    from lens.wiki import ingest as ingest_mod
    from lens.wiki.ingest import ClaudeCodeClient

    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")

        class _R:
            stdout = json.dumps({"result": "ok", "total_cost_usd": 0.01, "usage": {}})

        return _R()

    monkeypatch.setattr(ingest_mod.subprocess, "run", _fake_run)
    out = ClaudeCodeClient().complete("the prompt with lending data")
    assert out == "ok"
    assert captured["input"] == "the prompt with lending data"
    assert "the prompt with lending data" not in captured["cmd"]


def test_parse_claude_json_raises_on_is_error():
    from lens.wiki.ingest import _parse_claude_json

    envelope = json.dumps({"is_error": True, "result": "Invalid API key"})
    with pytest.raises(RuntimeError, match="Invalid API key"):
        _parse_claude_json(envelope)


def test_unavailable_client_is_not_retried(tmp_path):
    """`claude` missing from PATH is a config problem — one attempt, not
    max_retries × timeout."""
    from lens.wiki.ingest import ClaudeCodeUnavailableError

    class _MissingBinaryLLM:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt: str) -> str:
            self.calls += 1
            raise ClaudeCodeUnavailableError("`claude` not on PATH")

    wiki = _seed_wiki(tmp_path)
    client = _MissingBinaryLLM()
    worker = IngestionWorker(repo_root=REPO_ROOT, wiki_root=wiki, client=client, max_retries=3)
    with pytest.raises(ClaudeCodeUnavailableError):
        worker.ingest(dataset_name="senior_debt", code_paths=[FIXTURES / "safe_query.sql"])
    assert client.calls == 1


def test_ingest_refuses_cross_source_overwrite(tmp_path, caplog):
    """A hostile source whose extracted name collides with another source's
    page must not overwrite it (prompt-injection → detection bypass)."""
    wiki = _seed_wiki(tmp_path)
    worker = IngestionWorker(
        repo_root=REPO_ROOT, wiki_root=wiki, client=_StubLLM(), max_retries=3
    )

    # Source A ingests and owns the page.
    written = worker.ingest(
        dataset_name="senior_debt", code_paths=[FIXTURES / "safe_query.sql"]
    )
    assert len(written) == 1
    original = written[0].read_text()

    # A DIFFERENT source produces the same extracted name → refused.
    other_sql = tmp_path / "other_source.sql"
    other_sql.write_text("SELECT 1 AS hostile")
    with caplog.at_level("WARNING"):
        refused = worker.ingest(
            dataset_name="other_dataset",
            code_paths=[other_sql],
            allow_secrets=True,
        )
    assert refused == []
    assert written[0].read_text() == original
    assert any("ingestion refused" in rec.message for rec in caplog.records)

    # Re-ingest of the ORIGINAL source still updates its own page.
    again = worker.ingest(
        dataset_name="senior_debt", code_paths=[FIXTURES / "safe_query.sql"]
    )
    assert again == written
