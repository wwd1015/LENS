"""YAML-based configuration loader for check suites."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from lens.engine import Suite
from lens.types import Severity


def load_suite(config_path: str | Path) -> Suite:
    """Load a Suite from a YAML configuration file.

    Expected format::

        entity_col: loan_id
        snapshot_col: as_of_date
        checks:
          - name: null_check
            params:
              fields: [balance, status]
          - name: monotonicity
            severity: error
            params:
              field: cumulative_payments
              direction: increasing
    """
    path = Path(config_path)
    with path.open() as f:
        cfg: dict[str, Any] = yaml.safe_load(f)

    suite = Suite(
        entity_col=cfg.get("entity_col", "entity_id"),
        snapshot_col=cfg.get("snapshot_col", "snapshot_date"),
    )

    for check_cfg in cfg.get("checks", []):
        params = check_cfg.get("params", {})
        if "severity" in check_cfg:
            params["severity"] = Severity(check_cfg["severity"])
        suite.add(check_cfg["name"], **params)

    return suite
