"""Per-finding root-cause analysis agent.

The agent gathers structured context for a single :class:`lens.types.Finding`
— a contrast set of anomalous vs. prior rows, lineage and rule pages from
:class:`lens.wiki.cache.WikiCache`, and recent git commits on each
producing-code path mentioned in the wiki — and asks an LLM to synthesize a
single :class:`lens.types.RCAResult`.

LLM calls go through the same :class:`LLMClient` protocol the wiki ingestion
worker uses (:mod:`lens.wiki.ingest`). The default implementation,
:class:`lens.wiki.ingest.ClaudeCodeClient`, shells out to ``claude -p ...`` —
LENS environments authenticate via Claude Code SSO, so the Anthropic SDK is
not used.

The agent never touches network or filesystem state outside the supplied
``output_dir``; ``.save()`` writes the result as
``output_dir/rca/<run_id>/<finding_id>.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

import polars as pl

from lens.rca.git_links import commit_url
from lens.rca.prompts import RCA_PROMPT
from lens.types import Finding, RCAResult
from lens.wiki.cache import WikiCache
from lens.wiki.ingest import ClaudeCodeClient, LLMClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers — context building (pure-functional, easy to test on their own)
# ---------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_ANY_FENCE_RE = re.compile(r"```(?:[a-zA-Z]+)?\s*\n(.*?)```", re.DOTALL)


def _summarize_finding(finding: Finding) -> str:
    """One-paragraph description of the wrapped :class:`Issue`."""
    issue = finding.issue
    detectors = ", ".join(finding.detector_sources) or issue.detector_source or issue.check_name
    lines = [
        f"finding_id: {finding.finding_id}",
        f"check: {issue.check_name}",
        f"severity: {issue.severity.value}",
        f"confidence: {issue.confidence:.3f}",
        f"detector_sources: {detectors}",
        f"entity_id: {issue.entity_id}",
        f"field_name: {issue.field_name}",
        f"snapshot_date: {issue.snapshot_date}",
        f"description: {issue.description}",
    ]
    if issue.details:
        lines.append(f"details: {json.dumps(issue.details, default=str, sort_keys=True)}")
    return "\n".join(lines)


def _summarize_group(group: list[Finding], rep: Finding) -> str:
    """Describe the Finding Group the representative belongs to (ADR 0003).

    Batch RCA runs once per group, so the prompt must convey the blast
    radius: how many findings share this (detector family, field), which
    entities, and the severity mix. Capped entity list keeps the prompt
    bounded on fan-out incidents.
    """
    others = [f for f in group if f.finding_id != rep.finding_id]
    if not others and len(group) <= 1:
        return ""
    sev_counts: dict[str, int] = {}
    for f in group:
        sev_counts[f.issue.severity.value] = sev_counts.get(f.issue.severity.value, 0) + 1
    entities = []
    for f in group:
        eid = f.issue.entity_id
        if eid and eid not in entities:
            entities.append(eid)
    shown = entities[:20]
    more = len(entities) - len(shown)
    lines = [
        "",
        "group_context: this finding is the representative of a Finding Group "
        "investigated as ONE incident.",
        f"group_size: {len(group)} findings",
        f"group_severities: {json.dumps(sev_counts, sort_keys=True)}",
        f"group_entities: {', '.join(str(e) for e in shown)}"
        + (f" (+{more} more)" if more > 0 else ""),
    ]
    dates = sorted(
        {
            str(
                f.issue.snapshot_date.date()
                if hasattr(f.issue.snapshot_date, "date")
                else f.issue.snapshot_date
            )
            for f in group
            if f.issue.snapshot_date is not None
        }
    )
    if dates:
        lines.append(f"group_snapshot_dates: {dates[0]} .. {dates[-1]} ({len(dates)} distinct)")
    lines.append(
        "Prefer hypotheses that explain the WHOLE group (one upstream cause) "
        "over per-entity explanations."
    )
    return "\n".join(lines)


def _format_rows(rows: list[dict[str, Any]], header: str) -> str:
    """Format a list of polars rows as a plain-text block."""
    if not rows:
        return f"{header}\n(none available)"
    out_lines = [header]
    for row in rows:
        ordered = ", ".join(f"{k}={row[k]!r}" for k in row)
        out_lines.append(f"- {ordered}")
    return "\n".join(out_lines)


def _sample_rows(
    lf: pl.LazyFrame,
    *,
    entity_id: str | None,
    field_name: str | None,
    snapshot_date: Any,
    snapshot_col: str,
    entity_col: str,
    n: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(anomalous_rows, contrast_rows)`` best-effort.

    Anomalous = rows at ``snapshot_date`` (filtered by entity if available).
    Contrast = up to ``n`` rows from prior snapshots for the same entity.

    All polars failures are swallowed and degrade to empty lists — RCA must
    not crash because the analyst's sample query was malformed.

    Filters are pushed into the LazyFrame BEFORE ``.collect()`` so production-
    sized sources don't materialize their full row set just to sample a
    handful around one snapshot. Schema inspection uses ``lf.collect_schema()``
    which doesn't trigger materialization.
    """
    try:
        cols = lf.collect_schema().names()
    except Exception as exc:  # noqa: BLE001 - schema probe best-effort
        logger.debug("rca: could not inspect source schema: %s", exc)
        return [], []

    have_snapshot = snapshot_col in cols
    have_entity = entity_col in cols and entity_id is not None

    # Push filters BEFORE materialization. The anomalous and contrast slices
    # each materialize at most `n` rows.
    base = lf
    if have_entity:
        base = base.filter(pl.col(entity_col) == entity_id)

    try:
        df = base.collect()
    except Exception as exc:  # noqa: BLE001 - best-effort sampling
        logger.debug("rca: could not materialize filtered source: %s", exc)
        return [], []

    anomalous: list[dict[str, Any]] = []
    contrast: list[dict[str, Any]] = []

    # Anomalous rows: at the snapshot date.
    try:
        if have_snapshot and snapshot_date is not None:
            mask = pl.col(snapshot_col) == snapshot_date
            if have_entity:
                mask = mask & (pl.col(entity_col) == entity_id)
            anomalous_df = df.filter(mask).head(n)
            anomalous = anomalous_df.to_dicts()
    except Exception as exc:  # noqa: BLE001
        logger.debug("rca: anomalous-row sampling failed: %s", exc)

    # Contrast rows: prior snapshots for the same entity.
    try:
        if have_snapshot and snapshot_date is not None:
            mask = pl.col(snapshot_col) < snapshot_date
            if have_entity:
                mask = mask & (pl.col(entity_col) == entity_id)
            contrast_df = df.filter(mask).sort(snapshot_col, descending=True).head(n)
            contrast = contrast_df.to_dicts()
        elif have_entity:
            # No snapshot column → fall back to other rows for the entity.
            contrast = df.filter(pl.col(entity_col) == entity_id).head(n).to_dicts()
    except Exception as exc:  # noqa: BLE001
        logger.debug("rca: contrast-row sampling failed: %s", exc)

    # Avoid duplicating anomalous rows in the contrast section.
    if anomalous:
        anomalous_set = {tuple(sorted(r.items(), key=lambda kv: kv[0])) for r in anomalous}
        contrast = [
            r
            for r in contrast
            if tuple(sorted(r.items(), key=lambda kv: kv[0])) not in anomalous_set
        ]
    return anomalous, contrast


