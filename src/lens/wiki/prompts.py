"""LLM prompt templates for the wiki ingestion worker.

Single source of truth for the rule-extraction prompt shape. Mirrors the
T2.5 spike script (`tests/eval/spike_extract_rule.py`) so the eval and the
production ingestion path stay in lockstep.
"""

from __future__ import annotations

RULE_EXTRACTION_PROMPT: str = """\
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
"""Template with `{sql}`, `{lineage}`, `{schema_example}` placeholders.

`schema_example` is read at runtime from
`lens-wiki/rules/senior-debt-equals-pool-x-rate.md`. The ingestion worker
fills the three slots via `RULE_EXTRACTION_PROMPT.format(...)` and sends the
result to the LLM.
"""
