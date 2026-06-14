"""Run-config loader for the `lens run` batch entry point.

A run config is everything one scheduled batch needs in a single YAML file —
sources, wiki root, output dir, detector suite, RCA policy, feedback policy,
brief options. Relative paths are resolved against the config file's own
directory so a cron line can invoke the config from anywhere.

Example::

    entity_col: loan_id
    snapshot_col: as_of_date

    sources:
      loan_pool: data/loan_pool.csv          # shorthand: path string
      senior_debt:
        path: data/senior_debt.parquet
      warehouse:                              # requires the [snowflake] extra
        kind: snowflake
        connection_uri: ${SNOWFLAKE_URI}      # env vars are interpolated
        table: LENDING.LOAN_POOL

    wiki_root: lens-wiki                      # optional
    output_dir: out

    checks:                                   # single-source detectors
      - name: null_check
        params: {fields: [balance]}
      - name: stl_residual
        params: {field: balance}
    cross_checks:                             # cross-source detectors
      - name: cross_source_wiki

    rca:
      enabled: true
      severity_floor: error                   # groups at/above get one RCA each
      repo_root: .                            # git log root for producing paths
      max_investigations: 10                  # cost cap: at most N LLM calls/run
      model: claude-haiku-4-5-20251001        # cheaper model for RCA (optional)
      sample_rows: 5                          # data rows per prompt (context size)
      max_commits: 5                          # commits per producing path

    feedback:
      path: feedback.jsonl
      expiry_days: 90

    brief:
      dataset_label: "Lending portfolio"
      top_n: 5
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lens.io.base import DataSource
from lens.io.polars_source import PolarsSource
from lens.types import Severity

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class RCAConfig:
    enabled: bool = True
    severity_floor: Severity = Severity.ERROR
    repo_root: Path = Path(".")
    # --- token / cost controls -----------------------------------------
    # Cap on the number of Finding Groups investigated per run; the highest
    # severity/confidence groups are kept. None = no cap (every group above
    # the floor). Each investigation is one LLM call, so this bounds the
    # worst-case cost on an incident day.
    max_investigations: int | None = None
    # Model for RCA calls (passed to `claude -p --model`). Point at a cheaper
    # model (e.g. a Haiku tier) to cut cost. None = Claude Code's default.
    model: str | None = None
    # Context-size knobs — fewer sampled rows / commits = smaller prompts.
    sample_rows: int = 5
    max_commits: int = 5


@dataclass
class FeedbackConfig:
    path: Path | None = None
    expiry_days: int = 90


@dataclass
class BriefConfig:
    dataset_label: str = ""
    top_n: int = 5


@dataclass
class RunConfig:
    """Parsed, path-resolved batch run configuration."""

    config_path: Path
    entity_col: str = "entity_id"
    snapshot_col: str = "snapshot_date"
    sources: dict[str, DataSource] = field(default_factory=dict)
    wiki_root: Path | None = None
    output_dir: Path = Path("out")
    checks: list[dict[str, Any]] = field(default_factory=list)
    cross_checks: list[dict[str, Any]] = field(default_factory=list)
    rca: RCAConfig = field(default_factory=RCAConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    brief: BriefConfig = field(default_factory=BriefConfig)


def _interpolate_env(value: str) -> str:
    """Substitute ``${VAR}`` with the environment value; missing vars raise."""

    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        resolved = os.environ.get(var)
        if resolved is None:
            raise ValueError(f"run config references undefined environment variable ${{{var}}}")
        return resolved

    return _ENV_RE.sub(_sub, value)


def _resolve_path(raw: str, base: Path) -> Path:
    p = Path(_interpolate_env(raw)).expanduser()
    return p if p.is_absolute() else (base / p)


def _build_source(
    name: str,
    spec: Any,
    base: Path,
    *,
    entity_col: str,
    snapshot_col: str,
) -> DataSource:
    """Turn one ``sources:`` entry into a :class:`DataSource`."""
    if isinstance(spec, str):
        spec = {"path": spec}
    if not isinstance(spec, dict):
        raise ValueError(
            f"source {name!r}: expected a path string or a mapping, got {type(spec).__name__}"
        )

    kind = spec.get("kind", "snowflake" if "connection_uri" in spec else "file")

    if kind == "file":
        raw_path = spec.get("path")
        if not raw_path:
            raise ValueError(f"source {name!r}: file source needs a 'path'")
        return PolarsSource(
            path=str(_resolve_path(str(raw_path), base)),
            entity_col=entity_col,
            snapshot_col=snapshot_col,
        )

    if kind == "snowflake":
        try:
            from lens.io.snowflake_source import SnowflakeSource
        except ImportError as exc:  # pragma: no cover - dep-conditional
            raise ValueError(
                f"source {name!r}: snowflake source requires the [snowflake] extra"
            ) from exc
        uri = spec.get("connection_uri")
        if not uri:
            raise ValueError(f"source {name!r}: snowflake source needs 'connection_uri'")
        return SnowflakeSource(
            connection_uri=_interpolate_env(str(uri)),
            table=spec.get("table"),
            query=spec.get("query"),
            entity_col=entity_col,
            snapshot_col=snapshot_col,
        )

    raise ValueError(f"source {name!r}: unknown kind {kind!r} (expected 'file' or 'snowflake')")


def _parse_severity(raw: Any, *, context: str) -> Severity:
    try:
        return Severity(str(raw).lower())
    except ValueError as exc:
        valid = ", ".join(m.value for m in Severity)
        raise ValueError(f"{context}: unknown severity {raw!r}; expected one of {valid}") from exc


def _parse_check_list(raw: Any, *, context: str) -> list[dict[str, Any]]:
    """Normalize a checks list into ``[{name, params}, ...]``."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{context}: expected a list")
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ValueError(f"{context}[{i}]: each entry needs a 'name'")
        params = dict(entry.get("params") or {})
        if "severity" in entry:
            params["severity"] = _parse_severity(entry["severity"], context=f"{context}[{i}]")
        spec: dict[str, Any] = {"name": entry["name"], "params": params}
        if entry.get("sources") is not None:
            scope = entry["sources"]
            if not isinstance(scope, list):
                raise ValueError(f"{context}[{i}]: 'sources' must be a list of source names")
            spec["sources"] = [str(s) for s in scope]
        out.append(spec)
    return out


