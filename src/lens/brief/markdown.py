"""Top-5 markdown brief — the Slack-pasteable / terminal-``cat``-able alternative.

The HTML brief (``lens.brief.html``) is the primary delivery mode but it lives
as a static file on disk. When the analyst is in a hurry — the worst-case
LENS workflow per the product review — opening a browser tab is friction they
don't want. This module gives the same top findings as a markdown blob you can
paste straight into Slack, a GitHub issue body, an email, or just print to the
terminal.

Two entry points:

* ``render_brief_summary(findings, rcas)`` — a pure function returning a
  markdown string. Caller decides what to do with it (print, paste, attach).
* ``python -m lens.brief.markdown <findings.json> [<rcas_dir>] [--top N]
  [--dataset LABEL]`` — CLI that reads the orchestrator's
  ``findings.{run_id}.json`` plus an optional ``rca/<run_id>/`` directory of
  per-finding RCA JSON files, and prints the rendered markdown to stdout.

This module deliberately does NOT write to disk. The orchestrator already has
that responsibility for ``findings.json`` / ``rca/<finding_id>.json``; the
markdown summary is downstream of those files, not a sibling.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from lens.types import Finding, RCAResult, Severity

# Severity rank — higher is more urgent. Used as the primary sort key for
# selecting "top N" findings. Kept inline rather than imported from scoring.py
# to avoid coupling rendering to scoring internals.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 3,
    Severity.ERROR: 2,
    Severity.WARNING: 1,
    Severity.INFO: 0,
}

# Hypothesis text is truncated to this many characters in the one-line summary.
# Anything longer is a paragraph the analyst should open the HTML brief / RCA
# JSON to read in full.
_HYPOTHESIS_MAX_CHARS = 200


def _severity_rank(finding: Finding) -> int:
    """Lookup severity rank for sorting; unknown severities sort lowest."""
    return _SEVERITY_RANK.get(finding.issue.severity, -1)


def _severity_label(finding: Finding) -> str:
    """Render severity as upper-case label (e.g. ``CRITICAL``)."""
    return finding.issue.severity.value.upper()


def _format_snapshot_date(snap: Any) -> str:
    """Render a snapshot date as ``YYYY-MM-DD`` regardless of input shape.

    Inputs may be: ``datetime``, ``date``, an ISO-formatted string, or ``None``.
    A ``None`` returns the empty string so the caller can decide how to format
    the surrounding text.
    """
    if snap is None:
        return ""
    if isinstance(snap, datetime):
        return snap.date().isoformat()
    if isinstance(snap, date):
        return snap.isoformat()
    # Already-stringified (e.g. when loading from JSON via the CLI).
    return str(snap)


def _first_http_reference(refs: list[str] | None) -> str | None:
    """Return the first reference entry that starts with ``http``.

    The RCA agent's ``references`` list mixes commit URLs with prose evidence
    strings ("row 42 differs"). For the markdown summary we want exactly one
    clickable URL — the most likely candidate is the first HTTP-shaped entry,
    which the agent emits as the primary commit URL.
    """
    if not refs:
        return None
    for ref in refs:
        if isinstance(ref, str) and ref.startswith("http"):
            return ref
    return None


def _truncate_hypothesis(text: str) -> str:
    """Trim hypothesis text to fit on one summary line.

    If the text already fits, return it unchanged. Otherwise cut to
    ``_HYPOTHESIS_MAX_CHARS`` and append a single horizontal-ellipsis (the
    spec literally specifies the trailing ``…`` character, not three dots).
    """
    if len(text) <= _HYPOTHESIS_MAX_CHARS:
        return text
    return text[:_HYPOTHESIS_MAX_CHARS] + "…"


def _confidence_pct(confidence: float) -> int:
    """Round confidence in [0, 1] to a 0-100 integer for display."""
    return int(round(confidence * 100))


def _format_cost_footer(cost_summary: dict[str, Any] | None) -> str | None:
    """One-line 'est. LLM cost' footer for the digest, or ``None`` to omit."""
    if not cost_summary:
        return None
    investigated = int(cost_summary.get("investigated") or 0)
    reused = int(cost_summary.get("reused") or 0)
    if not (investigated or reused):
        return None
    total = float(cost_summary.get("total_cost_usd") or 0.0)
    usd = f"${total:,.2f}" if total >= 1 else f"${total:.4f}"
    detail = f"{investigated} investigated"
    if reused:
        detail += f", {reused} reused free"
    return (
        f"_Estimated LLM cost this run: {usd} ({detail}) — "
        "Claude Code estimate, not authoritative billing._"
    )


def render_brief_summary(
    findings: list[Finding],
    rcas: dict[str, RCAResult] | None = None,
    *,
    top_n: int = 5,
    dataset_label: str = "",
    date_iso: str | None = None,
    cost_summary: dict[str, Any] | None = None,
) -> str:
    """Render the top-N findings as a Slack-pasteable markdown digest.

    Args:
        findings: All findings from an orchestrator run. The function sorts +
            selects the top-N internally; pass the full list.
        rcas: Optional mapping of ``finding_id`` to RCA result. Findings without
            a matching RCA get a ``"(no RCA yet)"`` placeholder.
        top_n: Maximum number of findings to render in the digest.
        dataset_label: Free-text label shown in the header (e.g. "Q1 lending").
        date_iso: Optional ISO date string for the header. Defaults to today.
        cost_summary: Optional run-cost dict; appends an estimated-LLM-cost
            footer line when the run did (or reused) RCA work.

    Returns:
        A markdown-formatted string ready for stdout / clipboard / Slack paste.
    """
    if date_iso is None:
        date_iso = date.today().isoformat()

    # Sort by (severity_rank desc, confidence desc, finding_id asc).
    # The finding_id tie-breaker is what makes the output stable when two
    # findings have identical severity and confidence — without it the
    # ordering would depend on whatever order the orchestrator happened to
    # iterate detectors in.
    sorted_findings = sorted(
        findings,
        key=lambda f: (
            -_severity_rank(f),
            -f.issue.confidence,
            f.finding_id,
        ),
    )
    top = sorted_findings[:top_n]

    header_label = dataset_label if dataset_label else "(unlabeled)"
    lines: list[str] = [
        f"## LENS Brief — {header_label} — {date_iso}",
        "",
        f"{len(findings)} total findings; top {min(top_n, len(findings))} shown.",
        "",
    ]

    for i, finding in enumerate(top, start=1):
        issue = finding.issue
        severity = _severity_label(finding)
        entity = issue.entity_id or ""
        field = issue.field_name or ""
        snap = _format_snapshot_date(issue.snapshot_date)
        confidence = _confidence_pct(issue.confidence)

        rca = rcas.get(finding.finding_id) if rcas else None
        if rca is None:
            hypothesis_text = "(no RCA yet)"
            reference_url: str | None = None
        else:
            hypothesis_text = _truncate_hypothesis(rca.hypothesis or "(empty hypothesis)")
            reference_url = _first_http_reference(rca.references)

        line = (
            f"{i}. **[{severity}]** "
            f"entity=`{entity}` field=`{field}` date=`{snap}` "
            f"— confidence: {confidence}% "
            f"— *{hypothesis_text}*"
        )
        if reference_url:
            line += f" [→ {reference_url}]"
        lines.append(line)

    cost_footer = _format_cost_footer(cost_summary)
    if cost_footer:
        lines.append("")
        lines.append(cost_footer)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _finding_from_jsonable(record: dict[str, Any]) -> Finding:
    """Reconstruct a ``Finding`` from its ``_finding_to_jsonable`` output.

    The orchestrator writes findings via ``_finding_to_jsonable``; this is the
    inverse. We only need enough fidelity to render markdown — i.e. fields the
    summary actually consumes (severity, entity_id, field_name, snapshot_date,
    confidence, finding_id). Other Issue fields are filled in with defaults.
    """
    from lens.types import Issue  # local import to keep module top-level light

    issue_data = record.get("issue", {})
    severity_value = issue_data.get("severity", "info")
    try:
        severity = Severity(severity_value)
    except ValueError:
        severity = Severity.INFO

    issue = Issue(
        check_name=issue_data.get("check_name", ""),
        severity=severity,
        entity_id=issue_data.get("entity_id"),
        field_name=issue_data.get("field_name"),
        # Leave as string; _format_snapshot_date handles both str and date.
        snapshot_date=issue_data.get("snapshot_date"),
        description=issue_data.get("description", ""),
        details=issue_data.get("details", {}) or {},
        confidence=float(issue_data.get("confidence", 0.0)),
        detector_source=issue_data.get("detector_source", ""),
        finding_id=issue_data.get("finding_id", record.get("finding_id", "")),
    )
    return Finding(
        issue=issue,
        detector_sources=list(record.get("detector_sources", [])),
        detected_at=None,
        run_id=record.get("run_id", ""),
    )


def _load_findings(path: Path) -> list[Finding]:
    """Load and parse a ``findings.{run_id}.json`` file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} did not contain a JSON array of findings")
    return [_finding_from_jsonable(rec) for rec in data]


