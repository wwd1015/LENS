"""Tests for the wiki-driven cross-source detector and its equation evaluator."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from lens.checks.crosssource_wiki import CrossSourceWikiCheck
from lens.checks.equation import evaluate_equation, evaluate_node
from lens.wiki.cache import WikiCache

FIXTURE_WIKI = Path(__file__).parent / "fixtures" / "wiki_sample"


# ---------------------------------------------------------------------------
# Source-frame helpers
# ---------------------------------------------------------------------------


def _make_consistent_sources() -> dict[str, pl.LazyFrame]:
    """Build (loan_pool, deal_terms, senior_debt) frames where the equation
    senior_debt.balance == sum(loan_pool.balance per deal) * advance_rate
    holds exactly for every (deal_id, snapshot_date) pair.
    """
    snap1 = date(2026, 1, 31)
    snap2 = date(2026, 2, 28)

    # Two deals, two snapshots.
    loan_pool = pl.LazyFrame(
        {
            "deal_id": [
                "d1", "d1", "d1",  # snap1: 3 loans
                "d2", "d2",        # snap1: 2 loans
                "d1", "d1",        # snap2: 2 loans
                "d2", "d2", "d2",  # snap2: 3 loans
            ],
            "snapshot_date": [
                snap1, snap1, snap1,
                snap1, snap1,
                snap2, snap2,
                snap2, snap2, snap2,
            ],
            "balance": [
                100.0, 200.0, 300.0,   # d1@snap1 sum=600
                400.0, 500.0,          # d2@snap1 sum=900
                250.0, 250.0,          # d1@snap2 sum=500
                100.0, 200.0, 300.0,   # d2@snap2 sum=600
            ],
        }
    )

    deal_terms = pl.LazyFrame(
        {
            "deal_id": ["d1", "d2", "d1", "d2"],
            "snapshot_date": [snap1, snap1, snap2, snap2],
            "advance_rate": [0.8, 0.7, 0.8, 0.75],
        }
    )

    senior_debt = pl.LazyFrame(
        {
            "deal_id": ["d1", "d2", "d1", "d2"],
            "snapshot_date": [snap1, snap1, snap2, snap2],
            # 600*0.8, 900*0.7, 500*0.8, 600*0.75
            "balance": [480.0, 630.0, 400.0, 450.0],
        }
    )

    return {
        "loan_pool": loan_pool,
        "deal_terms": deal_terms,
        "senior_debt": senior_debt,
    }


# ---------------------------------------------------------------------------
# evaluate_node unit tests
# ---------------------------------------------------------------------------


def test_evaluate_node_leaf_without_agg_returns_canonical_three_columns():
    sources = _make_consistent_sources()
    node = {"table": "senior_debt", "field": "balance", "agg": None, "group_by": None}
    out = evaluate_node(node, sources, entity_col="deal_id", snapshot_col="snapshot_date").collect()
    assert out.columns == ["deal_id", "snapshot_date", "__value__"]
    # Four (deal, snapshot) pairs
    assert out.height == 4
    rows = {(r["deal_id"], r["snapshot_date"]): r["__value__"] for r in out.iter_rows(named=True)}
    assert rows[("d1", date(2026, 1, 31))] == 480.0
    assert rows[("d2", date(2026, 2, 28))] == 450.0


def test_evaluate_node_leaf_with_sum_by_group():
    sources = _make_consistent_sources()
    node = {
        "table": "loan_pool",
        "field": "balance",
        "agg": "sum",
        "group_by": "deal_id",
    }
    out = evaluate_node(node, sources, entity_col="deal_id", snapshot_col="snapshot_date").collect()
    assert out.columns == ["deal_id", "snapshot_date", "__value__"]
    rows = {(r["deal_id"], r["snapshot_date"]): r["__value__"] for r in out.iter_rows(named=True)}
    assert rows[("d1", date(2026, 1, 31))] == 600.0
    assert rows[("d2", date(2026, 1, 31))] == 900.0
    assert rows[("d1", date(2026, 2, 28))] == 500.0
    assert rows[("d2", date(2026, 2, 28))] == 600.0


def test_evaluate_node_compound_mul():
    sources = _make_consistent_sources()
    node = {
        "op": "mul",
        "args": [
            {"table": "loan_pool", "field": "balance", "agg": "sum", "group_by": "deal_id"},
            {"table": "deal_terms", "field": "advance_rate", "agg": None, "group_by": None},
        ],
    }
    out = evaluate_node(node, sources, entity_col="deal_id", snapshot_col="snapshot_date").collect()
    rows = {(r["deal_id"], r["snapshot_date"]): r["__value__"] for r in out.iter_rows(named=True)}
    assert rows[("d1", date(2026, 1, 31))] == pytest.approx(480.0)
    assert rows[("d2", date(2026, 1, 31))] == pytest.approx(630.0)
    assert rows[("d1", date(2026, 2, 28))] == pytest.approx(400.0)
    assert rows[("d2", date(2026, 2, 28))] == pytest.approx(450.0)


def test_evaluate_node_compound_div():
    snap = date(2026, 1, 1)
    sources = {
        "num": pl.LazyFrame({"eid": ["a", "b"], "snap": [snap, snap], "v": [10.0, 9.0]}),
        "den": pl.LazyFrame({"eid": ["a", "b"], "snap": [snap, snap], "v": [2.0, 3.0]}),
    }
    node = {
        "op": "div",
        "args": [
            {"table": "num", "field": "v", "agg": None, "group_by": None},
            {"table": "den", "field": "v", "agg": None, "group_by": None},
        ],
    }
    out = evaluate_node(node, sources, entity_col="eid", snapshot_col="snap").collect()
    rows = {r["eid"]: r["__value__"] for r in out.iter_rows(named=True)}
    assert rows["a"] == pytest.approx(5.0)
    assert rows["b"] == pytest.approx(3.0)


def test_evaluate_node_unknown_op_raises():
    sources = _make_consistent_sources()
    node = {
        "op": "pow",  # not in {add, sub, mul, div}
        "args": [
            {"table": "loan_pool", "field": "balance", "agg": "sum", "group_by": "deal_id"},
            {"table": "deal_terms", "field": "advance_rate", "agg": None, "group_by": None},
        ],
    }
    with pytest.raises(ValueError, match="Unknown op"):
        evaluate_node(node, sources, entity_col="deal_id", snapshot_col="snapshot_date")


def test_evaluate_node_unknown_agg_raises():
    sources = _make_consistent_sources()
    node = {"table": "loan_pool", "field": "balance", "agg": "median", "group_by": "deal_id"}
    with pytest.raises(ValueError, match="Unknown agg"):
        evaluate_node(node, sources, entity_col="deal_id", snapshot_col="snapshot_date")


# ---------------------------------------------------------------------------
# evaluate_equation tests
# ---------------------------------------------------------------------------


_RULE_A_EQ: dict = {
    "lhs": {"table": "senior_debt", "field": "balance", "agg": None, "group_by": None},
    "rhs": {
        "op": "mul",
        "args": [
            {"table": "loan_pool", "field": "balance", "agg": "sum", "group_by": "deal_id"},
            {"table": "deal_terms", "field": "advance_rate", "agg": None, "group_by": None},
        ],
    },
    "tolerance": 0.001,
    "tolerance_type": "relative",
}


def test_evaluate_equation_no_violations_returns_empty_frame():
    sources = _make_consistent_sources()
    out = evaluate_equation(
        _RULE_A_EQ, sources, entity_col="deal_id", snapshot_col="snapshot_date"
    ).collect()
    assert out.columns == ["deal_id", "snapshot_date", "__lhs__", "__rhs__", "__diff__"]
    assert out.height == 0


def test_evaluate_equation_flags_relative_tolerance_break():
    # Bump d1@snap1 advance_rate from 0.8 to 0.9 → senior_debt 480 vs 600*0.9=540, 12.5% off
    sources = _make_consistent_sources()
    sources["deal_terms"] = pl.LazyFrame(
        {
            "deal_id": ["d1", "d2", "d1", "d2"],
            "snapshot_date": [
                date(2026, 1, 31), date(2026, 1, 31),
                date(2026, 2, 28), date(2026, 2, 28),
            ],
            "advance_rate": [0.9, 0.7, 0.8, 0.75],
        }
    )
    out = evaluate_equation(
        _RULE_A_EQ, sources, entity_col="deal_id", snapshot_col="snapshot_date"
    ).collect()
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["deal_id"] == "d1"
    assert row["snapshot_date"] == date(2026, 1, 31)
    assert row["__lhs__"] == pytest.approx(480.0)
    assert row["__rhs__"] == pytest.approx(540.0)


def test_evaluate_equation_absolute_tolerance():
    snap = date(2026, 1, 1)
    sources = {
        "a": pl.LazyFrame({"eid": ["x", "y"], "snap": [snap, snap], "v": [100.0, 100.0]}),
        "b": pl.LazyFrame({"eid": ["x", "y"], "snap": [snap, snap], "v": [100.5, 102.0]}),
    }
    eq = {
        "lhs": {"table": "a", "field": "v", "agg": None, "group_by": None},
        "rhs": {"table": "b", "field": "v", "agg": None, "group_by": None},
        "tolerance": 1.0,
        "tolerance_type": "absolute",
    }
    out = evaluate_equation(eq, sources, entity_col="eid", snapshot_col="snap").collect()
    # x: diff=0.5 < 1.0 → ok; y: diff=2.0 > 1.0 → violated
    assert out.height == 1
    assert out.row(0, named=True)["eid"] == "y"


def test_evaluate_equation_missing_keys_raises():
    bad = {"lhs": {"table": "x", "field": "v"}, "tolerance": 0.0}  # missing rhs + tol_type
    with pytest.raises(ValueError, match="missing required keys"):
        evaluate_equation(bad, {}, entity_col="e", snapshot_col="s")


def test_evaluate_equation_bad_tol_type_raises():
    bad = {
        "lhs": {"table": "x", "field": "v"},
        "rhs": {"table": "x", "field": "v"},
        "tolerance": 0.0,
        "tolerance_type": "fractional",  # invalid
    }
    with pytest.raises(ValueError, match="tolerance_type"):
        evaluate_equation(bad, {}, entity_col="e", snapshot_col="s")


# ---------------------------------------------------------------------------
# CrossSourceWikiCheck integration tests
# ---------------------------------------------------------------------------


def _wiki_with_only_rule_a() -> WikiCache:
    """Build a WikiCache containing only the rule_a fixture (so we don't hit
    rule_b's tables which aren't in our test source dict)."""
    full = WikiCache.from_dir(FIXTURE_WIKI)
    only_a = [r for r in full.rules if r.name == "rule_a"]
    assert only_a, "fixture missing rule_a"
    return WikiCache(datasets=full.datasets, rules=only_a, lineages=full.lineages)


def test_check_happy_path_no_violations():
    wiki = _wiki_with_only_rule_a()
    sources = _make_consistent_sources()
    result = CrossSourceWikiCheck().run_cross(
        sources, wiki=wiki, entity_col="deal_id", snapshot_col="snapshot_date"
    )
    assert result.passed is True
    assert result.issues == []
    assert result.check_name == "cross_source_wiki"


def test_check_flags_broken_equation():
    wiki = _wiki_with_only_rule_a()
    sources = _make_consistent_sources()
    # Break d2@snap2: 600 * 0.75 = 450; bump senior_debt to 500 → 11% off.
    sources["senior_debt"] = pl.LazyFrame(
        {
            "deal_id": ["d1", "d2", "d1", "d2"],
            "snapshot_date": [
                date(2026, 1, 31), date(2026, 1, 31),
                date(2026, 2, 28), date(2026, 2, 28),
            ],
            "balance": [480.0, 630.0, 400.0, 500.0],
        }
    )
    result = CrossSourceWikiCheck().run_cross(
        sources, wiki=wiki, entity_col="deal_id", snapshot_col="snapshot_date"
    )
    assert result.passed is False
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.entity_id == "d2"
    assert issue.field_name == "balance"
    assert issue.snapshot_date == date(2026, 2, 28)
    assert issue.details["rule"] == "rule_a"
    assert issue.details["lhs"] == pytest.approx(500.0)
    assert issue.details["rhs"] == pytest.approx(450.0)
    assert issue.confidence == 1.0


def test_check_detector_source_format():
    wiki = _wiki_with_only_rule_a()
    sources = _make_consistent_sources()
    sources["senior_debt"] = pl.LazyFrame(
        {
            "deal_id": ["d1"],
            "snapshot_date": [date(2026, 1, 31)],
            "balance": [999.0],  # way off from 480
        }
    )
    # Trim other sources to the matching key so the join produces exactly one row.
    sources["loan_pool"] = pl.LazyFrame(
        {
            "deal_id": ["d1", "d1", "d1"],
            "snapshot_date": [date(2026, 1, 31)] * 3,
            "balance": [100.0, 200.0, 300.0],
        }
    )
    sources["deal_terms"] = pl.LazyFrame(
        {
            "deal_id": ["d1"],
            "snapshot_date": [date(2026, 1, 31)],
            "advance_rate": [0.8],
        }
    )
    result = CrossSourceWikiCheck().run_cross(
        sources, wiki=wiki, entity_col="deal_id", snapshot_col="snapshot_date"
    )
    assert len(result.issues) == 1
    assert result.issues[0].detector_source == "cross_source_wiki:rule_a"


def test_check_run_raises_not_implemented():
    lf = pl.LazyFrame({"entity_id": ["x"], "snapshot_date": [date(2026, 1, 1)]})
    with pytest.raises(NotImplementedError):
        CrossSourceWikiCheck().run(lf)


def test_check_skips_rule_when_source_missing():
    """rule_a references deal_terms; if it's absent, the rule is skipped (no false positives)."""
    wiki = _wiki_with_only_rule_a()
    sources = _make_consistent_sources()
    del sources["deal_terms"]
    result = CrossSourceWikiCheck().run_cross(
        sources, wiki=wiki, entity_col="deal_id", snapshot_col="snapshot_date"
    )
    assert result.passed is True
    assert result.issues == []


