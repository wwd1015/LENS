"""Tests for the run-config loader (lens.run_config)."""

from __future__ import annotations

import pytest

from lens.io.polars_source import PolarsSource
from lens.run_config import load_run_config
from lens.types import Severity


def _write(tmp_path, text):
    cfg = tmp_path / "lens-run.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def _touch_csv(tmp_path, name="data.csv"):
    p = tmp_path / name
    p.write_text("entity_id,snapshot_date,balance\na,2026-01-01,1.0\n", encoding="utf-8")
    return p


def test_minimal_config_with_shorthand_source(tmp_path):
    _touch_csv(tmp_path)
    cfg = load_run_config(
        _write(
            tmp_path,
            """
            sources:
              loans: data.csv
            checks:
              - name: null_check
                params: {fields: [balance]}
            """,
        )
    )
    assert isinstance(cfg.sources["loans"], PolarsSource)
    assert cfg.entity_col == "entity_id"
    assert cfg.output_dir == tmp_path / "out"
    assert cfg.checks == [{"name": "null_check", "params": {"fields": ["balance"]}}]
    assert cfg.rca.enabled is True
    assert cfg.rca.severity_floor is Severity.ERROR
    assert cfg.feedback.path is None
    assert cfg.feedback.expiry_days == 90


def test_paths_resolve_relative_to_config_file(tmp_path):
    sub = tmp_path / "configs"
    sub.mkdir()
    (tmp_path / "configs" / "data.csv").write_text(
        "entity_id,snapshot_date\na,2026-01-01\n", encoding="utf-8"
    )
    cfg = load_run_config(
        _write(
            sub,
            """
            sources:
              loans: data.csv
            output_dir: ../out
            wiki_root: ../wiki
            feedback:
              path: fb.jsonl
            """,
        )
    )
    assert cfg.output_dir == sub / "../out"
    assert cfg.wiki_root == sub / "../wiki"
    assert cfg.feedback.path == sub / "fb.jsonl"


def test_full_config_round_trip(tmp_path):
    _touch_csv(tmp_path, "a.csv")
    _touch_csv(tmp_path, "b.csv")
    cfg = load_run_config(
        _write(
            tmp_path,
            """
            entity_col: loan_id
            snapshot_col: as_of_date
            sources:
              pool: a.csv
              debt:
                path: b.csv
            wiki_root: wiki
            output_dir: results
            checks:
              - name: stl_residual
                params: {field: balance}
            cross_checks:
              - cross_source_wiki
            rca:
              enabled: false
              severity_floor: warning
              repo_root: .
            feedback:
              path: feedback.jsonl
              expiry_days: 30
            brief:
              dataset_label: "Test portfolio"
              top_n: 3
            """,
        )
    )
    assert set(cfg.sources) == {"pool", "debt"}
    assert cfg.entity_col == "loan_id"
    assert cfg.cross_checks == [{"name": "cross_source_wiki", "params": {}}]
    assert cfg.rca.enabled is False
    assert cfg.rca.severity_floor is Severity.WARNING
    assert cfg.feedback.expiry_days == 30
    # Cost controls default when unspecified.
    assert cfg.rca.max_investigations is None
    assert cfg.rca.model is None
    assert cfg.rca.sample_rows == 5
    assert cfg.rca.max_commits == 5
    assert cfg.brief.dataset_label == "Test portfolio"
    assert cfg.brief.top_n == 3


def test_rca_cost_controls_parsed(tmp_path):
    _touch_csv(tmp_path)
    cfg = load_run_config(
        _write(
            tmp_path,
            """
            sources: {loans: data.csv}
            rca:
              severity_floor: error
              max_investigations: 8
              model: claude-haiku-4-5-20251001
              sample_rows: 2
              max_commits: 3
            """,
        )
    )
    assert cfg.rca.max_investigations == 8
    assert cfg.rca.model == "claude-haiku-4-5-20251001"
    assert cfg.rca.sample_rows == 2
    assert cfg.rca.max_commits == 3


def test_env_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_TEST_DATA_DIR", str(tmp_path))
    _touch_csv(tmp_path)
    cfg = load_run_config(
        _write(
            tmp_path,
            """
            sources:
              loans: ${LENS_TEST_DATA_DIR}/data.csv
            """,
        )
    )
    assert isinstance(cfg.sources["loans"], PolarsSource)


def test_undefined_env_var_raises(tmp_path):
    with pytest.raises(ValueError, match="LENS_NO_SUCH_VAR"):
        load_run_config(
            _write(
                tmp_path,
                """
                sources:
                  loans: ${LENS_NO_SUCH_VAR}/data.csv
                """,
            )
        )


def test_missing_sources_raises(tmp_path):
    with pytest.raises(ValueError, match="sources"):
        load_run_config(_write(tmp_path, "output_dir: out\n"))


def test_bad_severity_floor_raises(tmp_path):
    _touch_csv(tmp_path)
    with pytest.raises(ValueError, match="severity"):
        load_run_config(
            _write(
                tmp_path,
                """
                sources: {loans: data.csv}
                rca: {severity_floor: catastrophic}
                """,
            )
        )


def test_unknown_source_kind_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown kind"):
        load_run_config(
            _write(
                tmp_path,
                """
                sources:
                  loans: {kind: bigquery, path: x}
                """,
            )
        )