def _load_rcas(rca_dir: Path) -> dict[str, RCAResult]:
    """Index every ``*.json`` under ``rca_dir`` by ``finding_id``.

    Files written by the RCA agent are named ``{finding_id}.json`` and the
    record itself contains ``finding_id`` — both should agree, but we trust
    the in-file value to be authoritative.
    """
    rcas: dict[str, RCAResult] = {}
    if not rca_dir.exists():
        return rcas
    for json_path in sorted(rca_dir.glob("*.json")):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        finding_id = payload.get("finding_id") or json_path.stem

        def _opt_float(v: Any) -> float | None:
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _opt_int(v: Any) -> int | None:
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        model = payload.get("model")
        rcas[finding_id] = RCAResult(
            finding_id=finding_id,
            hypothesis=payload.get("hypothesis", ""),
            evidence=list(payload.get("evidence", []) or []),
            confidence=float(payload.get("confidence", 0.0)),
            references=list(payload.get("references", []) or []),
            cost_usd=_opt_float(payload.get("cost_usd")),
            input_tokens=_opt_int(payload.get("input_tokens")),
            output_tokens=_opt_int(payload.get("output_tokens")),
            model=str(model) if model is not None else None,
        )
    return rcas


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — see module docstring for usage."""
    parser = argparse.ArgumentParser(
        prog="python -m lens.brief.markdown",
        description=(
            "Render the top-N findings from a LENS run as a Slack-pasteable "
            "markdown digest. Writes to stdout."
        ),
    )
    parser.add_argument(
        "findings_json",
        type=Path,
        help="Path to a findings.{run_id}.json file written by the orchestrator.",
    )
    parser.add_argument(
        "rcas_dir",
        type=Path,
        nargs="?",
        default=None,
        help="Optional directory containing per-finding RCA JSON files.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="Number of findings to include in the digest (default: 5).",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="",
        help="Free-text label shown in the brief header.",
    )

    args = parser.parse_args(argv)

    findings_path: Path = args.findings_json
    if not findings_path.exists():
        print(f"error: findings file not found: {findings_path}", file=sys.stderr)
        return 2

    try:
        findings = _load_findings(findings_path)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"error: could not parse {findings_path}: {exc}", file=sys.stderr)
        return 2

    rcas: dict[str, RCAResult] | None = None
    if args.rcas_dir is not None:
        rcas = _load_rcas(args.rcas_dir)

    output = render_brief_summary(
        findings,
        rcas,
        top_n=args.top,
        dataset_label=args.dataset,
    )
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())
