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
import re
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from lens.types import Finding, RCAResult, Severity, finding_group_key

_TEMPLATES_DIR: Path = Path(__file__).parent / "templates"
_STYLES_PATH: Path = _TEMPLATES_DIR / "styles.css"

# Severity rank for sort + grouping order. CRITICAL = highest.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 3,
    Severity.ERROR: 2,
    Severity.WARNING: 1,
    Severity.INFO: 0,
}

# ---------------------------------------------------------------------------
# Plain-language layer — translate detector / severity jargon into something a
# non-technical analyst can read at a glance. Keys are detector FAMILIES (the
# part before the ':' in a detector_source). Values: (headline, what-it-means).
# ---------------------------------------------------------------------------
_DETECTOR_FRIENDLY: dict[str, tuple[str, str]] = {
    "null_check": (
        "Missing information",
        "A value that should be filled in was left blank.",
    ),
    "range_check": (
        "Number outside the expected range",
        "A figure is higher or lower than the limits set for it.",
    ),
    "stale_data": (
        "Figure hasn't updated",
        "A value stayed the same across dates when it was expected to change.",
    ),
    "monotonicity": (
        "Moved in the wrong direction",
        "A running total went down when it should only ever go up (or vice-versa).",
    ),
    "volatility": (
        "Unusually big jump between dates",
        "A value changed far more from one date to the next than it normally does.",
    ),
    "stl_residual": (
        "Doesn't match its recent trend",
        "This figure broke from the steady month-to-month pattern it had followed.",
    ),
    "tabpfn_anomaly": (
        "Unexpected value for this series",
        "A forecasting model expected a very different number here.",
    ),
    "hierarchical_drill_down": (
        "One segment is driving the swing",
        "A specific slice of the portfolio is behind a larger overall movement.",
    ),
    "cross_source_wiki": (
        "Two systems don't agree",
        "Numbers that should match across systems are off by more than allowed.",
    ),
    "cross_source_match": (
        "Two systems don't agree",
        "The same figure is recorded differently in two places.",
    ),
}
_GENERIC_FRIENDLY: tuple[str, str] = (
    "Possible data problem",
    "An automated check flagged this value for review.",
)

# Plain meaning for each severity, shown as a pill instead of a bare label.
_SEVERITY_MEANING: dict[str, str] = {
    "critical": "Needs urgent attention",
    "error": "Likely a real problem",
    "warning": "Worth a look",
    "info": "For your awareness",
}

# How an earlier analyst verdict reads back on an ongoing finding.
_PRIOR_FEEDBACK_HUMAN: dict[str, str] = {
    "real": "an analyst confirmed this is real",
    "false_positive": "an analyst marked this a false alarm",
    "needs_more": "an analyst asked for a closer look",
}

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_URL_RE = re.compile(r"https?://[^\s<>\"')]+")


def _detector_friendly(family: str) -> tuple[str, str]:
    return _DETECTOR_FRIENDLY.get(family, _GENERIC_FRIENDLY)


def _humanize_field(field: str) -> str:
    """``advance_rate`` → ``Advance rate``; empty stays empty."""
    if not field:
        return ""
    return field.replace("_", " ").strip().capitalize()


def _humanize_date(value: Any) -> str:
    """Render any date-ish value as ``Jun 30, 2026``; fall back to its string."""
    if value is None:
        return ""
    iso = value.isoformat() if hasattr(value, "isoformat") else str(value)
    try:
        d = date.fromisoformat(iso[:10])
    except (ValueError, TypeError):
        return str(value)
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _linkify_segments(text: str) -> list[dict[str, str]]:
    """Split prose into ``{"text": ...}`` / ``{"url": ...}`` segments.

    The template renders text through autoescape and URLs as ``<a>`` tags, so
    a model-supplied link becomes clickable in place without ever marking raw
    LLM output as ``safe``.
    """
    segments: list[dict[str, str]] = []
    last = 0
    for m in _URL_RE.finditer(text):
        if m.start() > last:
            segments.append({"text": text[last : m.start()]})
        segments.append({"url": m.group(0)})
        last = m.end()
    if last < len(text):
        segments.append({"text": text[last:]})
    return segments or [{"text": text}]


def _friendly_link_label(url: str) -> str:
    if "/commit/" in url:
        sha = url.rsplit("/commit/", 1)[-1].strip("/")[:8]
        suffix = f" ({sha})" if sha else ""
        return f"See the data-pipeline change that may have caused this{suffix}"
    if "/blob/" in url or "/tree/" in url or "/-/blob/" in url:
        return "Open the data-pipeline code that builds this data"
    return "Open reference"


def _fmt_num(value: Any) -> str:
    """Format a number for a non-technical reader.

    Big numbers get thousands separators (``2905000`` → ``2,905,000``); whole
    numbers drop the decimal; small fractions render naturally (``0.75``).
    Non-numbers pass through as their string form.
    """
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    if x == int(x):
        return f"{int(x):,}"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    return f"{x:g}"