def _pick_source_lf(
    sources: dict[str, pl.LazyFrame],
    issue: Any,
) -> pl.LazyFrame | None:
    """Best-effort: pick the most-relevant LazyFrame for sampling.

    Preference order:
      1. Source whose name is recorded in ``issue.details['__source__']``
         (the orchestrator stamps this on single-source issues).
      2. Source whose name matches ``issue.detector_source`` prefix
         (e.g. a check_name namespaced by source).
      3. The first source containing ``issue.field_name`` as a column.
      4. The first source in the dict, if any.
    """
    if not sources:
        return None

    details = issue.details or {}
    src_name = details.get("__source__")
    if isinstance(src_name, str) and src_name in sources:
        return sources[src_name]

    field = issue.field_name
    if field:
        for _name, lf in sources.items():
            try:
                cols = lf.collect_schema().names()
            except Exception:  # noqa: BLE001
                continue
            if field in cols:
                return lf

    # Fallback: first source in insertion order.
    return next(iter(sources.values()))


def _format_lineage_section(wiki: WikiCache) -> tuple[str, list[str]]:
    """Render the lineage pages section + collect producing-code paths.

    MVP behavior: include every lineage page with at least one
    ``producing_code`` entry; we don't yet have authoritative table-name
    plumbing per Issue, so we cast a wide net and rely on the LLM to filter.
    Returns ``(rendered_section, list_of_producing_code_paths)``.
    """
    bullets: list[str] = []
    producing_paths: list[str] = []
    for table, page in sorted(wiki.lineages.items()):
        paths = page.producing_code
        if not paths:
            continue
        producing_paths.extend(paths)
        upstream = ", ".join(u.get("table", "?") for u in page.upstream) or "(none)"
        downstream = ", ".join(d.get("table", "?") for d in page.downstream) or "(none)"
        bullets.append(
            f"- table={table}; upstream=[{upstream}]; downstream=[{downstream}]; "
            f"producing_code={paths}"
        )
    if not bullets:
        return "(no lineage pages with producing-code paths)", []
    return "\n".join(bullets), producing_paths


