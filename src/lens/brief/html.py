"""HTML morning brief renderer.

Produces a single self-contained HTML file from a list of
:class:`lens.types.Finding` plus optional RCA results. Security-critical:
the Jinja2 environment is instantiated with ``autoescape=select_autoescape``
so that LLM-authored hostile content in descriptions / RCA hypotheses is
escaped rather than rendered as live markup.
"""

from __future__ import annotations

import json
import os
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from lens.types import Finding, RCAResult, Severity

_TEMPLATES_DIR: Path = Path(__file__).parent / "templates"
_STYLES_PATH: Path = _TEMPLATES_DIR / "styles.css"

# Severity rank for sort + grouping order. CRITICAL = highest.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 3,
    Severity.ERROR: 2,
    Severity.WARNING: 1,
    Severity.INFO: 0,
}


@dataclass
class _GroupView:
    """View model for a single (detector_prefix, field_name) group."""

    label: str
    findings_in_group: list[Finding]
    total_in_group: int


def _detector_prefix(finding: Finding) -> str:
    """Pull the ``foo`` out of a ``foo:rule_slug`` detector_source identifier.

    Falls back to the first entry of ``detector_sources`` or the check_name.
    """
    if finding.detector_sources:
        src = finding.detector_sources[0]
    elif finding.issue.detector_source:
        src = finding.issue.detector_source
    else:
        src = finding.issue.check_name or "unknown"
    return src.split(":", 1)[0] if src else "unknown"


def _sort_key(finding: Finding) -> tuple[int, float]:
    """Sort key for findings — severity descending, then confidence descending."""
    rank = _SEVERITY_RANK.get(finding.issue.severity, -1)
    # We sort ascending, so negate to get descending order.
    return (-rank, -float(finding.issue.confidence or 0.0))


def _summary_counts(findings: list[Finding]) -> dict[str, int]:
    """Per-severity tally, keyed by severity .value (``critical``/``error``/...)."""
    counts: dict[str, int] = {}
    for f in findings:
        key = f.issue.severity.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _read_prior_finding_ids(prior_findings_path: Path | None) -> set[str] | None:
    """Load finding_ids from a prior run's findings.json.

    Returns ``None`` if no path was supplied or the file is missing — the
    template treats this as "no delta to show". Malformed JSON also yields
    ``None`` rather than crashing (the brief should still render).
    """
    if prior_findings_path is None:
        return None
    if not prior_findings_path.exists():
        return None
    try:
        with prior_findings_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    ids: set[str] = set()
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                fid = entry.get("finding_id") or entry.get("issue", {}).get("finding_id")
                if fid:
                    ids.add(str(fid))
    return ids


def _compute_delta(
    current: list[Finding], prior_ids: set[str] | None
) -> dict[str, int] | None:
    """Compute the new/resolved/ongoing counts vs. a prior run.

    Returns ``None`` when there is no prior run (the template suppresses the
    delta header in that case).
    """
    if prior_ids is None:
        return None
    current_ids = {f.finding_id for f in current}
    new_ids = current_ids - prior_ids
    resolved_ids = prior_ids - current_ids
    ongoing_ids = current_ids & prior_ids
    return {
        "new": len(new_ids),
        "resolved": len(resolved_ids),
        "ongoing": len(ongoing_ids),
    }


