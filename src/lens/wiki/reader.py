"""Typed reader for `lens-wiki/` markdown pages.

Each page is YAML frontmatter (fenced by `---`) followed by a markdown body.
`parse_page(path)` returns a `RulePage`, `DatasetPage`, or `LineagePage`
depending on the parent directory. Malformed pages are logged and skipped
(return `None`) — the reader never raises on bad input.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RulePage:
    """A rule page: a cross-source equation with a structured frontmatter spec."""

    frontmatter: dict[str, Any]
    body: str
    path: Path

    @property
    def name(self) -> str:
        return str(self.frontmatter.get("name", ""))

    @property
    def tables(self) -> list[str]:
        value = self.frontmatter.get("tables", []) or []
        return [str(t) for t in value]

    @property
    def fields(self) -> list[str]:
        value = self.frontmatter.get("fields", []) or []
        return [str(f) for f in value]

    @property
    def equation(self) -> dict[str, Any]:
        value = self.frontmatter.get("equation", {}) or {}
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class DatasetPage:
    """A dataset page: entity grain, segments, snapshot cadence, lineage link."""

    frontmatter: dict[str, Any]
    body: str
    path: Path

    @property
    def name(self) -> str:
        return str(self.frontmatter.get("name", ""))

    @property
    def entity_grain(self) -> str:
        return str(self.frontmatter.get("entity_grain", ""))

    @property
    def segments(self) -> list[str]:
        value = self.frontmatter.get("segments", []) or []
        return [str(s) for s in value]


@dataclass(frozen=True)
class LineagePage:
    """A lineage page: upstream + downstream tables, producing-code paths."""

    frontmatter: dict[str, Any]
    body: str
    path: Path

    @property
    def table(self) -> str:
        return str(self.frontmatter.get("table", ""))

    @property
    def upstream(self) -> list[dict[str, Any]]:
        value = self.frontmatter.get("upstream", []) or []
        return [v for v in value if isinstance(v, dict)]

    @property
    def downstream(self) -> list[dict[str, Any]]:
        value = self.frontmatter.get("downstream", []) or []
        return [v for v in value if isinstance(v, dict)]

    @property
    def producing_code(self) -> list[str]:
        value = self.frontmatter.get("producing_code", []) or []
        return [str(p) for p in value]


# Marker type — referenced only to satisfy default-factory typing in dataclasses
# (none currently use it but kept for symmetry with types.py style).
_UNUSED = field  # noqa: F841


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split a markdown file's text into (yaml_block, body).

    Returns None if the file doesn't have a proper `---` ... `---` frontmatter
    fence at the start. Strips a leading BOM and surrounding whitespace.
    """
    text = text.lstrip("﻿").lstrip()
    if not text.startswith("---"):
        return None
    # Drop the opening fence (the first line, which is just `---` plus maybe trailing ws).
    rest = text[3:]
    # Normalize: ensure we are positioned after the newline that terminates the opening fence.
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    else:
        # Unusual: `---` not followed by newline — treat as malformed.
        return None

    # Find the closing fence: a line that is exactly `---` (allowing trailing ws).
    lines = rest.splitlines(keepends=True)
    closing_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            closing_idx = i
            break
    if closing_idx is None:
        return None

    yaml_block = "".join(lines[:closing_idx])
    body = "".join(lines[closing_idx + 1 :])
    return yaml_block, body


_TYPE_BY_PARENT = {
    "rules": RulePage,
    "datasets": DatasetPage,
    "lineage": LineagePage,
}


def parse_page(path: Path) -> RulePage | DatasetPage | LineagePage | None:
    """Parse a wiki markdown page into its typed dataclass.

    Returns None for:
      - template files (name starts with `_`)
      - files outside a recognized subdirectory (`rules/`, `datasets/`, `lineage/`)
      - files missing or with broken frontmatter
      - files with unparseable YAML
      - files with an empty body
    Malformed inputs are logged at WARNING; the function never raises.
    """
    path = Path(path)
    if path.name.startswith("_"):
        return None

    parent_name = path.parent.name
    page_cls = _TYPE_BY_PARENT.get(parent_name)
    if page_cls is None:
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("wiki reader: could not read %s: %s", path, exc)
        return None

    split = _split_frontmatter(text)
    if split is None:
        logger.warning("wiki reader: %s is missing or has unterminated frontmatter", path)
        return None

    yaml_block, body = split

    try:
        frontmatter = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        logger.warning("wiki reader: %s has unparseable YAML: %s", path, exc)
        return None

    if not isinstance(frontmatter, dict):
        logger.warning(
            "wiki reader: %s frontmatter is not a mapping (got %s)",
            path,
            type(frontmatter).__name__,
        )
        return None

    if not body.strip():
        logger.warning("wiki reader: %s has empty body", path)
        return None

    return page_cls(frontmatter=frontmatter, body=body, path=path)