def _format_rules_section(wiki: WikiCache, field_name: str | None) -> str:
    """Render rule pages mentioning ``field_name`` (or all rules for MVP)."""
    if not wiki.rules:
        return "(no rule pages loaded)"

    matched = []
    for rule in wiki.rules:
        if field_name is None:
            matched.append(rule)
            continue
        if any(field_name in f for f in rule.fields):
            matched.append(rule)
            continue
        eq = rule.equation
        text = json.dumps(eq, default=str)
        if field_name and field_name in text:
            matched.append(rule)

    if not matched:
        return "(no rules reference this field)"

    bullets = []
    for rule in matched:
        bullets.append(
            f"- name={rule.name}; tables={rule.tables}; fields={rule.fields}; "
            f"equation={json.dumps(rule.equation, default=str, sort_keys=True)}"
        )
    return "\n".join(bullets)


def _git_log_for_path(
    repo_root: Path,
    path: str,
    n: int = 5,
) -> list[tuple[str, str, str]]:
    """Return up to ``n`` recent (sha, date, subject) tuples for ``path``.

    Uses ``git log --follow --no-merges`` for rename safety. Returns an empty
    list on any git failure (missing repo, missing path, git not installed).
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "log",
                "--follow",
                "--no-merges",
                "-n",
                str(n),
                "--pretty=%H | %ad | %s",
                "--date=short",
                "--",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        logger.debug("rca: git log failed for %s: %s", path, exc)
        return []

    commits: list[tuple[str, str, str]] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) == 3:
            commits.append((parts[0], parts[1], parts[2]))
    return commits


def _stable_str_dedupe(items: list[str]) -> list[str]:
    """Order-preserving dedupe for reference URL lists."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _format_declared_changes(wiki: WikiCache) -> tuple[str, list[str]]:
    """Render producing-code changes DECLARED on lineage pages.

    A lineage page can carry ``repo_url`` plus a ``recent_changes`` list of
    ``{commit, date, message}``. This surfaces them (and builds commit URLs)
    so RCA can point at the actual data-pipeline change without a local
    checkout of that repo. Returns ``(rendered_section, list_of_urls)``.
    """
    bullets: list[str] = []
    urls: list[str] = []
    for table, page in sorted(wiki.lineages.items()):
        changes = page.recent_changes
        if not changes:
            continue
        repo = page.repo_url
        bullets.append(f"- {table} (pipeline repo: {repo or 'n/a'}):")
        for idx, change in enumerate(changes):
            commit = str(change.get("commit", "")).strip()
            date = change.get("date", "")
            message = change.get("message", "")
            url = f"{repo.rstrip('/')}/commit/{commit}" if (repo and commit) else None
            url_part = f" [{url}]" if url else ""
            bullets.append(f"    - {commit[:12]} | {date} | {message}{url_part}")
            # Only the most-recent change per page becomes a clickable
            # reference, so the brief surfaces one likely culprit rather than
            # every historical commit. Older changes stay in the prompt text.
            if url and idx == 0:
                urls.append(url)
    if not bullets:
        return "", []
    header = "Declared recent changes to producing code (from lineage pages):"
    return header + "\n" + "\n".join(bullets), urls


