"""Tests for SnowflakeSource SQL construction — no warehouse connection needed."""

from __future__ import annotations

import pytest

from lens.io.snowflake_source import SnowflakeSource

URI = "snowflake://user:pass@account/db"


def _source(**kwargs) -> SnowflakeSource:
    return SnowflakeSource(URI, table="loans", **kwargs)


def test_entity_id_quotes_are_escaped():
    """entity_id flows from data and analyst input (`lens-rca
    --investigate-entity`); a quote must not break out of the literal."""
    q = _source()._history_query(entity_id="O'Brien'; DROP TABLE loans;--")
    assert "'O''Brien''; DROP TABLE loans;--'" in q
    # The raw un-escaped payload never appears.
    assert "= 'O'Brien" not in q


def test_dates_are_validated_and_re_emitted():
    q = _source()._history_query(start_date="2026-01-01", end_date="2026-02-01")
    assert "snapshot_date >= '2026-01-01'" in q
    assert "snapshot_date <= '2026-02-01'" in q


def test_bad_date_rejected():
    with pytest.raises(ValueError, match="not an ISO date"):
        _source()._history_query(start_date="2026-01-01' OR '1'='1")


def test_snapshot_query_uses_validated_literal():
    q = _source()._snapshot_query("2026-03-31")
    assert "WHERE snapshot_date = '2026-03-31'" in q
    with pytest.raises(ValueError, match="not an ISO date"):
        _source()._snapshot_query("31/03/2026")


def test_bad_identifiers_rejected_at_construction():
    with pytest.raises(ValueError, match="entity_col"):
        SnowflakeSource(URI, table="loans", entity_col="id; DROP TABLE x")
    with pytest.raises(ValueError, match="snapshot_col"):
        SnowflakeSource(URI, table="loans", snapshot_col="d'ate")
    with pytest.raises(ValueError, match="table"):
        SnowflakeSource(URI, table="loans; DROP TABLE x")


def test_dotted_table_identifier_allowed():
    src = SnowflakeSource(URI, table="PROD.LENDING.LOANS")
    assert src._base_query() == "SELECT * FROM PROD.LENDING.LOANS"