def test_check_skips_malformed_equation_without_erroring(caplog):
    """Build a fake wiki with one rule whose equation is missing keys."""

    class _FakeRule:
        name = "broken"
        equation = {"lhs": {"table": "a", "field": "v"}, "tolerance": 0.0}  # no rhs / tol_type

    class _FakeWiki:
        def all_rules(self):
            return [_FakeRule()]

    snap = date(2026, 1, 1)
    sources = {"a": pl.LazyFrame({"deal_id": ["x"], "snapshot_date": [snap], "v": [1.0]})}

    with caplog.at_level("WARNING"):
        result = CrossSourceWikiCheck().run_cross(
            sources, wiki=_FakeWiki(), entity_col="deal_id", snapshot_col="snapshot_date"
        )
    assert result.passed is True
    assert result.issues == []
    assert any("broken" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Static safety check — the equation module must never use eval / ast parsing
# ---------------------------------------------------------------------------


def test_equation_module_uses_no_eval_or_ast_parse():
    """P0 from eng review: `eval(...)`/`ast.parse(...)`/`ast.literal_eval(...)`
    would be a code-execution surface and can't parse
    `sum(loan_pool.balance) * advance_rate` anyway. This test pins that
    invariant by looking for call sites of the dangerous builtins, plus any
    import of the `ast` module."""
    src = Path("src/lens/checks/equation.py").read_text(encoding="utf-8")
    # Match call sites only (token followed by `(`); won't trip on the docstring
    # noun "eval" or on `evaluate_*` identifiers because of the surrounding
    # word boundaries + literal `(`.
    call_pattern = re.compile(
        r"(?<![A-Za-z_])(eval|exec|ast\.parse|ast\.literal_eval)\("
    )
    import_pattern = re.compile(r"^\s*(?:import\s+ast\b|from\s+ast\s+import)", re.MULTILINE)
    call_matches = call_pattern.findall(src)
    import_matches = import_pattern.findall(src)
    assert call_matches == [], f"equation.py must not call {call_matches}"
    assert import_matches == [], f"equation.py must not import ast: {import_matches}"
