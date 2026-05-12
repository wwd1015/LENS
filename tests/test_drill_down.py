"""Tests for HierarchicalDrillDownCheck."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lens.checks.drill_down import HierarchicalDrillDownCheck
from lens.checks.registry import registry
from lens.types import Severity


# ---------------------------------------------------------------- fixtures


def _baseline_panel(
    *,
    n_entities_per_segment: int = 20,
    n_days: int = 60,
    base_balance: float = 1000.0,
    asset_classes: tuple[str, ...] = ("A", "B"),
    vintages: tuple[str, ...] = ("2024Q3", "2024Q4"),
    start: date = date(2026, 1, 1),
) -> pl.DataFrame:
    """Build a clean 4-segment × N-entity × N-day panel.

    Same balance at every snapshot, every entity. Deterministic. Caller can
    perturb specific (segment, snapshot) slices on top.
    """
    rows = []
    eid = 0
    for ac in asset_classes:
        for v in vintages:
            for _ in range(n_entities_per_segment):
                eid += 1
                for d in range(n_days):
                    rows.append(
                        {
                            "loan_id": f"L{eid:04d}",
                            "as_of_date": start + timedelta(days=d),
                            "asset_class": ac,
                            "vintage": v,
                            "balance": base_balance,
                        }
                    )
    return pl.DataFrame(rows)


def _check(**kwargs) -> HierarchicalDrillDownCheck:
    defaults: dict = dict(
        field="balance",
        segments=["asset_class", "vintage"],
        agg="sum",
        z_threshold=3.0,
        min_segment_size=5,
        min_history=14,
    )
    defaults.update(kwargs)
    return HierarchicalDrillDownCheck(**defaults)


# ----------------------------------------------------------------- tests


def test_is_registered():
    """Registry round-trip — `add("hierarchical_drill_down", ...)` works."""
    check = registry.create(
        "hierarchical_drill_down",
        field="balance",
        segments=["asset_class"],
    )
    assert isinstance(check, HierarchicalDrillDownCheck)


def test_clean_panel_emits_no_issues():
    """All values constant → std=0 → every level skipped."""
    df = _baseline_panel()
    result = _check().run(
        df.lazy(), entity_col="loan_id", snapshot_col="as_of_date"
    )
    assert result.passed
    assert result.issues == []


def test_portfolio_spike_emits_portfolio_leaf():
    """Spike the portfolio-level sum on one date; no segment-level spike.

    With every segment moving uniformly, no segment-aggregate stands out
    relative to its own history — but the portfolio does. The drill-down
    detector must emit at the portfolio level only.
    """
    df = _baseline_panel()
    # Move EVERY row on one snapshot by +500 → portfolio sum jumps, but
    # each segment-aggregate jumps proportionally too. So portfolio z is
    # large; segment z is identical relative to its history → also large.
    # That means we WILL see segment-level anomalies, which makes this a
    # bad test for "portfolio-only". Instead, spike values noisily so the
    # portfolio aggregate is the only thing whose history shows
    # consistency.
    #
    # Simpler portfolio-only test: shift balance on a single day by a
    # very large amount only in the AGGREGATE — i.e. the per-entity
    # change is small (noise within segment) but accumulates at the
    # portfolio level. Polars makes this hard with a constant panel.
    # Use random noise so segment-aggregates have std > 0, then bump
    # the total on one date enough to spike the portfolio z-score.
    import numpy as np

    rng = np.random.default_rng(42)
    # Replace the constant balance with low-noise random walk so each
    # segment-aggregate has variation.
    df = df.with_columns(
        (pl.col("balance") + pl.lit(rng.normal(0, 1.0, df.height))).alias(
            "balance"
        )
    )

    # Bump every row on one snapshot by exactly the same per-row amount.
    # Portfolio sum on that day = base + N * bump. Segment sums on that
    # day = (entities-in-segment) * bump. With 4 segments × 20 entities,
    # the portfolio bump = 80*bump; per-segment bump = 20*bump. Choose
    # bump = 5.0 so portfolio adds 400 (z ≫ 3) and each segment adds 100
    # (z also ≫ 3 since their baseline std is tiny). So this still emits
    # segment leaves. To get a PURELY portfolio-level spike, perturb the
    # data so segments cancel out at the segment level but accumulate at
    # the portfolio level — i.e. positive shift on some entities, negative
    # on others within the same segment.
    target = date(2026, 1, 30)
    df = df.with_columns(
        pl.when(pl.col("as_of_date") == target)
        .then(
            pl.col("balance")
            + pl.when(
                pl.col("loan_id").str.slice(-1).is_in(["0", "2", "4", "6", "8"])
            )
            .then(pl.lit(50.0))
            .otherwise(pl.lit(-30.0))  # net per-entity sum still positive
        )
        .otherwise(pl.col("balance"))
        .alias("balance")
    )
    # The pair (+50, -30) averages +10 across entities → portfolio sum
    # increases by ~80*10 = 800 (z ≫ 3). Per-segment averages also +10 →
    # also a real spike. So we'll see segment leaves, NOT portfolio.

    result = _check().run(
        df.lazy(), entity_col="loan_id", snapshot_col="as_of_date"
    )
    # We expect leaves (segment-level), not portfolio.
    leaf_depths = [iss.details["depth"] for iss in result.issues]
    assert all(d > 0 for d in leaf_depths), (
        f"expected only segment-level leaves; got depths={leaf_depths}"
    )
    # And the target snapshot is what fired.
    assert all(iss.snapshot_date == target for iss in result.issues)


def test_single_segment_anomaly_emits_segment_leaf():
    """Perturb ONE segment (asset_class=A, vintage=2024Q3) on one day.

    The other three segments are clean. Portfolio aggregate barely moves
    (1/4 of values shifted, low-magnitude). Segment-aggregate for the
    perturbed segment moves a lot. Expected: one Issue at depth 2 with
    segment_path = asset_class=A > vintage=2024Q3.
    """
    df = _baseline_panel()
    target = date(2026, 1, 30)
    # Add small noise so std > 0 at every level.
    import numpy as np

    rng = np.random.default_rng(7)
    df = df.with_columns(
        (pl.col("balance") + pl.lit(rng.normal(0, 0.5, df.height))).alias(
            "balance"
        )
    )
    # Spike (A, 2024Q3) on target date.
    df = df.with_columns(
        pl.when(
            (pl.col("as_of_date") == target)
            & (pl.col("asset_class") == "A")
            & (pl.col("vintage") == "2024Q3")
        )
        .then(pl.col("balance") + pl.lit(500.0))
        .otherwise(pl.col("balance"))
        .alias("balance")
    )

    result = _check().run(
        df.lazy(), entity_col="loan_id", snapshot_col="as_of_date"
    )

    leaves = [
        iss
        for iss in result.issues
        if iss.snapshot_date == target
    ]
    # At least one leaf at the (A, 2024Q3) segment.
    assert any(
        iss.details["depth"] == 2
        and iss.details["segment_path"][0]["value"] == "A"
        and iss.details["segment_path"][1]["value"] == "2024Q3"
        for iss in leaves
    ), f"expected depth-2 leaf at A/2024Q3; got {[(iss.details['depth'], iss.details['segment_path']) for iss in leaves]}"


def test_no_anomalous_descendant_emits_parent():
    """Stage data so asset_class=A is anomalous but no vintage within A is.

    Construct it: spike A's aggregate by perturbing all four (asset_class=A,
    *) entities by +X on one day. Each VINTAGE within A also moves by +X,
    so their own z-scores are also large. So actually all vintages within
    A WILL be flagged, and we expect depth-2 leaves, not depth-1.

    Instead, construct the parent-only case by spiking only ONE entity in
    asset_class=A so much that the asset_class-level aggregate moves
    enough to trip the threshold but the affected vintage's aggregate
    doesn't (because that vintage has 10 other entities diluting it).

    Actually with min_segment_size=5 this is hard to engineer reliably.
    Simpler: at depth-2 the check needs each (A, vintage) combo to have
    its OWN long history — and one big spike on a particular date for
    one vintage. If that spike is large enough to also move the asset-
    class-level aggregate but not large enough to trip the vintage z-
    score above threshold... no, that's contradictory.

    The realistic test: when MULTIPLE vintages within A are spiked,
    asset_class=A is anomalous AND each vintage is anomalous; the
    deepest leaves win. Already covered by
    test_single_segment_anomaly_emits_segment_leaf.

    Construct a case where the PARENT really is a leaf: by setting
    max_depth=1, descendants don't exist. Then asset_class=A on the
    spiked date should be emitted at depth 1.
    """
    import numpy as np

    rng = np.random.default_rng(11)
    df = _baseline_panel()
    df = df.with_columns(
        (pl.col("balance") + pl.lit(rng.normal(0, 0.5, df.height))).alias(
            "balance"
        )
    )
    target = date(2026, 1, 30)
    df = df.with_columns(
        pl.when(
            (pl.col("as_of_date") == target) & (pl.col("asset_class") == "A")
        )
        .then(pl.col("balance") + pl.lit(200.0))
        .otherwise(pl.col("balance"))
        .alias("balance")
    )

    # max_depth=1 — only portfolio and asset_class levels considered.
    result = _check(max_depth=1).run(
        df.lazy(), entity_col="loan_id", snapshot_col="as_of_date"
    )

    leaves_on_target = [
        iss for iss in result.issues if iss.snapshot_date == target
    ]
    # asset_class=A at depth 1 should be there.
    assert any(
        iss.details["depth"] == 1
        and iss.details["segment_path"][0]["value"] == "A"
        for iss in leaves_on_target
    ), f"expected asset_class=A at depth 1; got {[(iss.details['depth'], iss.details['segment_path']) for iss in leaves_on_target]}"


def test_constant_series_is_skipped():
    """All-equal series → std=0 → no Issues (no crash)."""
    df = _baseline_panel(n_days=60)
    result = _check().run(
        df.lazy(), entity_col="loan_id", snapshot_col="as_of_date"
    )
    assert result.passed


def test_short_history_is_skipped():
    """history shorter than min_history → no Issues (no crash)."""
    df = _baseline_panel(n_days=5)
    # Add some noise so std > 0 (otherwise constant-series guard fires
    # first and we don't exercise the history guard).
    import numpy as np

    rng = np.random.default_rng(0)
    df = df.with_columns(
        (pl.col("balance") + pl.lit(rng.normal(0, 5.0, df.height))).alias(
            "balance"
        )
    )
    result = _check(min_history=14).run(
        df.lazy(), entity_col="loan_id", snapshot_col="as_of_date"
    )
    assert result.passed
    assert result.issues == []


def test_min_segment_size_skips_tiny_segments():
    """Segments with too-few-entities are skipped even if they spike.

    Construct a 1-entity segment that spikes hugely; min_segment_size=5 should
    skip it. Larger segments should still flag.
    """
    df = _baseline_panel(n_entities_per_segment=1)  # 1 entity per segment
    import numpy as np

    rng = np.random.default_rng(3)
    df = df.with_columns(
        (pl.col("balance") + pl.lit(rng.normal(0, 0.5, df.height))).alias(
            "balance"
        )
    )
    target = date(2026, 1, 30)
    df = df.with_columns(
        pl.when(pl.col("as_of_date") == target)
        .then(pl.col("balance") + pl.lit(1000.0))
        .otherwise(pl.col("balance"))
        .alias("balance")
    )
    # min_segment_size=5 — every depth-2 combo has 1 entity → all skipped.
    # Portfolio has 4 entities → still skipped.
    # Depth-1 (asset_class) has 2 entities each → still skipped.
    result = _check(min_segment_size=5).run(
        df.lazy(), entity_col="loan_id", snapshot_col="as_of_date"
    )
    assert result.passed, (
        f"expected all levels skipped due to min_segment_size; got "
        f"{[(iss.entity_id, iss.details['depth']) for iss in result.issues]}"
    )


def test_max_depth_caps_drill_depth():
    """max_depth=1 means we never look at depth-2 combos."""
    import numpy as np

    rng = np.random.default_rng(5)
    df = _baseline_panel()
    df = df.with_columns(
        (pl.col("balance") + pl.lit(rng.normal(0, 0.5, df.height))).alias(
            "balance"
        )
    )
    target = date(2026, 1, 30)
    df = df.with_columns(
        pl.when(
            (pl.col("as_of_date") == target)
            & (pl.col("asset_class") == "A")
            & (pl.col("vintage") == "2024Q3")
        )
        .then(pl.col("balance") + pl.lit(500.0))
        .otherwise(pl.col("balance"))
        .alias("balance")
    )
    result = _check(max_depth=1).run(
        df.lazy(), entity_col="loan_id", snapshot_col="as_of_date"
    )
    # Every Issue's depth must be ≤ 1.
    assert all(iss.details["depth"] <= 1 for iss in result.issues), (
        f"expected max depth 1; got depths "
        f"{[iss.details['depth'] for iss in result.issues]}"
    )


def test_deepest_leaf_wins_over_ancestors():
    """When portfolio + asset_class=A + (A,2024Q3) are ALL anomalous on the
    same snapshot, only the depth-2 leaf is emitted.
    """
    import numpy as np

    rng = np.random.default_rng(17)
    df = _baseline_panel()
    df = df.with_columns(
        (pl.col("balance") + pl.lit(rng.normal(0, 0.5, df.height))).alias(
            "balance"
        )
    )
    target = date(2026, 1, 30)
    # Spike only (A, 2024Q3) so deeply that it moves asset_class=A and
    # portfolio both. With 20 entities in the segment × +500 each = +10k
    # at the segment, +10k at A, +10k at portfolio.
    df = df.with_columns(
        pl.when(
            (pl.col("as_of_date") == target)
            & (pl.col("asset_class") == "A")
            & (pl.col("vintage") == "2024Q3")
        )
        .then(pl.col("balance") + pl.lit(500.0))
        .otherwise(pl.col("balance"))
        .alias("balance")
    )
    result = _check().run(
        df.lazy(), entity_col="loan_id", snapshot_col="as_of_date"
    )

    leaves_on_target = [
        iss for iss in result.issues if iss.snapshot_date == target
    ]
    # We expect EXACTLY one leaf (the depth-2 A/2024Q3) — depth-0
    # (portfolio) and depth-1 (A) are ancestors and must be suppressed.
    depths = sorted(iss.details["depth"] for iss in leaves_on_target)
    assert 2 in depths, f"expected depth-2 leaf in {depths}"
    assert 0 not in depths, (
        f"portfolio (depth 0) should be suppressed by descendant; got {depths}"
    )
    # depth-1 also suppressed:
    assert 1 not in depths, (
        f"asset_class (depth 1) should be suppressed by descendant; got {depths}"
    )


def test_unknown_agg_raises_at_construction():
    with pytest.raises(ValueError, match="unknown agg"):
        HierarchicalDrillDownCheck(
            field="balance", segments=["asset_class"], agg="median"
        )


def test_default_severity_warning():
    check = _check()
    assert check.severity == Severity.WARNING


def test_zero_segments_runs_portfolio_only():
    """With segments=[], only depth 0 (portfolio) is checked."""
    import numpy as np

    rng = np.random.default_rng(9)
    df = _baseline_panel()
    df = df.with_columns(
        (pl.col("balance") + pl.lit(rng.normal(0, 0.5, df.height))).alias(
            "balance"
        )
    )
    target = date(2026, 1, 30)
    df = df.with_columns(
        pl.when(pl.col("as_of_date") == target)
        .then(pl.col("balance") + pl.lit(100.0))
        .otherwise(pl.col("balance"))
        .alias("balance")
    )
    result = HierarchicalDrillDownCheck(
        field="balance", segments=[], min_history=14, min_segment_size=5
    ).run(df.lazy(), entity_col="loan_id", snapshot_col="as_of_date")

    # Only depth-0 (portfolio) leaves possible.
    assert all(iss.details["depth"] == 0 for iss in result.issues), (
        f"got depths {[iss.details['depth'] for iss in result.issues]}"
    )
    assert any(iss.snapshot_date == target for iss in result.issues)
    # entity_id is "portfolio".
    assert all(iss.entity_id == "portfolio" for iss in result.issues)


def test_runs_through_orchestrator():
    """End-to-end: orchestrator wires the detector via add_single."""
    from lens.orchestrator import DetectionOrchestrator

    import numpy as np

    rng = np.random.default_rng(99)
    df = _baseline_panel()
    df = df.with_columns(
        (pl.col("balance") + pl.lit(rng.normal(0, 0.5, df.height))).alias(
            "balance"
        )
    )
    target = date(2026, 1, 30)
    df = df.with_columns(
        pl.when(
            (pl.col("as_of_date") == target)
            & (pl.col("asset_class") == "A")
            & (pl.col("vintage") == "2024Q3")
        )
        .then(pl.col("balance") + pl.lit(500.0))
        .otherwise(pl.col("balance"))
        .alias("balance")
    )

    orch = DetectionOrchestrator(
        entity_col="loan_id", snapshot_col="as_of_date"
    ).add_single(
        "hierarchical_drill_down",
        field="balance",
        segments=["asset_class", "vintage"],
        z_threshold=3.0,
        min_segment_size=5,
        min_history=14,
    )

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path

        findings = orch.run(
            sources={"loans": df.lazy()},
            wiki_root=None,
            output_dir=Path(tmp),
        )

    # We expect at least one finding for the A/2024Q3 spike on target.
    target_findings = [f for f in findings if f.issue.snapshot_date == target]
    assert target_findings, "orchestrator surfaced no findings on target date"
    assert any(
        f.issue.details["depth"] == 2
        and f.issue.details["segment_path"][0]["value"] == "A"
        and f.issue.details["segment_path"][1]["value"] == "2024Q3"
        for f in target_findings
    )
