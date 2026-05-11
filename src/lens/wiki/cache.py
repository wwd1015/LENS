"""In-memory cache of the `lens-wiki/` tree, loaded once per orchestrator run.

Detectors and the RCA agent share one `WikiCache` per run rather than re-reading
markdown from disk on every call. Pages are indexed by `name` (datasets, rules)
or `table` (lineage); the cache exposes a small query API targeted at detector
needs (`rules_for_field`, `dataset`, `lineage`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from lens.wiki.reader import DatasetPage, LineagePage, RulePage, parse_page

logger = logging.getLogger(__name__)


@dataclass
class WikiCache:
    """Eagerly-loaded index of the wiki, queried many times per run.

    Built by `WikiCache.from_dir(wiki_root)`; the caller passes the resulting
    instance into every detector and RCA invocation so the markdown tree is
    read exactly once per orchestrator run.
    """

    datasets: dict[str, DatasetPage] = field(default_factory=dict)
    rules: list[RulePage] = field(default_factory=list)
    lineages: dict[str, LineagePage] = field(default_factory=dict)

    @classmethod
    def from_dir(cls, wiki_root: Path | str) -> WikiCache:
        """Walk `wiki_root` recursively and index every parseable page.

        Files whose names start with `_` (templates) are skipped. Malformed
        pages emit a warning via the reader and are skipped silently here.
        """
        wiki_root = Path(wiki_root)
        cache = cls()
        if not wiki_root.exists():
            logger.warning("WikiCache.from_dir: %s does not exist", wiki_root)
            return cache

        for md_path in sorted(wiki_root.rglob("*.md")):
            page = parse_page(md_path)
            if page is None:
                continue
            if isinstance(page, DatasetPage):
                if page.name:
                    cache.datasets[page.name] = page
                else:
                    logger.warning("WikiCache: dataset page %s has no `name`", md_path)
            elif isinstance(page, RulePage):
                cache.rules.append(page)
            elif isinstance(page, LineagePage):
                if page.table:
                    cache.lineages[page.table] = page
                else:
                    logger.warning("WikiCache: lineage page %s has no `table`", md_path)

        return cache

    def rules_for_field(self, table: str, field_name: str) -> list[RulePage]:
        """Return rules that mention `table` (in `tables`) or `table.field_name`
        (in `fields`). Order preserves load order.
        """
        qualified = f"{table}.{field_name}"
        matched: list[RulePage] = []
        for rule in self.rules:
            if table in rule.tables or qualified in rule.fields:
                matched.append(rule)
        return matched

    def dataset(self, name: str) -> DatasetPage | None:
        return self.datasets.get(name)

    def lineage(self, table: str) -> LineagePage | None:
        return self.lineages.get(table)

    def all_rules(self) -> list[RulePage]:
        return list(self.rules)
