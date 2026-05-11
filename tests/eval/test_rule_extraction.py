"""T12 — Real-LLM eval for auto-extraction of structured rules from production SQL.

These tests exercise `IngestionWorker` against the real `claude` CLI in headless
mode (the same subprocess pattern used by `lens.wiki.ingest.ClaudeCodeClient`
and the T2.5 spike at `tests/eval/spike_extract_rule.py`). The structured
`equation` block of the LLM-extracted rule is compared to a pinned ground-truth
fixture — body prose is intentionally ignored because stylistic variation is
expected and not the contract under test.

Run with:

    LENS_RUN_EVAL=1 pytest -m eval tests/eval/test_rule_extraction.py

Without `LENS_RUN_EVAL=1` the LLM-dependent test is SKIPPED so default CI never
incurs an LLM call. The safety test does NOT require `LENS_RUN_EVAL=1` since it
asserts pre-LLM behavior, but it stays under `tests/eval/` and carries the
`@pytest.mark.eval` marker so it's grouped with the eval suite.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from lens.wiki.ingest import ClaudeCodeClient, IngestionWorker
from lens.wiki.safety import UnsafePathError

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = Path(__file__).resolve().parent
FIXTURES = EVAL_DIR / "fixtures"
FIXTURE_SQL = FIXTURES / "code_senior_debt.sql"
FIXTURE_LINEAGE = FIXTURES / "lineage_senior_debt.yaml"
EXPECTED_RULE = FIXTURES / "expected_rule_senior_debt.md"
GROUND_TRUTH_SCHEMA = REPO_ROOT / "lens-wiki/rules/senior-debt-equals-pool-x-rate.md"

pytestmark = pytest.mark.eval


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(md: str) -> dict:
    """Parse YAML frontmatter from a markdown page; raise on malformed input."""
    if not md.startswith("---"):
        raise ValueError("page is missing frontmatter")
    parts = md.split("---", 2)
    if len(parts) < 3:
        raise ValueError("malformed frontmatter")
    loaded = yaml.safe_load(parts[1])
    if not isinstance(loaded, dict):
        raise ValueError("frontmatter did not parse to a mapping")
    return loaded


def _arg_key(arg: dict) -> tuple:
    """Stable, order-insensitive key for a single equation arg dict."""
    if not isinstance(arg, dict):
        return ("__nondict__", repr(arg))
    return tuple(sorted((str(k), repr(v)) for k, v in arg.items()))


def _args_match(actual_args: list, expected_args: list) -> bool:
    """Compare arg lists as sets of canonicalized dicts (order-insensitive)."""
    if len(actual_args) != len(expected_args):
        return False
    return {_arg_key(a) for a in actual_args} == {
        _arg_key(e) for e in expected_args
    }


def _tolerance_close(actual: float, expected: float) -> bool:
    """Accept tolerance within 10x of the expected value (LLM picks 0.01 vs 0.001)."""
    if actual is None or expected is None:
        return actual == expected
    try:
        a = float(actual)
        e = float(expected)
    except (TypeError, ValueError):
        return False
    if e == 0:
        return a == 0
    ratio = a / e
    return 0.1 <= ratio <= 10.0


def _seed_wiki(tmp_path: Path) -> Path:
    """Seed a clean wiki_root with just the schema-example page.

    The ingestion worker reads the schema example at runtime from
    `wiki_root/rules/senior-debt-equals-pool-x-rate.md`. We copy the real page
    in so the LLM gets the same exemplar production uses, but writes land in
    `tmp_path` and never touch the real wiki.
    """
    wiki = tmp_path / "lens-wiki"
    (wiki / "rules").mkdir(parents=True)
    (wiki / "rules" / "senior-debt-equals-pool-x-rate.md").write_text(
        GROUND_TRUTH_SCHEMA.read_text()
    )
    return wiki


# ---------------------------------------------------------------------------
# Real-LLM eval — gated by LENS_RUN_EVAL=1
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("LENS_RUN_EVAL") != "1",
    reason="LENS_RUN_EVAL=1 required to call the real LLM",
)
@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="`claude` CLI not on PATH",
)
def test_rule_extraction_matches_expected_equation(tmp_path):
    """End-to-end: run `IngestionWorker` against the real `claude` CLI.

    The LLM-extracted page must structurally match the pinned expected
    frontmatter on the `equation` block (lhs, rhs.op, rhs.args as a set, and
    tolerance within an order of magnitude). Body prose is not compared.
    """
    wiki = _seed_wiki(tmp_path)
    worker = IngestionWorker(
        repo_root=REPO_ROOT,
        wiki_root=wiki,
        client=ClaudeCodeClient(),
    )

    written = worker.ingest(
        dataset_name="senior_debt",
        code_paths=[FIXTURE_SQL],
        lineage_yaml=FIXTURE_LINEAGE,
    )

    assert len(written) == 1
    out_path = written[0]
    assert out_path.exists(), f"ingest claimed to write {out_path} but it doesn't exist"

    actual_fm = _parse_frontmatter(out_path.read_text())
    expected_fm = _parse_frontmatter(EXPECTED_RULE.read_text())

    a_eq = actual_fm.get("equation") or {}
    e_eq = expected_fm.get("equation") or {}

    # LHS: exact match on table + field (agg/group_by also compared since they
    # form part of the LHS shape, but they're allowed to be null on either side).
    a_lhs = a_eq.get("lhs") or {}
    e_lhs = e_eq.get("lhs") or {}
    assert a_lhs.get("table") == e_lhs.get("table"), (
        f"lhs.table mismatch: actual={a_lhs.get('table')!r} "
        f"expected={e_lhs.get('table')!r}"
    )
    assert a_lhs.get("field") == e_lhs.get("field"), (
        f"lhs.field mismatch: actual={a_lhs.get('field')!r} "
        f"expected={e_lhs.get('field')!r}"
    )

    # RHS: op must match exactly, args compared order-insensitively.
    a_rhs = a_eq.get("rhs") or {}
    e_rhs = e_eq.get("rhs") or {}
    assert a_rhs.get("op") == e_rhs.get("op"), (
        f"rhs.op mismatch: actual={a_rhs.get('op')!r} "
        f"expected={e_rhs.get('op')!r}"
    )

    a_args = a_rhs.get("args") or []
    e_args = e_rhs.get("args") or []
    assert _args_match(a_args, e_args), (
        f"rhs.args mismatch (order-insensitive set compare): "
        f"actual={a_args!r} expected={e_args!r}"
    )

    # Tolerance: same tolerance_type, value within 10x of expected.
    assert a_eq.get("tolerance_type") == e_eq.get("tolerance_type"), (
        f"tolerance_type mismatch: actual={a_eq.get('tolerance_type')!r} "
        f"expected={e_eq.get('tolerance_type')!r}"
    )
    assert _tolerance_close(a_eq.get("tolerance"), e_eq.get("tolerance")), (
        f"tolerance not within 10x: actual={a_eq.get('tolerance')!r} "
        f"expected={e_eq.get('tolerance')!r}"
    )


# ---------------------------------------------------------------------------
# Safety: unsafe input aborts before any LLM call
# ---------------------------------------------------------------------------


def test_extraction_handles_unsafe_path_when_secrets_present(tmp_path):
    """A `.env`-named file with an AWS-key-shaped string aborts before LLM call.

    Note: this test does NOT require `LENS_RUN_EVAL=1` — it exercises the
    pre-LLM safety boundary, so it's safe (and useful) to run in default CI.
    It carries `@pytest.mark.eval` only by inheritance from the module-level
    `pytestmark`; the only way to skip it is `pytest -m "not eval"`, which the
    non-eval-suite invocation already does.
    """
    # Seed wiki so the worker constructs cleanly; we never get far enough to
    # use it, but the constructor doesn't fail on a missing one.
    wiki = _seed_wiki(tmp_path)

    # Use a path INSIDE repo_root so containment passes — we want the failure
    # to come from the path-name pattern / content regex, not from containment.
    fake_env = REPO_ROOT / "tests/eval/fixtures/.env.unsafe"
    fake_env.write_text(
        "# fake .env for the eval safety test — value is a shaped AWS access key\n"
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
    )

    class _ExplodingLLM:
        """If safety fails open, this client makes the test fail loudly."""

        def __init__(self):
            self.calls = 0

        def complete(self, prompt: str) -> str:  # pragma: no cover — must not run
            self.calls += 1
            raise AssertionError(
                "LLM was called despite unsafe input — safety gate failed open"
            )

    exploding = _ExplodingLLM()
    worker = IngestionWorker(
        repo_root=REPO_ROOT,
        wiki_root=wiki,
        client=exploding,
    )

    try:
        with pytest.raises(UnsafePathError):
            worker.ingest(
                dataset_name="senior_debt",
                code_paths=[fake_env],
                allow_secrets=False,
            )
        assert exploding.calls == 0
    finally:
        # Tidy up the on-disk fake-secret file regardless of test outcome.
        if fake_env.exists():
            fake_env.unlink()
