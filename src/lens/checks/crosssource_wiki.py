"""Wiki-driven cross-source detector.

Reads structured equation specs from `lens-wiki/rules/*.md` via a
``WikiCache`` and executes each rule against a dict of LazyFrames. This is the
generalized big-brother of ``CrossSourceMatchCheck``: instead of comparing one
field across two sources, it can express arbitrary multi-source equations like
``senior_debt.balance == sum(loan_pool.balance per deal) * deal_terms.advance_rate``.

CRITICAL: rule equations are structured frontmatter, never expression strings —
this module does not call ``eval``, ``ast.parse``, or ``ast.literal_eval``.
"""

from __future__ import annotations

import logging
from typing import Any

import polars as pl

from lens.checks.base import BaseCheck
from lens.checks.equation import evaluate_equation
from lens.checks.registry import registry
from lens.types import CheckResult, Issue, Severity

logger = logging.getLogger(__name__)


def _collect_referenced_tables(node: Any, out: set[str]) -> None:
    """Walk an equation node and accumulate every leaf's table name."""
    if not isinstance(node, dict):
        return
    if "table" in node and "field" in node:
        out.add(str(node["table"]))
        return
    args = node.get("args")
    if isinstance(args, list):
        for arg in args:
            _collect_referenced_tables(arg, out)


@registry.register
class CrossSourceWikiCheck(BaseCheck):
    """Run every wiki-defined cross-source equation rule against a source dict."""

    name = "cross_source_wiki"
    description = (
        "Evaluates structured equation rules from the LENS wiki against a "
        "dict of LazyFrames; flags entities whose lhs and rhs disagree "
        "beyond the rule's tolerance."
    )
    default_severity = Severity.ERROR

    def __init__(self, **kwargs: Any) -> None:
        # No detector-specific constructor args — the wiki is the source of
        # truth for which rules to run. Pass severity / params through to base.
        super().__init__(**kwargs)

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        raise NotImplementedError(
            "CrossSourceWikiCheck requires multiple sources and a WikiCache. "
            "Use run_cross()."
        )

    def run_cross(
        self,
        sources: dict[str, pl.LazyFrame],
        *,
        wiki: Any,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        """Run every rule from ``wiki.all_rules()`` against ``sources``.

        Rules referencing tables that aren't in ``sources`` are skipped with a
        debug log (no false positives, no error). Rules with malformed
        equations are skipped with a warning.
        """
        issues: list[Issue] = []
        available = set(sources.keys())

        for rule in wiki.all_rules():
            eq = rule.equation
            if not eq:
                logger.warning(
                    "cross_source_wiki: rule '%s' has no equation; skipping", rule.name
                )
                continue

            referenced: set[str] = set()
            _collect_referenced_tables(eq.get("lhs"), referenced)
            _collect_referenced_tables(eq.get("rhs"), referenced)

            missing = referenced - available
            if missing:
                logger.debug(
                    "cross_source_wiki: rule '%s' skipped — missing sources: %s",
                    rule.name,
                    sorted(missing),
                )
                continue

            try:
                violations_lf = evaluate_equation(
                    eq, sources, entity_col, snapshot_col
                )
                violations = violations_lf.collect()
            except (ValueError, KeyError) as exc:
                logger.warning(
                    "cross_source_wiki: rule '%s' has malformed equation (%s); skipping",
                    rule.name,
                    exc,
                )
                continue

            lhs_node = eq.get("lhs", {}) or {}
            field_name = lhs_node.get("field") if isinstance(lhs_node, dict) else None

            for row in violations.iter_rows(named=True):
                issues.append(
                    Issue(
                        check_name=self.name,
                        severity=self.severity,
                        entity_id=str(row[entity_col]),
                        field_name=field_name,
                        snapshot_date=row[snapshot_col],
                        description=(
                            f"Rule '{rule.name}' violated: "
                            f"lhs={row['__lhs__']:.4f}, rhs={row['__rhs__']:.4f}, "
                            f"diff={row['__diff__']:.4f}"
                        ),
                        details={
                            "lhs": float(row["__lhs__"]),
                            "rhs": float(row["__rhs__"]),
                            "diff": float(row["__diff__"]),
                            "rule": rule.name,
                        },
                        confidence=1.0,
                        detector_source=f"cross_source_wiki:{rule.name}",
                    )
                )

        return CheckResult(
            check_name=self.name, passed=len(issues) == 0, issues=issues
        )
