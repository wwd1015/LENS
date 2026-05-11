"""LENS wiki — typed reader + in-memory cache for `lens-wiki/` markdown pages.

Detectors and the RCA agent consume this package to access dataset, rule, and
lineage knowledge. Pages are markdown with YAML frontmatter; the reader returns
typed dataclasses and `WikiCache` indexes them for the orchestrator.
"""

from lens.wiki.cache import WikiCache
from lens.wiki.reader import DatasetPage, LineagePage, RulePage, parse_page

__all__ = [
    "DatasetPage",
    "LineagePage",
    "RulePage",
    "WikiCache",
    "parse_page",
]
