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


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.ERROR: 2,
    Severity.CRITICAL: 3,
}
"""Canonical severity ranking — higher is more urgent. Single source of truth
for every module that sorts or floors by severity."""


def detector_family(detector_source: str) -> str:
    """Strip the rule-slug suffix off a namespaced detector identity.

    ``cross_source_wiki:rule_a`` → ``cross_source_wiki``. Plain identities
    pass through unchanged. Empty input yields ``"unknown"``.
    """
    if not detector_source:
        return "unknown"
    return detector_source.split(":", 1)[0]


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
    snapshot_date: date | datetime | None = None
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

    @property
    def detector_families(self) -> list[str]:
        """Distinct detector families that flagged this point, stable order."""
        seen: list[str] = []
        for src in self.detector_sources or [self.issue.detector_source or self.issue.check_name]:
            fam = detector_family(src)
            if fam not in seen:
                seen.append(fam)
        return seen


def finding_group_key(finding: Finding) -> tuple[str, str]:
    """The Finding Group identity: ``(detector_family, field_name)``.

    This is the unit at which the brief renders sections AND at which batch
    RCA investigates (ADR 0003) — one investigation per group. The family is
    taken from the first detector source so a multi-detector finding lands in
    the group of whichever detector the dedup kept first.
    """
    families = finding.detector_families
    family = families[0] if families else "unknown"
    return (family, finding.issue.field_name or "")


@dataclass
class RCAResult:
    """Output of the RCA agent for a single Finding.

    The trailing ``cost_*`` / ``model`` fields record what the LLM call that
    produced this result cost (Claude Code's per-call estimate) and which model
    answered. They are optional and default to ``None`` so older persisted RCA
    JSON loads unchanged and non-cost-aware stubs keep working.
    """

    finding_id: str
    hypothesis: str
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    references: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    reused_from: str | None = None
    """When the batch reused a prior run's hypothesis for this group, the
    finding_id whose persisted RCA was reused. ``None`` for fresh
    investigations (and for RCA JSON written before this field existed)."""
