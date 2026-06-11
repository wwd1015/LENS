"""Consume analyst feedback: downgrade false-positive findings, never drop.

This is the consumer side of ``feedback.jsonl`` (the capture side lives in
:mod:`lens.brief.feedback`). Per ADR 0001:

* An unexpired ``false_positive`` verdict downgrades future findings on the
  same ``(entity_id, field_name)`` to :attr:`Severity.INFO` — but only when
  every detector family flagging the new finding was already judged FP for
  that point. A new, independent detector family always breaks through.
* Verdicts expire after ``expiry_days``; a muted series that breaks for real
  later resurfaces automatically.
* Downgraded findings stay in findings.json with
  ``details["suppressed_by_feedback"]`` carrying the original severity, so
  the brief can render them in a collapsed section and nothing is ever
  silently hidden.
* A later ``real`` / ``needs_more`` verdict on the same point clears earlier
  false-positive verdicts (the analyst changed their mind).

Feedback entries written by the serve handler / current CLI carry
``entity_id`` / ``field_name`` / ``detector_sources`` inline. Older entries
only carry ``finding_id``; those are resolved by scanning prior
``findings.*.json`` files in the output directory. Entries that cannot be
resolved are skipped with a debug log — feedback application must never
break a detection run.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lens.types import Finding, Severity, detector_family

logger = logging.getLogger(__name__)

_FP_LABEL = "false_positive"
_CLEARING_LABELS = {"real", "needs_more"}

# Sentinel family meaning "suppress any detector family" — used when a
# false-positive verdict carries no detector information at all.
_WILDCARD = "*"


def load_entries(path: Path) -> list[dict[str, Any]]:
    """Read a feedback JSONL file, skipping malformed lines."""
    entries: list[dict[str, Any]] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return entries
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("feedback: skipping malformed line %d in %s", lineno, path)
            continue
        if isinstance(obj, dict) and obj.get("finding_id") and obj.get("label"):
            entries.append(obj)
    return entries


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp; naive values are assumed UTC."""
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _build_finding_index(output_dir: Path) -> dict[str, dict[str, Any]]:
    """Index prior findings files: finding_id → {entity_id, field_name, detector_sources}.

    Scans every ``findings.*.json`` in ``output_dir`` except the ``latest``
    symlink (its target is scanned under its real name). Best-effort — a
    malformed file is skipped, not raised.
    """
    index: dict[str, dict[str, Any]] = {}
    if output_dir is None or not Path(output_dir).is_dir():
        return index
    for path in sorted(Path(output_dir).glob("findings.*.json")):
        if path.name == "findings.latest.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        for rec in data:
            if not isinstance(rec, dict):
                continue
            fid = rec.get("finding_id")
            issue = rec.get("issue") or {}
            if not fid or not isinstance(issue, dict):
                continue
            index[str(fid)] = {
                "entity_id": issue.get("entity_id"),
                "field_name": issue.get("field_name"),
                "detector_sources": list(rec.get("detector_sources") or []),
            }
    return index


def _resolve_entry(
    entry: dict[str, Any],
    index_getter: Any,
) -> tuple[str, str, list[str]] | None:
    """Pull ``(entity_id, field_name, detector_sources)`` out of an entry.

    Inline fields win; otherwise the entry's finding_id is looked up in prior
    findings files via ``index_getter()`` (a lazy thunk so the scan only
    happens when an old-format entry actually needs it).
    """
    entity = entry.get("entity_id")
    field = entry.get("field_name")
    detectors = entry.get("detector_sources")
    if not (entity and field):
        resolved = index_getter().get(str(entry.get("finding_id")))
        if resolved is None:
            return None
        entity = entity or resolved.get("entity_id")
        field = field or resolved.get("field_name")
        if not detectors:
            detectors = resolved.get("detector_sources")
    if not (entity and field):
        return None
    if isinstance(detectors, str):
        detectors = [detectors]
    return str(entity), str(field), [str(d) for d in (detectors or [])]


