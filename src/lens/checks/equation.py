"""Structured-equation evaluator for wiki-driven cross-source rules.

A rule's `equation` frontmatter is a tree of nodes:

  - leaf:     ``{table, field, agg, group_by}``
              ``agg`` ∈ {None, "sum", "min", "max", "mean"}
              ``group_by`` is an alternative grouping column (or None to fall
              back to ``entity_col``).
  - compound: ``{op, args: [node, node]}`` where ``op`` ∈ {add, sub, mul, div}.

This module turns that tree into Polars expressions. **It deliberately never
imports ``ast`` and never calls ``eval``** — the equation arrives already
structured, so no expression-string parsing is required.
"""

from __future__ import annotations

from typing import Any

import polars as pl

_VALID_OPS = {"add", "sub", "mul", "div"}
_VALID_AGGS: dict[str | None, str | None] = {
    None: None,
    "sum": "sum",
    "min": "min",
    "max": "max",
    "mean": "mean",
}


def _is_leaf(node: dict[str, Any]) -> bool:
    return "table" in node and "field" in node


def _evaluate_leaf(
    node: dict[str, Any],
    sources: dict[str, pl.LazyFrame],
    entity_col: str,
    snapshot_col: str,
) -> pl.LazyFrame:
    table = node["table"]
    field = node["field"]
    agg = node.get("agg")
    group_by = node.get("group_by")

    if agg not in _VALID_AGGS:
        raise ValueError(
            f"Unknown agg '{agg}' for leaf {table}.{field}. "
            f"Valid aggs: {sorted(k for k in _VALID_AGGS if k is not None)} or null."
        )

    if table not in sources:
        raise KeyError(f"Source '{table}' not found in sources dict")

    lf = sources[table]

    if agg is None:
        # No aggregation: pull the field directly. The frame is assumed to
        # already be at the (entity, snapshot) grain.
        return lf.select(
            pl.col(entity_col),
            pl.col(snapshot_col),
            pl.col(field).cast(pl.Float64).alias("__value__"),
        )

    # Aggregated leaf: collapse by (group_key, snapshot_col) where
    # group_key defaults to entity_col when group_by is null.
    group_key = group_by or entity_col

    agg_expr_map = {
        "sum": pl.col(field).sum(),
        "min": pl.col(field).min(),
        "max": pl.col(field).max(),
        "mean": pl.col(field).mean(),
    }
    agg_expr = agg_expr_map[agg]

    grouped = lf.group_by([group_key, snapshot_col]).agg(
        agg_expr.cast(pl.Float64).alias("__value__")
    )

    # The aggregated frame's entity column is `group_key`; rename to entity_col
    # so the caller sees the canonical 3-column shape.
    if group_key != entity_col:
        grouped = grouped.rename({group_key: entity_col})

    return grouped.select(entity_col, snapshot_col, "__value__")


def _apply_op(op: str, a: pl.Expr, b: pl.Expr) -> pl.Expr:
    if op == "add":
        return a + b
    if op == "sub":
        return a - b
    if op == "mul":
        return a * b
    if op == "div":
        return a / b
    raise ValueError(f"Unknown op '{op}'. Valid ops: {sorted(_VALID_OPS)}")


def evaluate_node(
    node: dict[str, Any],
    sources: dict[str, pl.LazyFrame],
    entity_col: str,
    snapshot_col: str,
) -> pl.LazyFrame:
    """Evaluate one node of an equation tree.

    Returns a LazyFrame with exactly three columns:
    ``[entity_col, snapshot_col, "__value__"]``.
    """
    if not isinstance(node, dict):
        raise ValueError(f"Equation node must be a dict, got {type(node).__name__}")

    if _is_leaf(node):
        return _evaluate_leaf(node, sources, entity_col, snapshot_col)

    # Compound node.
    op = node.get("op")
    args = node.get("args")

    if op not in _VALID_OPS:
        raise ValueError(
            f"Unknown op '{op}' in compound node. Valid ops: {sorted(_VALID_OPS)}"
        )
    if not isinstance(args, list) or len(args) != 2:
        raise ValueError(
            f"Compound node with op '{op}' must have exactly 2 args, got {args!r}"
        )

    left = evaluate_node(args[0], sources, entity_col, snapshot_col)
    right = evaluate_node(args[1], sources, entity_col, snapshot_col)

    left_r = left.rename({"__value__": "__a__"})
    right_r = right.rename({"__value__": "__b__"})

    joined = left_r.join(right_r, on=[entity_col, snapshot_col], how="inner")
    return joined.select(
        pl.col(entity_col),
        pl.col(snapshot_col),
        _apply_op(op, pl.col("__a__"), pl.col("__b__")).alias("__value__"),
    )


def evaluate_equation(
    eq: dict[str, Any],
    sources: dict[str, pl.LazyFrame],
    entity_col: str,
    snapshot_col: str,
) -> pl.LazyFrame:
    """Evaluate a full equation spec and return only the violating rows.

    Output columns: ``[entity_col, snapshot_col, "__lhs__", "__rhs__", "__diff__"]``.
    A row is included iff ``__diff__ > eq["tolerance"]`` per the configured
    ``tolerance_type``.
    """
    required = {"lhs", "rhs", "tolerance", "tolerance_type"}
    missing = required - set(eq or {})
    if missing:
        raise ValueError(
            f"Equation spec is missing required keys: {sorted(missing)}"
        )

    tolerance = eq["tolerance"]
    tol_type = eq["tolerance_type"]
    if tol_type not in ("absolute", "relative"):
        raise ValueError(
            f"tolerance_type must be 'absolute' or 'relative', got {tol_type!r}"
        )

    lhs_lf = evaluate_node(eq["lhs"], sources, entity_col, snapshot_col).rename(
        {"__value__": "__lhs__"}
    )
    rhs_lf = evaluate_node(eq["rhs"], sources, entity_col, snapshot_col).rename(
        {"__value__": "__rhs__"}
    )

    joined = lhs_lf.join(rhs_lf, on=[entity_col, snapshot_col], how="inner")

    lhs_e = pl.col("__lhs__").cast(pl.Float64)
    rhs_e = pl.col("__rhs__").cast(pl.Float64)
    abs_diff = (lhs_e - rhs_e).abs()

    if tol_type == "absolute":
        diff_expr = abs_diff
    else:
        # relative — divide by |lhs|; guard against zero lhs by treating
        # 0-vs-nonzero as an infinite relative diff (always flagged).
        diff_expr = pl.when(lhs_e == 0).then(
            pl.when(rhs_e == 0).then(0.0).otherwise(float("inf"))
        ).otherwise(abs_diff / lhs_e.abs())

    with_diff = joined.with_columns(diff_expr.alias("__diff__"))
    violations = with_diff.filter(pl.col("__diff__") > tolerance)
    return violations.select(
        entity_col, snapshot_col, "__lhs__", "__rhs__", "__diff__"
    )
