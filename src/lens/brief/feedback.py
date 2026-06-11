"""Analyst feedback capture for LENS findings.

Writes one-click analyst labels (``real | false_positive | needs_more``) to a
JSONL file. This is the programmatic path that backs the HTML brief's one-click
buttons (T10) and any future Slack / analyst-tooling integrations — the same
JSONL can be appended to from a script, a server, or this CLI.

Concurrency safety: appends use ``fcntl.flock(LOCK_EX)`` on POSIX so concurrent
writers can't interleave bytes mid-line. On Windows the lock is silently
skipped (``fcntl`` is unavailable); single-writer is assumed there.

HTML brief button payload format
--------------------------------
The HTML brief renders three buttons per Finding that wrap a ``mailto:`` URL
with a pre-filled JSON body. The body should be a single JSONL line matching
the schema written by :func:`append_feedback`::

    {"finding_id": "<uuid>", "label": "real", "ts": "<iso>",
     "analyst": null, "note": null}

An analyst clicking the button opens their mail client; the recipient (or a
future server-side handler) pipes the JSON body through this module's CLI to
append it to ``feedback.jsonl``.
"""

from __future__ import annotations

import argparse
import enum
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:  # POSIX-only; on Windows we degrade gracefully.
    import fcntl  # type: ignore[import-not-found]

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows path
    _HAS_FCNTL = False


class FeedbackLabel(str, enum.Enum):
    """Analyst verdict on a Finding."""

    REAL = "real"
    FALSE_POSITIVE = "false_positive"
    NEEDS_MORE = "needs_more"


def _coerce_label(label: FeedbackLabel | str) -> FeedbackLabel:
    if isinstance(label, FeedbackLabel):
        return label
    try:
        return FeedbackLabel(label)
    except ValueError as exc:
        valid = ", ".join(repr(m.value) for m in FeedbackLabel)
        raise ValueError(
            f"Unknown feedback label {label!r}; expected one of {valid}"
        ) from exc


def append_feedback(
    finding_id: str,
    label: FeedbackLabel | str,
    output_path: Path | str,
    *,
    analyst: str | None = None,
    note: str | None = None,
    ts: datetime | None = None,
    entity_id: str | None = None,
    field_name: str | None = None,
    detector_sources: list[str] | None = None,
) -> dict[str, Any]:
    """Append one feedback entry to ``output_path`` as a JSONL line.

    The append is wrapped in ``fcntl.flock(LOCK_EX)`` on POSIX so concurrent
    callers (threads, processes, scripts) can't corrupt the file by writing
    partial lines that interleave. On Windows the flock call is skipped
    silently — single-writer is assumed there for MVP.

    Args:
        finding_id: Stable v5 UUID identifying the Finding (see
            :func:`lens.types.compute_finding_id`).
        label: ``FeedbackLabel`` member or its string value (``"real"``,
            ``"false_positive"``, ``"needs_more"``).
        output_path: Destination JSONL file. Created if missing; appended
            to if existing.
        analyst: Optional analyst identifier (email, name, slack handle).
        note: Optional free-text rationale.
        ts: Override timestamp (defaults to ``datetime.now(timezone.utc)``).
        entity_id: Optional entity the Finding refers to. Recording it makes
            the entry self-contained for the suppression loop
            (:mod:`lens.feedback_loop`) — otherwise the consumer must resolve
            the finding_id against prior findings files.
        field_name: Optional field the Finding refers to (see ``entity_id``).
        detector_sources: Optional detector identities that flagged the
            Finding; scopes false-positive suppression to those families.

    Returns:
        The entry dict that was written.

    Raises:
        ValueError: If ``label`` is not a recognised :class:`FeedbackLabel`.
    """

    coerced = _coerce_label(label)
    entry: dict[str, Any] = {
        "finding_id": finding_id,
        "label": coerced.value,
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "analyst": analyst,
        "note": note,
    }
    if entity_id is not None:
        entry["entity_id"] = entity_id
    if field_name is not None:
        entry["field_name"] = field_name
    if detector_sources:
        entry["detector_sources"] = list(detector_sources)
    line = (json.dumps(entry) + "\n").encode("utf-8")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "ab") as f:
        if _HAS_FCNTL:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_UN)

    return entry


def format_button_url(
    finding_id: str,
    label: FeedbackLabel | str,
    *,
    recipient: str = "lens-feedback@example.com",
) -> str:
    """Build a ``mailto:`` URL for the HTML brief's one-click feedback buttons.

    The body is a single JSONL line that an analyst (or a future server-side
    handler) can pipe through :func:`append_feedback` to record the verdict.

    Args:
        finding_id: Finding's stable v5 UUID.
        label: Verdict — :class:`FeedbackLabel` or its string value.
        recipient: Mailbox the button addresses. Defaults to a placeholder;
            production deployments should pass their analyst-feedback inbox.

    Returns:
        ``mailto:<recipient>?subject=...&body=<url-encoded JSON line>``
    """

    coerced = _coerce_label(label)
    payload = json.dumps({"finding_id": finding_id, "label": coerced.value})
    subject = f"LENS feedback: {coerced.value} — {finding_id}"
    return (
        f"mailto:{recipient}"
        f"?subject={quote(subject)}"
        f"&body={quote(payload)}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lens.brief.feedback",
        description=(
            "Append one analyst feedback entry to a JSONL file. "
            "Safe under concurrent invocation on POSIX via fcntl.flock."
        ),
    )
    parser.add_argument("finding_id", help="Stable v5 UUID for the Finding.")
    parser.add_argument(
        "label",
        help="Verdict: real | false_positive | needs_more.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./feedback.jsonl"),
        help="Destination JSONL file (default: ./feedback.jsonl).",
    )
    parser.add_argument(
        "--analyst",
        default=None,
        help="Optional analyst identifier (email, name, slack handle).",
    )
    parser.add_argument(
        "--note",
        default=None,
        help="Optional free-text rationale.",
    )
    parser.add_argument(
        "--entity",
        default=None,
        help="Entity the Finding refers to (makes the entry self-contained "
        "for the suppression loop).",
    )
    parser.add_argument(
        "--field",
        default=None,
        help="Field the Finding refers to (see --entity).",
    )
    parser.add_argument(
        "--detector",
        action="append",
        default=None,
        help="Detector identity that flagged the Finding; repeatable. Scopes "
        "false-positive suppression to these families.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m lens.brief.feedback <finding_id> <label>``.

    Returns 0 on success, 2 on validation error (unknown label).
    """

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        entry = append_feedback(
            args.finding_id,
            args.label,
            args.output,
            analyst=args.analyst,
            note=args.note,
            entity_id=args.entity,
            field_name=args.field,
            detector_sources=args.detector,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(entry))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess/CLI smoke
    sys.exit(main())
