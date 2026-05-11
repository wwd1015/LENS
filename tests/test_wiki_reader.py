"""Tests for `lens.wiki.reader` and `lens.wiki.cache`."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from lens.wiki import DatasetPage, LineagePage, RulePage, WikiCache, parse_page

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "wiki_sample"


def test_parse_page_rule_returns_rule_page() -> None:
    page = parse_page(FIXTURE_ROOT / "rules" / "rule_a.md")
    assert isinstance(page, RulePage)
    assert page.name == "rule_a"
    assert "senior_debt" in page.tables
    assert "loan_pool" in page.tables
    assert "senior_debt.balance" in page.fields
    assert page.equation["lhs"]["table"] == "senior_debt"
    assert page.equation["rhs"]["op"] == "mul"


def test_parse_page_dataset_returns_dataset_page() -> None:
    page = parse_page(FIXTURE_ROOT / "datasets" / "loan_pool.md")
    assert isinstance(page, DatasetPage)
    assert page.name == "loan_pool"
    assert page.entity_grain == "loan_id"
    assert "deal_id" in page.segments


def test_parse_page_lineage_returns_lineage_page() -> None:
    page = parse_page(FIXTURE_ROOT / "lineage" / "senior_debt.lineage.md")
    assert isinstance(page, LineagePage)
    assert page.table == "senior_debt"
    assert any(u["table"] == "loan_pool" for u in page.upstream)
    assert any(d["table"] == "deal_totals" for d in page.downstream)
    assert "lens/transforms/senior_debt.sql" in page.producing_code


def test_parse_page_skips_templates() -> None:
    assert parse_page(FIXTURE_ROOT / "rules" / "_template.md") is None
    assert parse_page(FIXTURE_ROOT / "datasets" / "_template.md") is None


def test_parse_page_malformed_returns_none_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="lens.wiki.reader")
    result = parse_page(FIXTURE_ROOT / "rules" / "malformed.md")
    assert result is None
    assert any("malformed" in rec.getMessage() for rec in caplog.records)


def test_parse_page_unknown_directory_returns_none(tmp_path: Path) -> None:
    stray = tmp_path / "other" / "stray.md"
    stray.parent.mkdir()
    stray.write_text("---\nname: x\n---\n\nbody\n", encoding="utf-8")
    assert parse_page(stray) is None


def test_wiki_cache_from_dir_counts() -> None:
    cache = WikiCache.from_dir(FIXTURE_ROOT)
    # 2 datasets (templates skipped), 2 rules (template + malformed skipped),
    # 1 lineage page.
    assert len(cache.datasets) == 2
    assert len(cache.rules) == 2
    assert len(cache.lineages) == 1
    assert set(cache.datasets) == {"loan_pool", "senior_debt"}
    assert set(cache.lineages) == {"senior_debt"}


def test_wiki_cache_rules_for_field_matches_table_or_qualified_field() -> None:
    cache = WikiCache.from_dir(FIXTURE_ROOT)
    matched = cache.rules_for_field("senior_debt", "balance")
    names = sorted(r.name for r in matched)
    # rule_a lists `senior_debt` in tables AND `senior_debt.balance` in fields.
    # rule_b mentions senior_debt only inside its equation args, not in `tables`
    # or `fields`, so it must NOT match.
    assert names == ["rule_a"]


def test_wiki_cache_rules_for_field_no_match() -> None:
    cache = WikiCache.from_dir(FIXTURE_ROOT)
    assert cache.rules_for_field("nonexistent_table", "x") == []


def test_wiki_cache_dataset_lookup() -> None:
    cache = WikiCache.from_dir(FIXTURE_ROOT)
    page = cache.dataset("loan_pool")
    assert page is not None
    assert page.name == "loan_pool"
    assert cache.dataset("missing") is None


def test_wiki_cache_lineage_lookup() -> None:
    cache = WikiCache.from_dir(FIXTURE_ROOT)
    page = cache.lineage("senior_debt")
    assert page is not None
    assert page.table == "senior_debt"
    assert cache.lineage("missing_table") is None


def test_wiki_cache_all_rules_returns_copy() -> None:
    cache = WikiCache.from_dir(FIXTURE_ROOT)
    rules = cache.all_rules()
    assert len(rules) == 2
    rules.clear()
    # Mutating the returned list must not affect the cache's internal list.
    assert len(cache.all_rules()) == 2


def test_wiki_cache_missing_root_returns_empty(tmp_path: Path) -> None:
    cache = WikiCache.from_dir(tmp_path / "does_not_exist")
    assert cache.datasets == {}
    assert cache.rules == []
    assert cache.lineages == {}


def test_parse_page_unparseable_yaml_returns_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    bad = rules_dir / "bad_yaml.md"
    bad.write_text(
        "---\nname: x\n  bad: [unclosed\n---\n\nbody\n", encoding="utf-8"
    )
    caplog.set_level(logging.WARNING, logger="lens.wiki.reader")
    assert parse_page(bad) is None
    assert any("YAML" in rec.getMessage() or "yaml" in rec.getMessage() for rec in caplog.records)


def test_parse_page_empty_body_returns_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    page_path = rules_dir / "empty_body.md"
    page_path.write_text("---\nname: x\n---\n\n   \n", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="lens.wiki.reader")
    assert parse_page(page_path) is None
    assert any("empty body" in rec.getMessage() for rec in caplog.records)