def load_run_config(config_path: str | Path) -> RunConfig:
    """Load and validate a run config YAML into a :class:`RunConfig`."""
    config_path = Path(config_path).resolve()
    with config_path.open(encoding="utf-8") as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{config_path}: top level must be a mapping")

    base = config_path.parent
    entity_col = str(cfg.get("entity_col", "entity_id"))
    snapshot_col = str(cfg.get("snapshot_col", "snapshot_date"))

    raw_sources = cfg.get("sources") or {}
    if not isinstance(raw_sources, dict) or not raw_sources:
        raise ValueError(f"{config_path}: 'sources' must be a non-empty mapping")
    sources = {
        str(name): _build_source(
            str(name), spec, base, entity_col=entity_col, snapshot_col=snapshot_col
        )
        for name, spec in raw_sources.items()
    }

    wiki_root = _resolve_path(str(cfg["wiki_root"]), base) if cfg.get("wiki_root") else None
    output_dir = _resolve_path(str(cfg.get("output_dir", "out")), base)

    rca_raw = cfg.get("rca") or {}
    max_inv = rca_raw.get("max_investigations")
    rca = RCAConfig(
        enabled=bool(rca_raw.get("enabled", True)),
        severity_floor=_parse_severity(
            rca_raw.get("severity_floor", "error"), context="rca.severity_floor"
        ),
        repo_root=_resolve_path(str(rca_raw.get("repo_root", ".")), base),
        max_investigations=int(max_inv) if max_inv is not None else None,
        model=str(rca_raw["model"]) if rca_raw.get("model") else None,
        sample_rows=int(rca_raw.get("sample_rows", 5)),
        max_commits=int(rca_raw.get("max_commits", 5)),
    )

    fb_raw = cfg.get("feedback") or {}
    feedback = FeedbackConfig(
        path=_resolve_path(str(fb_raw["path"]), base) if fb_raw.get("path") else None,
        expiry_days=int(fb_raw.get("expiry_days", 90)),
    )

    brief_raw = cfg.get("brief") or {}
    brief = BriefConfig(
        dataset_label=str(brief_raw.get("dataset_label", "")),
        top_n=int(brief_raw.get("top_n", 5)),
    )

    return RunConfig(
        config_path=config_path,
        entity_col=entity_col,
        snapshot_col=snapshot_col,
        sources=sources,
        wiki_root=wiki_root,
        output_dir=output_dir,
        checks=_parse_check_list(cfg.get("checks"), context="checks"),
        cross_checks=_parse_check_list(cfg.get("cross_checks"), context="cross_checks"),
        rca=rca,
        feedback=feedback,
        brief=brief,
    )