def apply_feedback(
    findings: list[Finding],
    *,
    feedback_path: Path,
    output_dir: Path | None = None,
    expiry_days: int = 90,
    now: datetime | None = None,
) -> list[Finding]:
    """Return ``findings`` with feedback verdicts applied.

    Every finding whose ``(entity_id, field_name)`` has a recorded verdict is
    annotated with ``details["prior_feedback"]`` (latest verdict, any label).
    Findings fully covered by unexpired false-positive verdicts are downgraded
    to INFO with ``details["suppressed_by_feedback"]``.
    """
    entries = load_entries(feedback_path)
    if not entries:
        return findings

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=expiry_days)

    # Lazy prior-findings index — only built if an entry needs resolution.
    index_cache: dict[str, dict[str, Any]] | None = None

    def _index() -> dict[str, dict[str, Any]]:
        nonlocal index_cache
        if index_cache is None:
            index_cache = _build_finding_index(Path(output_dir)) if output_dir else {}
        return index_cache

    # Process verdicts in chronological order so "latest wins" semantics fall
    # out naturally: an FP accumulates suppressed families; a later real /
    # needs_more verdict on the same point clears them.
    dated = [(e, _parse_ts(e.get("ts"))) for e in entries]
    dated.sort(key=lambda pair: pair[1] or datetime.min.replace(tzinfo=UTC))

    fp_families: dict[tuple[str, str], set[str]] = {}
    fp_meta: dict[tuple[str, str], dict[str, Any]] = {}
    latest_verdict: dict[tuple[str, str], dict[str, Any]] = {}

    for entry, ts in dated:
        resolved = _resolve_entry(entry, _index)
        if resolved is None:
            logger.debug(
                "feedback: cannot resolve entry for finding_id=%s; skipping",
                entry.get("finding_id"),
            )
            continue
        entity, field, detectors = resolved
        key = (entity, field)
        label = str(entry.get("label"))

        latest_verdict[key] = {
            "label": label,
            "ts": entry.get("ts"),
            "analyst": entry.get("analyst"),
        }

        if label in _CLEARING_LABELS:
            fp_families.pop(key, None)
            fp_meta.pop(key, None)
            continue
        if label != _FP_LABEL:
            continue
        # Expired FP verdicts neither suppress nor clear.
        if ts is not None and ts < cutoff:
            continue
        families = {detector_family(d) for d in detectors} if detectors else {_WILDCARD}
        fp_families.setdefault(key, set()).update(families)
        fp_meta[key] = {
            "label": label,
            "ts": entry.get("ts"),
            "analyst": entry.get("analyst"),
            "expires": (ts + timedelta(days=expiry_days)).isoformat() if ts else None,
        }

    out: list[Finding] = []
    for finding in findings:
        issue = finding.issue
        key = (str(issue.entity_id), str(issue.field_name))
        verdict = latest_verdict.get(key)
        suppressing = fp_families.get(key)

        new_details = dict(issue.details or {})
        changed = False

        if verdict is not None:
            new_details["prior_feedback"] = dict(verdict)
            changed = True

        if (
            suppressing
            and issue.severity is not Severity.INFO
            and (
                _WILDCARD in suppressing
                or set(finding.detector_families) <= suppressing
            )
        ):
            meta = dict(fp_meta.get(key) or {})
            meta["original_severity"] = issue.severity.value
            new_details["suppressed_by_feedback"] = meta
            new_issue = dataclasses.replace(
                issue,
                severity=Severity.INFO,
                details=new_details,
            )
            out.append(dataclasses.replace(finding, issue=new_issue))
            continue

        if changed:
            out.append(
                dataclasses.replace(
                    finding,
                    issue=dataclasses.replace(issue, details=new_details),
                )
            )
        else:
            out.append(finding)
    return out


def is_suppressed(finding: Finding) -> bool:
    """True when ``finding`` was downgraded by :func:`apply_feedback`."""
    return bool((finding.issue.details or {}).get("suppressed_by_feedback"))
