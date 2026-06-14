"""The `lens` command-line interface.

Subcommands:

* ``lens run <config.yaml>`` — one scheduled batch: detect → group RCA →
  brief. Prints the markdown digest to stdout (cron output = Slack paste).
* ``lens serve`` — serve the latest brief + capture one-click feedback.
* ``lens feedback <finding_id> <label>`` — append an analyst verdict
  (delegates to :mod:`lens.brief.feedback`).
* ``lens brief <findings.json>`` — render the markdown digest from a
  findings file (delegates to :mod:`lens.brief.markdown`).

The ``python -m lens.brief.markdown`` / ``python -m lens.brief.feedback`` /
``python -m lens.brief.serve`` module forms remain available aliases.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _cmd_run(args: argparse.Namespace) -> int:
    from lens.batch import run_batch
    from lens.run_config import load_run_config

    try:
        cfg = load_run_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = run_batch(cfg, run_id=args.run_id)

    suppressed = sum(
        1 for f in result.findings if (f.issue.details or {}).get("suppressed_by_feedback")
    )
    print(result.markdown_digest)
    print(
        f"run {result.run_id}: {len(result.findings)} findings"
        + (f" ({suppressed} suppressed by feedback)" if suppressed else "")
        + f"; RCA on {result.rca_groups_investigated} group(s)"
        + (
            f", {result.rca_groups_skipped_below_floor} below severity floor"
            if result.rca_groups_skipped_below_floor
            else ""
        )
        + (
            f", {result.rca_groups_skipped_over_cap} skipped over max_investigations cap"
            if result.rca_groups_skipped_over_cap
            else ""
        ),
        file=sys.stderr,
    )
    print(f"findings: {result.findings_path}", file=sys.stderr)
    print(f"brief:    {result.brief_html_path}", file=sys.stderr)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from lens.brief.serve import main as serve_main

    argv: list[str] = [
        "--output-dir",
        str(args.output_dir),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.feedback is not None:
        argv += ["--feedback", str(args.feedback)]
    return serve_main(argv)


def _cmd_feedback(args: argparse.Namespace) -> int:
    from lens.brief.feedback import main as feedback_main

    return feedback_main(args.rest)


def _cmd_brief(args: argparse.Namespace) -> int:
    from lens.brief.markdown import main as markdown_main

    return markdown_main(args.rest)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lens",
        description="LENS — data quality surveillance for commercial lending data.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable INFO-level logging.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Execute one batch run from a run-config YAML.")
    p_run.add_argument("config", type=Path, help="Path to the run-config YAML.")
    p_run.add_argument("--run-id", default=None, help="Explicit run id (default: generated).")
    p_run.set_defaults(func=_cmd_run)

    p_serve = sub.add_parser("serve", help="Serve the latest brief + capture feedback.")
    p_serve.add_argument("--output-dir", type=Path, default=Path("out"))
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8377)
    p_serve.add_argument("--feedback", type=Path, default=None)
    p_serve.set_defaults(func=_cmd_serve)

    p_fb = sub.add_parser(
        "feedback",
        help="Append an analyst verdict (see `lens feedback --help`).",
        add_help=False,
    )
    p_fb.add_argument("rest", nargs=argparse.REMAINDER)
    p_fb.set_defaults(func=_cmd_feedback)

    p_brief = sub.add_parser(
        "brief",
        help="Render the markdown digest from a findings.json (see `lens brief --help`).",
        add_help=False,
    )
    p_brief.add_argument("rest", nargs=argparse.REMAINDER)
    p_brief.set_defaults(func=_cmd_brief)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover - exercised via console script
    sys.exit(main())
