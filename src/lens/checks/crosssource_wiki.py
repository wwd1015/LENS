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

from lens.checks import equation
from lens.checks.base import BaseCheck
from lens.checks.equation import evaluate_equation
from lens.checks.registry import registry
from lens.types import CheckResult, Issue, Severity

logger = logging.getLogger(__name__)

# Above this many one-sided-null violations on a single snapshot date, a rule
# emits one aggregated coverage-gap Issue for that date instead of one Issue
# per entity — a late-arriving snapshot must not fan out into millions of
# per-entity findings.
_NULL_MISMATCH_CAP = 50


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
            "CrossSourceWikiCheck requires multiple sources and a WikiCache. Use run_cross()."
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
                logger.warning("cross_source_wiki: rule '%s' has no equation; skipping", rule.name)
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
                violations_lf = evaluate_equation(eq, sources, entity_col, snapshot_col)
                violations = violations_lf.collect()
            except (ValueError, KeyError, pl.exceptions.PolarsError) as exc:
                # PolarsError covers execution-time failures too (a rule
                # referencing a field the table doesn't have raises
                # ColumnNotFoundError at collect) — one bad rule must skip,
                # not disable every other rule in the run.
                logger.warning(
                    "cross_source_wiki: rule '%s' failed to evaluate (%s); skipping",
                    rule.name,
                    exc,
                )
                continue

            if violations.height == 0:
                # No breaches — skip the operand-breakdown work entirely.
                continue

            lhs_node = eq.get("lhs", {}) or {}
            field_name = lhs_node.get("field") if isinstance(lhs_node, dict) else None

            # A systematic coverage gap (one table's snapshot arrives late, a
            # grain mismatch) turns EVERY entity into a null-mismatch row
            # under the full join. Cap the per-row fan-out: dates whose
            # null-mismatch count exceeds the cap collapse into one
            # coverage-gap Issue instead of one Issue per entity.
            null_expr = pl.col("__lhs__").is_null() | pl.col("__rhs__").is_null()
            flooded_dates: dict[Any, int] = {}
            null_counts = (
                violations.filter(null_expr).group_by(snapshot_col).len().iter_rows(named=True)
            )
            for crow in null_counts:
                if crow["len"] > _NULL_MISMATCH_CAP:
                    flooded_dates[crow[snapshot_col]] = crow["len"]
            if flooded_dates:
                per_row = violations.filter(
                    ~(null_expr & pl.col(snapshot_col).is_in(list(flooded_dates)))
                )
            else:
                per_row = violations

            # Operand breakdown — the actual values feeding the rhs, so a
            # reader can replay the math (e.g. 2,905,000 × 0.75 = 2,178,750).
            # Bounded to the violating keys via a semi-join, not the whole
            # source universe.
            term_lookup, term_labels, op_symbol = self._term_breakdown(
                eq,
                sources,
                entity_col,
                snapshot_col,
                keys=per_row.lazy().select(entity_col, snapshot_col),
            )
            lhs_label = equation.node_label(lhs_node)
            rhs_label = equation.node_label(eq.get("rhs", {}) or {})
            formula = equation.equation_formula(eq)

            def _fmt(value: float | None) -> str:
                return "missing" if value is None else f"{value:.4f}"

            for snap, count in flooded_dates.items():
                sample = (
                    violations.filter(null_expr & (pl.col(snapshot_col) == snap))
                    .head(10)[entity_col]
                    .to_list()
                )
                issues.append(
                    Issue(
                        check_name=self.name,
                        severity=self.severity,
                        entity_id=None,
                        field_name=field_name,
                        snapshot_date=snap,
                        description=(
                            f"Rule '{rule.name}': coverage gap — {count} entities "
                            f"have one-sided missing/null values on this snapshot"
                        ),
                        details={
                            "rule": rule.name,
                            "formula": formula,
                            "null_mismatch": True,
                            "coverage_gap": True,
                            "null_mismatch_count": count,
                            "sample_entities": [str(e) for e in sample],
                            "score": 1.0,
                            "tolerance": eq.get("tolerance"),
                            "tolerance_type": eq.get("tolerance_type"),
                        },
                        confidence=1.0,
                        detector_source=f"cross_source_wiki:{rule.name}",
                    )
                )

            for row in per_row.iter_rows(named=True):
                key = (row[entity_col], row[snapshot_col])
                term_values = term_lookup.get(key, [])
                lhs_v, rhs_v, diff_v = row["__lhs__"], row["__rhs__"], row["__diff__"]
                null_mismatch = lhs_v is None or rhs_v is None
                if null_mismatch:
                    missing_side = "lhs" if lhs_v is None else "rhs"
                    desc = (
                        f"Rule '{rule.name}' violated: {missing_side} is missing/null "
                        f"(lhs={_fmt(lhs_v)}, rhs={_fmt(rhs_v)})"
                    )
                else:
                    desc = (
                        f"Rule '{rule.name}' violated: "
                        f"lhs={_fmt(lhs_v)}, rhs={_fmt(rhs_v)}, diff={_fmt(diff_v)}"
                    )
                details: dict[str, Any] = {
                    "lhs": float(lhs_v) if lhs_v is not None else None,
                    "rhs": float(rhs_v) if rhs_v is not None else None,
                    "diff": float(diff_v) if diff_v is not None else None,
                    "rule": rule.name,
                }
                if null_mismatch:
                    # One side absent entirely — score as a full (100%)
                    # relative break so the orchestrator maps it to CRITICAL.
                    details["null_mismatch"] = True
                    details["score"] = 1.0
                issues.append(
                    Issue(
                        check_name=self.name,
                        severity=self.severity,
                        entity_id=str(row[entity_col]),
                        field_name=field_name,
                        snapshot_date=row[snapshot_col],
                        description=desc,
                        details={
                            **details,
                            "formula": formula,
                            "lhs_label": lhs_label,
                            "rhs_label": rhs_label,
                            "rhs_op_symbol": op_symbol,
                            "terms": [
                                {"label": lbl, "value": val}
                                for lbl, val in zip(term_labels, term_values)
                            ],
                            "tolerance": eq.get("tolerance"),
                            "tolerance_type": eq.get("tolerance_type"),
                        },
                        confidence=1.0,
                        detector_source=f"cross_source_wiki:{rule.name}",
                    )
                )

        return CheckResult(check_name=self.name, passed=len(issues) == 0, issues=issues)

    @staticmethod
    def _term_breakdown(
        eq: dict[str, Any],
        sources: dict[str, pl.LazyFrame],
        entity_col: str,
        snapshot_col: str,
        keys: pl.LazyFrame | None = None,
    ) -> tuple[dict[tuple[Any, Any], list[float | None]], list[str], str | None]:
        """Materialize the rhs operand values keyed by ``(entity, snapshot)``.

        ``keys`` (the violating rows' key columns) bounds the work via a
        semi-join — one breach in a 5M-row portfolio must not materialize 5M
        term rows to serve one lookup.

        Best-effort: any failure yields an empty lookup so the rest of the
        finding (lhs/rhs/diff) still renders. Returns
        ``(lookup, labels, op_symbol)``. Null operand values (possible for
        null-mismatch violations) stay ``None`` in the lookup.
        """
        try:
            labels, op_symbol, terms_lf = equation.evaluate_terms(
                eq, sources, entity_col, snapshot_col
            )
            if keys is not None:
                terms_lf = terms_lf.join(keys, on=[entity_col, snapshot_col], how="semi")
            terms_df = terms_lf.collect()
            term_cols = [c for c in terms_df.columns if c.startswith("__term")]
            lookup: dict[tuple[Any, Any], list[float | None]] = {}
            for r in terms_df.iter_rows(named=True):
                lookup[(r[entity_col], r[snapshot_col])] = [
                    float(r[c]) if r[c] is not None else None for c in term_cols
                ]
        except Exception as exc:  # noqa: BLE001 - breakdown is non-essential
            logger.debug("cross_source_wiki: term breakdown unavailable: %s", exc)
            return {}, [], None
        return lookup, labels, op_symbol