def _group_findings(findings: list[Finding]) -> list[_GroupView]:
    """Group findings by ``(detector_prefix, field_name)``.

    Group order is determined by the highest-severity finding inside each
    group — groups with a CRITICAL finding lead, then ERROR, etc.
    """
    buckets: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for f in findings:
        key = (_detector_prefix(f), f.issue.field_name or "")
        buckets[key].append(f)

    groups: list[_GroupView] = []
    for (prefix, field), members in buckets.items():
        # Already sorted within bucket because we grouped post-sort.
        label = f"{prefix} / {field}" if field else prefix
        groups.append(
            _GroupView(
                label=label,
                findings_in_group=members,
                total_in_group=len(members),
            )
        )

    # Top-level group ordering: highest severity at the top, ties broken by
    # max confidence in the group, finally by label for stability.
    def _group_sort_key(g: _GroupView) -> tuple[int, float, str]:
        top = g.findings_in_group[0]
        rank = _SEVERITY_RANK.get(top.issue.severity, -1)
        return (-rank, -float(top.issue.confidence or 0.0), g.label)

    groups.sort(key=_group_sort_key)
    return groups


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomic write: write to a sibling tmp file then ``os.replace`` over target.

    POSIX guarantees the rename is atomic when source and destination share a
    filesystem, so partial writes never surface to a reader.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    tmp_path = path.parent / tmp_name
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    finally:
        # If os.replace succeeded, tmp_path no longer exists — unlink will
        # raise FileNotFoundError, which we swallow. If we failed midway,
        # this cleans up the leftover.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _finding_view(f: Finding, rcas: dict[str, RCAResult]) -> dict[str, Any]:
    """Build the per-finding dict the template renders.

    Pulling everything into a flat dict here (rather than passing the
    dataclass) keeps the template tidy and avoids accidentally exposing
    additional Issue fields.
    """
    issue = f.issue
    rca = rcas.get(f.finding_id)
    return {
        "finding_id": f.finding_id,
        "severity": issue.severity.value,
        "severity_upper": issue.severity.value.upper(),
        "confidence": float(issue.confidence or 0.0),
        "entity_id": issue.entity_id or "",
        "field_name": issue.field_name or "",
        "snapshot_date": (
            issue.snapshot_date.isoformat()
            if hasattr(issue.snapshot_date, "isoformat") and issue.snapshot_date is not None
            else (str(issue.snapshot_date) if issue.snapshot_date else "")
        ),
        "description": issue.description or "",
        "detector_sources": list(f.detector_sources),
        "rca": (
            {
                "hypothesis": rca.hypothesis,
                "evidence": list(rca.evidence),
                "references": list(rca.references),
                "confidence": float(rca.confidence or 0.0),
            }
            if rca is not None
            else None
        ),
    }


def render_brief(
    findings: list[Finding],
    rcas: dict[str, RCAResult],
    output_path: Path,
    *,
    prior_findings_path: Path | None = None,
    dataset_label: str = "",
    max_findings: int = 500,
) -> Path:
    """Render an HTML morning brief to ``output_path``.

    Args:
        findings: The current run's findings (will be sorted + grouped here).
        rcas: Mapping ``finding_id`` → :class:`RCAResult`. Missing keys render
            without an RCA expander.
        output_path: Target HTML file. Written atomically via tmp + replace.
        prior_findings_path: Optional path to a prior run's ``findings.json``;
            if supplied, the brief includes a "what changed since prior run"
            delta header.
        dataset_label: Free-text label rendered in the header (e.g.
            ``"loan_pool / 2026-05-10"``). Escaped by Jinja2.
        max_findings: Hard cap on rendered cards. The full list is still in
            findings.json; this prevents the brief from blowing up on 10k+
            findings days.

    Returns:
        ``output_path`` (for chaining).
    """
    output_path = Path(output_path)

    # Sort + cap. The cap is rendered with a warning so the analyst knows
    # they're seeing the top-N rather than the full set.
    sorted_findings = sorted(findings, key=_sort_key)
    total_findings = len(sorted_findings)
    cap_applied = total_findings > max_findings
    visible_findings = sorted_findings[:max_findings] if cap_applied else sorted_findings

    # Grouping operates on the visible (post-cap) findings — capping first
    # then grouping prevents a 10k-finding run from rendering 10k group
    # headers.
    groups = _group_findings(visible_findings)

    # Build view models for each group.
    group_views = [
        {
            "label": g.label,
            "total_in_group": g.total_in_group,
            "findings_in_group": [_finding_view(f, rcas) for f in g.findings_in_group],
        }
        for g in groups
    ]

    # Delta vs. prior — None when no prior path was given.
    prior_ids = _read_prior_finding_ids(prior_findings_path)
    delta_counts = _compute_delta(visible_findings, prior_ids)

    summary_counts = _summary_counts(visible_findings)

    # Inline the styles so the output is fully self-contained. The CSS is
    # marked `safe` in the template (CSS is not user-controlled), but every
    # other variable goes through autoescape.
    if _STYLES_PATH.exists():
        styles_inline = _STYLES_PATH.read_text(encoding="utf-8")
    else:
        styles_inline = ""

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2", "html.j2"]),
    )
    template = env.get_template("brief.html.j2")

    rendered = template.render(
        dataset_label=dataset_label,
        date_iso=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        summary_counts=summary_counts,
        delta_counts=delta_counts,
        groups=group_views,
        total_findings=total_findings,
        visible_count=len(visible_findings),
        cap_applied=cap_applied,
        max_findings=max_findings,
        styles_inline=styles_inline,
    )

    _atomic_write_text(output_path, rendered)
    return output_path
