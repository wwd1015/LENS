"""Core types and data structures used across LENS modules."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid5

LENS_FINDING_NAMESPACE: UUID = UUID("8c4b3a82-9d31-4a4c-8a16-1c2e5b6a0d10")
"""Stable namespace UUID for v5 finding_id generation. Never change — would
invalidate every historical finding_id."""


class Severity(enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def compute_finding_id(
    entity_id: str | None,
    field_name: str | None,
    snapshot_date: date | datetime | None,
) -> str:
    """Deterministic finding_id for dedup across detectors.

    Per spec §6, dedup key is (entity_id, field_name, snapshot_date) — detector
    source is intentionally excluded so the same point flagged by multiple
    detectors collapses into one Finding.

    `snapshot_date` is normalized to its `date` part before keying so a
    Polars source returning `datetime` and a CSV source returning `date`
    produce the same finding_id for the same logical day.
    """
    if isinstance(snapshot_date, datetime):
        date_part = snapshot_date.date().isoformat()
    elif isinstance(snapshot_date, date):
        date_part = snapshot_date.isoformat()
    else:
        date_part = ""
    key = f"{entity_id or ''}|{field_name or ''}|{date_part}"
    return str(uuid5(LENS_FINDING_NAMESPACE, key))


@dataclass(frozen=True)
class Issue:
    """A single data quality issue detected by a check."""

    check_name: str
    severity: Severity
    entity_id: str | None = None
    field_name: str | None = None
    snapshot_date: datetime | None = None
    description: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    detector_source: str = ""
    finding_id: str = ""


@dataclass
class CheckResult:
    """Result of running a single check."""

    check_name: str
    passed: bool
    issues: list[Issue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuiteResult:
    """Aggregated result of running a suite of checks."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def all_issues(self) -> list[Issue]:
        return [issue for r in self.results for issue in r.issues]

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in self.all_issues:
            key = issue.severity.value
            counts[key] = counts.get(key, 0) + 1
        return counts


@dataclass
class Finding:
    """Orchestrator-level wrapper around an Issue with dedup metadata.

    When multiple detectors flag the same (entity_id, field_name, snapshot_date)
    they merge into one Finding; the merged detector list lives in
    `detector_sources`.
    """

    issue: Issue
    detector_sources: list[str] = field(default_factory=list)
    detected_at: datetime | None = None
    run_id: str = ""

    @property
    def finding_id(self) -> str:
        return self.issue.finding_id


@dataclass
class RCAResult:
    """Output of the RCA agent for a single Finding."""

    finding_id: str
    hypothesis: str
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    references: list[str] = field(default_factory=list)
