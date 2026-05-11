"""T2.5 spike — rule-extraction gate.

One-shot script (not a pytest). Run manually with `LENS_RUN_EVAL=1`. It
prompts an LLM to extract a structured rule from the fixture SQL+lineage and
compares the result to the hand-authored ground-truth page.

Outcomes:
    PASS  → T4 scopes to full LLM-driven ingestion worker.
    FAIL  → T4 scopes to a loader for hand-authored rules; auto-extraction
            deferred to v2.1. Document the decision in lens-wiki/index.md.

Usage:
    LENS_RUN_EVAL=1 ANTHROPIC_API_KEY=... python tests/eval/spike_extract_rule.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SQL_PATH = REPO / "tests/eval/fixtures/code_senior_debt.sql"
LINEAGE_PATH = REPO / "tests/eval/fixtures/lineage_senior_debt.yaml"
GROUND_TRUTH = REPO / "lens-wiki/rules/senior-debt-equals-pool-x-rate.md"

PROMPT = """\
You are extracting a structured data-quality rule from production
transformation code.

Read the SQL below and the lineage YAML. Output ONE markdown page with YAML
frontmatter and a body, exactly in the schema shown by the example.

The page must include:
  - `name` (slug)
  - `description` (one sentence)
  - `tables` (list)
  - `fields` (list of `<table>.<field>`)
  - `equation` with `lhs` and `rhs` as STRUCTURED nodes:
      - `lhs`: {{table, field, agg, group_by}}
      - `rhs`: {{op, args: [...]}}; op in {{add, sub, mul, div}}; each arg is
              either a structured node like lhs OR a nested rhs.
      - `tolerance`, `tolerance_type` (absolute | relative).

Do NOT emit Python or SQL expression strings. Use the structured form only.

--- SCHEMA EXAMPLE ---

{schema_example}

--- SQL ---

{sql}

--- LINEAGE YAML ---

{lineage}

Now produce the rule page for the SQL above. Use the same structured
frontmatter shape as the example.
"""


def _read(path: Path) -> str:
    return path.read_text()


def _extract_frontmatter(md: str) -> dict:
    if not md.startswith("---"):
        raise ValueError("page is missing frontmatter")
    parts = md.split("---", 2)
    if len(parts) < 3:
        raise ValueError("malformed frontmatter")
    return yaml.safe_load(parts[1]) or {}


def _equation_matches(actual: dict, expected: dict) -> tuple[bool, str]:
    """Structurally compare equations; tolerant of arg order in commutative ops."""
    a_eq = actual.get("equation") or {}
    e_eq = expected.get("equation") or {}

    if a_eq.get("lhs") != e_eq.get("lhs"):
        return False, f"lhs mismatch: actual={a_eq.get('lhs')!r} expected={e_eq.get('lhs')!r}"

    a_rhs = a_eq.get("rhs") or {}
    e_rhs = e_eq.get("rhs") or {}
    if a_rhs.get("op") != e_rhs.get("op"):
        return False, f"rhs.op mismatch: actual={a_rhs.get('op')!r} expected={e_rhs.get('op')!r}"

    a_args = a_rhs.get("args") or []
    e_args = e_rhs.get("args") or []
    if sorted(map(str, a_args)) != sorted(map(str, e_args)):
        return False, f"rhs.args mismatch (order-insensitive): actual={a_args!r} expected={e_args!r}"

    return True, "equation matches structurally"


def main() -> int:
    if os.environ.get("LENS_RUN_EVAL") != "1":
        print("SKIP: LENS_RUN_EVAL=1 required to run the real-LLM spike. "
              "Default decision: proceed with full ingestion-worker scope, "
              "with hand-authored fallback path retained in T4.")
        return 0

    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError:
        print("FAIL: anthropic SDK not installed. `pip install anthropic` and retry.")
        return 2

    sql = _read(SQL_PATH)
    lineage = _read(LINEAGE_PATH)
    schema_example = _read(GROUND_TRUTH)

    prompt = PROMPT.format(sql=sql, lineage=lineage, schema_example=schema_example)

    client = Anthropic()
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")

    try:
        actual_fm = _extract_frontmatter(text)
        expected_fm = _extract_frontmatter(_read(GROUND_TRUTH))
    except ValueError as e:
        print(f"FAIL: could not parse LLM output as a frontmatter page: {e}")
        print("--- raw output ---")
        print(text)
        return 3

    ok, note = _equation_matches(actual_fm, expected_fm)
    print(f"{'PASS' if ok else 'FAIL'}: {note}")
    if not ok:
        print("--- actual frontmatter ---")
        print(yaml.dump(actual_fm))
        print("--- expected frontmatter ---")
        print(yaml.dump(expected_fm))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