def _format_commits_section(
    repo_root: Path,
    paths: list[str],
) -> tuple[str, list[str]]:
    """Render the recent-commits section + collect commit URLs.

    Returns ``(rendered_section, list_of_commit_urls)``. URLs are only
    included when :func:`commit_url` returns a non-``None`` value.
    """
    if not paths:
        return "(no producing-code paths to walk)", []

    bullets: list[str] = []
    urls: list[str] = []
    seen_shas: set[str] = set()
    for path in paths:
        commits = _git_log_for_path(repo_root, path, n=5)
        if not commits:
            bullets.append(f"- path={path}: (no commit history available)")
            continue
        bullets.append(f"- path={path}:")
        for sha, date, subject in commits:
            url = commit_url(sha, repo_root)
            url_part = f" [{url}]" if url else ""
            bullets.append(f"    - {sha[:12]} | {date} | {subject}{url_part}")
            if url and sha not in seen_shas:
                urls.append(url)
                seen_shas.add(sha)
    return "\n".join(bullets), urls


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_rca_response(
    response: str,
    finding_id: str,
    fallback_references: list[str],
) -> RCAResult:
    """Extract the structured RCA payload from an LLM completion.

    The prompt asks for a fenced ```json block. If parsing succeeds, the
    extracted fields are merged with ``fallback_references`` so commit URLs
    we already collected always end up on the RCAResult. If parsing fails,
    the raw response becomes the ``hypothesis`` and we ship empty evidence /
    confidence=0 — better to return SOMETHING than to crash the run loop.
    """
    body = response.strip()
    payload: dict[str, Any] | None = None

    # Prefer an explicit ```json fence; fall back to any fenced block.
    match = _JSON_FENCE_RE.search(body)
    if match is None:
        match = _ANY_FENCE_RE.search(body)

    candidates: list[str] = []
    if match is not None:
        candidates.append(match.group(1).strip())
    candidates.append(body)  # last-ditch: maybe the whole body is JSON

    for text in candidates:
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            payload = decoded
            break

    if payload is None:
        return RCAResult(
            finding_id=finding_id,
            hypothesis=body,
            evidence=[],
            confidence=0.0,
            references=list(fallback_references),
        )

    hypothesis = str(payload.get("hypothesis", "")).strip() or body
    evidence_raw = payload.get("evidence", [])
    if isinstance(evidence_raw, list):
        evidence = [str(e) for e in evidence_raw]
    else:
        evidence = [str(evidence_raw)]

    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    references_raw = payload.get("references", [])
    if isinstance(references_raw, list):
        references = [str(r) for r in references_raw]
    else:
        references = [str(references_raw)]

    # Merge fallback references (commit URLs) without losing any LLM-supplied ones.
    merged: list[str] = []
    seen: set[str] = set()
    for ref in references + list(fallback_references):
        if ref and ref not in seen:
            seen.add(ref)
            merged.append(ref)

    return RCAResult(
        finding_id=finding_id,
        hypothesis=hypothesis,
        evidence=evidence,
        confidence=confidence,
        references=merged,
    )


# ---------------------------------------------------------------------------
# RCAAgent
# ---------------------------------------------------------------------------