def _fmt_usd(value: Any) -> str:
    """Format a USD amount for display. Sub-dollar shows 4 dp ($0.0123)."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "$0.00"
    if abs(x) >= 1:
        return f"${x:,.2f}"
    return f"${x:.4f}"


def _rca_cost_label(rca: RCAResult) -> str:
    """One-line 'Investigated with <model> · est. $<cost>' caption, or ''.

    Both halves are optional: a session-default model (no ``model``) drops the
    'Investigated with' clause; a zero / missing cost (e.g. a test stub) drops
    the dollar clause. Empty string when neither is present.
    """
    parts: list[str] = []
    model = getattr(rca, "model", None)
    if model:
        parts.append(f"Investigated with {model}")
    cost = getattr(rca, "cost_usd", None)
    if cost:
        parts.append(f"estimated {_fmt_usd(cost)}")
    return " · ".join(parts)


def _tech_breakdown(details: dict[str, Any]) -> dict[str, Any] | None:
    """Build a reproducible calculation view from a cross-source finding.

    Returns ``None`` for findings that don't carry a structured equation
    breakdown (e.g. statistical detectors) — the template falls back to the
    raw description there.
    """
    if "lhs" not in details or "rhs" not in details:
        return None
    lhs = details["lhs"]
    rhs = details["rhs"]
    terms = [
        {"label": t.get("label", ""), "value": _fmt_num(t.get("value"))}
        for t in (details.get("terms") or [])
        if isinstance(t, dict)
    ]
    tol = details.get("tolerance")
    tol_type = details.get("tolerance_type")
    if tol is None:
        tolerance_str = ""
    elif tol_type == "relative":
        tolerance_str = f"{float(tol) * 100:g}%"
    else:
        tolerance_str = _fmt_num(tol)
    try:
        abs_diff = abs(float(lhs) - float(rhs))
        pct = f"{abs(float(details.get('diff', 0.0))) * 100:.1f}%"
    except (TypeError, ValueError):
        abs_diff, pct = None, ""
    return {
        "rule": details.get("rule", ""),
        "formula": details.get("formula", ""),
        "lhs_label": details.get("lhs_label", ""),
        "rhs_label": details.get("rhs_label", ""),
        "op_symbol": details.get("rhs_op_symbol"),
        "lhs_value": _fmt_num(lhs),
        "rhs_value": _fmt_num(rhs),
        "terms": terms,
        "abs_diff": _fmt_num(abs_diff) if abs_diff is not None else "",
        "pct": pct,
        "tolerance": tolerance_str,
    }


def _classify_references(refs: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    """Split RCA references into clickable links and plain-text context chips."""
    links: list[dict[str, str]] = []
    context: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            continue
        urls = _URL_RE.findall(ref)
        if urls:
            for url in urls:
                if url not in seen:
                    seen.add(url)
                    links.append({"url": url, "label": _friendly_link_label(url)})
        else:
            context.append(ref.strip())
    return links, context


@dataclass
class _GroupView:
    """View model for a single (detector_prefix, field_name) group."""

    label: str
    findings_in_group: list[Finding]
    total_in_group: int


def _detector_prefix(finding: Finding) -> str:
    """First detector family for the finding — see :func:`finding_group_key`."""
    return finding_group_key(finding)[0]


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


def _compute_delta(current: list[Finding], prior_ids: set[str] | None) -> dict[str, int] | None:
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
        # Same key as batch RCA (ADR 0003) — one brief section per Finding
        # Group, one RCA per Finding Group.
        buckets[finding_group_key(f)].append(f)

    groups: list[_GroupView] = []
    for (prefix, field), members in buckets.items():
        # Already sorted within bucket because we grouped post-sort. Use the
        # plain-language headline for the detector family rather than its code.
        headline = _detector_friendly(prefix)[0]
        label = f"{headline} — {_humanize_field(field)}" if field else headline
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
    details = issue.details or {}
    suppression = details.get("suppressed_by_feedback")
    prior_feedback = details.get("prior_feedback")

    families = f.detector_families
    primary_family = families[0] if families else "unknown"
    title, subtitle = _detector_friendly(primary_family)

    prior_label = prior_feedback.get("label") if isinstance(prior_feedback, dict) else None

    rca_view: dict[str, Any] | None = None
    if rca is not None:
        inline_urls: set[str] = set()
        cause_segments = _linkify_segments(str(rca.hypothesis))
        inline_urls.update(s["url"] for s in cause_segments if "url" in s)
        evidence_views: list[list[dict[str, str]]] = []
        for ev in rca.evidence:
            segs = _linkify_segments(str(ev))
            inline_urls.update(s["url"] for s in segs if "url" in s)
            evidence_views.append(segs)
        ref_links, context_refs = _classify_references(list(rca.references))
        # Don't repeat a link as a button if it already appears inline in the
        # cause or the reasoning.
        ref_links = [link for link in ref_links if link["url"] not in inline_urls]
        rca_view = {
            "cause_segments": cause_segments,
            "evidence": evidence_views,
            "links": ref_links,
            "context_refs": context_refs,
            "confidence_pct": round(float(rca.confidence or 0.0) * 100),
            "cost_label": _rca_cost_label(rca),
        }

    return {
        "finding_id": f.finding_id,
        "suppression": dict(suppression) if isinstance(suppression, dict) else None,
        "prior_feedback_human": _PRIOR_FEEDBACK_HUMAN.get(prior_label) if prior_label else None,
        "severity": issue.severity.value,
        "severity_meaning": _SEVERITY_MEANING.get(issue.severity.value, ""),
        "title": title,
        "subtitle": subtitle,
        "confidence_pct": round(float(issue.confidence or 0.0) * 100),
        "entity_id": issue.entity_id or "",
        "field_human": _humanize_field(issue.field_name or ""),
        "field_name": issue.field_name or "",
        "date_human": _humanize_date(issue.snapshot_date),
        "check_count": len(families),
        "description": issue.description or "",
        "tech": _tech_breakdown(details),
        "detectors_csv": ",".join(f.detector_sources),
        "rca": rca_view,
    }


def render_brief(
    findings: list[Finding],
    rcas: dict[str, RCAResult],
    output_path: Path,
    *,
    prior_findings_path: Path | None = None,
    dataset_label: str = "",
    max_findings: int = 500,
    cost_summary: dict[str, Any] | None = None,
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
        cost_summary: Optional run-cost dict (``total_cost_usd``, ``input_tokens``,
            ``output_tokens``, ``investigated``, ``reused``) — renders the
            estimated LLM spend for the run. Omitted/empty hides the readout.

    Returns:
        ``output_path`` (for chaining).
    """
    output_path = Path(output_path)

    # Feedback-suppressed findings (ADR 0001: downgraded, never dropped)
    # render in their own collapsed section instead of the main groups.
    suppressed_findings = [
        f for f in findings if (f.issue.details or {}).get("suppressed_by_feedback")
    ]
    active_findings = [
        f for f in findings if not (f.issue.details or {}).get("suppressed_by_feedback")
    ]

    # Sort + cap. The cap is rendered with a warning so the analyst knows
    # they're seeing the top-N rather than the full set.
    sorted_findings = sorted(active_findings, key=_sort_key)
    total_findings = len(sorted_findings)
    cap_applied = total_findings > max_findings
    visible_findings = sorted_findings[:max_findings] if cap_applied else sorted_findings

    # Grouping operates on the visible (post-cap) findings — capping first
    # then grouping prevents a 10k-finding run from rendering 10k group
    # headers.
    groups = _group_findings(visible_findings)

    # Build view models for each group. `top_severity` drives the colored
    # count dot in the sidebar table of contents.
    group_views = [
        {
            "label": g.label,
            "total_in_group": g.total_in_group,
            "top_severity": g.findings_in_group[0].issue.severity.value,
            "findings_in_group": [_finding_view(f, rcas) for f in g.findings_in_group],
        }
        for g in groups
    ]

    suppressed_views = [_finding_view(f, rcas) for f in sorted(suppressed_findings, key=_sort_key)]

    # Delta vs. prior — None when no prior path was given. Computed over the
    # FULL findings list (pre-cap, including feedback-suppressed): a finding
    # that was suppressed or capped out of the visible set is still open, and
    # must not be reported as "cleared up".
    prior_ids = _read_prior_finding_ids(prior_findings_path)
    delta_counts = _compute_delta(findings, prior_ids)

    summary_counts = _summary_counts(visible_findings)

    # Run-cost readout — only when the run actually did (or reused) RCA work.
    cost_view: dict[str, Any] | None = None
    if cost_summary and (cost_summary.get("investigated") or cost_summary.get("reused")):
        # cost_known=False means the client reported no estimate for at least
        # one investigation — render "n/a", never a misleading "$0.00".
        cost_view = {
            "total_usd": (
                _fmt_usd(cost_summary.get("total_cost_usd") or 0.0)
                if cost_summary.get("cost_known", True)
                else "n/a"
            ),
            "input_tokens": f"{int(cost_summary.get('input_tokens') or 0):,}",
            "output_tokens": f"{int(cost_summary.get('output_tokens') or 0):,}",
            "investigated": int(cost_summary.get("investigated") or 0),
            "reused": int(cost_summary.get("reused") or 0),
        }

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
        suppressed=suppressed_views,
        total_findings=total_findings,
        visible_count=len(visible_findings),
        cap_applied=cap_applied,
        max_findings=max_findings,
        cost=cost_view,
        styles_inline=styles_inline,
    )

    _atomic_write_text(output_path, rendered)
    return output_path