class RCAAgent:
    """Drive per-finding root-cause investigation.

    Parameters
    ----------
    repo_root:
        Repository root used as ``-C`` for every ``git`` invocation and for
        resolving commit URLs.
    client:
        An :class:`LLMClient` (same Protocol as
        :mod:`lens.wiki.ingest`). Defaults to
        :class:`~lens.wiki.ingest.ClaudeCodeClient`. Tests pass a stub.
    output_dir:
        Default directory for :meth:`save`. May be overridden per-call.
    """

    def __init__(
        self,
        repo_root: Path,
        client: LLMClient | None = None,
        output_dir: Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.client = client if client is not None else ClaudeCodeClient()
        self.output_dir = Path(output_dir) if output_dir is not None else None

    def investigate(
        self,
        finding: Finding,
        wiki: WikiCache,
        sources: dict[str, pl.LazyFrame],
        *,
        snapshot_col: str = "snapshot_date",
        entity_col: str = "entity_id",
        group: list[Finding] | None = None,
    ) -> RCAResult:
        """Synthesize an :class:`RCAResult` for one :class:`Finding`.

        When ``group`` is supplied (batch mode, ADR 0003), ``finding`` is the
        group's representative and the prompt carries the full group context —
        member count, entities, severity mix — so the hypothesis explains the
        incident, not just one entity.
        """
        issue = finding.issue

        finding_summary = _summarize_finding(finding)
        if group:
            finding_summary += "\n" + _summarize_group(group, finding)

        lf = _pick_source_lf(sources, issue)
        if lf is not None:
            anomalous_rows, contrast_rows = _sample_rows(
                lf,
                entity_id=issue.entity_id,
                field_name=issue.field_name,
                snapshot_date=issue.snapshot_date,
                snapshot_col=snapshot_col,
                entity_col=entity_col,
                n=5,
            )
        else:
            anomalous_rows, contrast_rows = [], []

        anomalous_block = _format_rows(anomalous_rows, "Anomalous rows:")
        contrast_block = _format_rows(contrast_rows, "Contrast rows:")

        lineage_section, producing_paths = _format_lineage_section(wiki)
        rules_section = _format_rules_section(wiki, issue.field_name)
        commits_section, commit_urls = _format_commits_section(self.repo_root, producing_paths)
        # Lineage pages may also DECLARE recent producing-code changes (repo_url
        # + recent_changes), so RCA can link a real pipeline commit even when
        # that repo isn't checked out locally. Combine both sources.
        declared_section, declared_urls = _format_declared_changes(wiki)
        if declared_section:
            commits_section = f"{commits_section}\n\n{declared_section}"
        commit_urls = _stable_str_dedupe(commit_urls + declared_urls)

        prompt = RCA_PROMPT.format(
            finding_summary=finding_summary,
            anomalous_rows=anomalous_block,
            contrast_rows=contrast_block,
            lineage_section=lineage_section,
            rules_section=rules_section,
            recent_commits=commits_section,
        )

        try:
            response = self.client.complete(prompt)
        except Exception as exc:  # noqa: BLE001 - surface as low-confidence RCA
            logger.exception("rca: LLM call failed: %s", exc)
            return RCAResult(
                finding_id=finding.finding_id,
                hypothesis=f"LLM call failed: {exc}",
                evidence=[],
                confidence=0.0,
                references=list(commit_urls),
            )

        return _parse_rca_response(
            response,
            finding_id=finding.finding_id,
            fallback_references=commit_urls,
        )

    def save(
        self,
        rca: RCAResult,
        run_id: str,
        output_dir: Path | None = None,
    ) -> Path:
        """Atomically write the RCA as ``output_dir/rca/<run_id>/<finding_id>.json``.

        Raises ``ValueError`` if no ``output_dir`` is available (neither passed
        here nor set on the agent).
        """
        dest_root = output_dir if output_dir is not None else self.output_dir
        if dest_root is None:
            raise ValueError(
                "save() needs an output_dir — pass one here or set RCAAgent.output_dir"
            )
        dest_dir = Path(dest_root) / "rca" / run_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / f"{rca.finding_id}.json"

        tmp_path = out_path.with_name(f"{out_path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
        payload = asdict(rca)
        tmp_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp_path, out_path)
        return out_path


# Re-export to keep `from lens.rca.agent import LLMClient` usable in tests.
__all__ = ["LLMClient", "RCAAgent"]
