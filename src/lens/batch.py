"""The scheduled batch run: detect → group RCA → brief.

``run_batch`` is what ``lens run <config.yaml>`` executes — the "morning
brief" path. It composes the pieces that already exist as libraries:

1. :class:`lens.orchestrator.DetectionOrchestrator` over the configured
   sources (with feedback suppression applied, ADR 0001).
2. One RCA per Finding Group at/above the configured severity floor
   (ADR 0003) — never one per finding, so a fan-out incident costs one LLM
   call, not hundreds. Suppressed findings never trigger RCA.
3. :func:`lens.brief.html.render_brief` → ``brief.{run_id}.html`` plus an
   atomically repointed ``brief.latest.html``, and the markdown digest
   returned for stdout / Slack.

Per-group RCA failures are logged and skipped; the brief still renders.

Token / cost controls (all configurable under ``rca:``):

* **reuse_prior_rca** — an ongoing finding (same finding_id as last run, i.e.
  same immutable snapshot) reuses last run's hypothesis with no LLM call. The
  biggest steady-state saver for a daily cron.
* **Tiered model routing** — the bulk runs on ``model`` (default the ``sonnet``
  tier, balancing RCA quality against cost); findings at/above
  ``escalate_severity`` go to the stronger ``escalate_model`` (default ``opus``).
* **max_investigations** — caps NEW investigations per run (reused are free).
* **sample_rows / max_commits** — bound the per-prompt context size.

Note: cross-call prompt caching is NOT available — each ``claude -p`` headless
invocation is an independent subprocess with no shared cache, and ``-p`` has no
session resume (see ADR 0002). The levers above are what control token spend;
real prefix caching would require the Anthropic SDK, which ADR 0002 rejects.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from lens.brief.html import render_brief
from lens.brief.markdown import _load_rcas, render_brief_summary
from lens.feedback_loop import is_suppressed
from lens.orchestrator import DetectionOrchestrator, _atomic_symlink, _materialize_source
from lens.rca.agent import RCAAgent
from lens.run_config import RunConfig
from lens.types import SEVERITY_ORDER, Finding, RCAResult, Severity, finding_group_key
from lens.wiki.cache import WikiCache
from lens.wiki.ingest import ClaudeCodeClient, LLMClient

logger = logging.getLogger(__name__)


@dataclass
class BatchResult:
    """Everything one batch run produced."""

    run_id: str
    findings: list[Finding]
    rcas: dict[str, RCAResult] = field(default_factory=dict)
    findings_path: Path | None = None
    brief_html_path: Path | None = None
    markdown_digest: str = ""
    rca_groups_investigated: int = 0
    rca_groups_reused: int = 0
    rca_groups_skipped_below_floor: int = 0
    rca_groups_skipped_over_cap: int = 0
    # LLM spend for THIS run — fresh investigations only. Reused groups cost
    # $0 of new spend (that's the point of reuse_prior_rca), though each reused
    # card still shows its original per-finding cost. Estimated by Claude Code.
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # False when at least one fresh investigation reported no cost estimate
    # (non-Claude client, or the CLI envelope didn't parse) — the brief then
    # shows "n/a" instead of a misleading "$0.00". Zero fresh investigations
    # is genuinely $0 of new spend, so the flag stays True.
    cost_known: bool = True

    @property
    def cost_summary(self) -> dict[str, float | int | bool]:
        """Compact run-cost dict for the brief / digest / CLI summary."""
        return {
            "total_cost_usd": self.total_cost_usd,
            "cost_known": self.cost_known,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "investigated": self.rca_groups_investigated,
            "reused": self.rca_groups_reused,
        }


def _build_orchestrator(cfg: RunConfig) -> DetectionOrchestrator:
    orch = DetectionOrchestrator(entity_col=cfg.entity_col, snapshot_col=cfg.snapshot_col)
    for spec in cfg.checks:
        orch.add_single(spec["name"], sources=spec.get("sources"), **spec["params"])
    for spec in cfg.cross_checks:
        orch.add_cross(spec["name"], **spec["params"])
    return orch


def _rca_model_for(cfg: RunConfig, severity: Severity) -> str | None:
    """Pick the model for a group of this severity (tiered routing).

    Findings at/above ``escalate_severity`` go to the stronger
    ``escalate_model``; everything else uses the cheap bulk ``model``.
    ``None`` means Claude Code's session default.
    """
    if cfg.rca.escalate_model and SEVERITY_ORDER.get(severity, -1) >= SEVERITY_ORDER.get(
        cfg.rca.escalate_severity, 99
    ):
        return cfg.rca.escalate_model
    return cfg.rca.model


def _client_for_model(
    model: str | None,
    llm_client: LLMClient | None,
    cache: dict[str | None, LLMClient | None],
) -> LLMClient | None:
    """Resolve (and cache) the LLM client for a model string.

    An injected client (tests) always wins, regardless of model. Otherwise a
    :class:`ClaudeCodeClient` pinned to ``model`` (or ``None`` → session
    default). Cached so a run builds at most one client per model.
    """
    if llm_client is not None:
        return llm_client
    if model not in cache:
        cache[model] = ClaudeCodeClient(extra_args=["--model", model]) if model else None
    return cache[model]


def _prior_run_id(prior_findings: Path | None) -> str | None:
    """Extract the prior run id from a ``findings.<run_id>.json`` path."""
    if prior_findings is None:
        return None
    name = prior_findings.name
    if name.startswith("findings.") and name.endswith(".json"):
        return name[len("findings.") : -len(".json")]
    return None


def _group_for_rca(findings: list[Finding]) -> dict[tuple[str, str], list[Finding]]:
    """Bucket non-suppressed findings into Finding Groups (ADR 0003)."""
    groups: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        if is_suppressed(f):
            continue
        groups.setdefault(finding_group_key(f), []).append(f)
    return groups


def _group_representative(members: list[Finding]) -> Finding:
    """Highest severity, then highest confidence, then stable finding_id."""
    return max(
        members,
        key=lambda f: (
            SEVERITY_ORDER.get(f.issue.severity, -1),
            float(f.issue.confidence or 0.0),
            f.finding_id,
        ),
    )


def _prior_findings_target(output_dir: Path) -> Path | None:
    """Resolve the pre-run ``findings.latest.json`` target for the brief delta.

    Must be captured BEFORE the orchestrator repoints the symlink at the new
    run's file, otherwise the delta compares the run against itself.
    """
    latest = output_dir / "findings.latest.json"
    try:
        return latest.resolve(strict=True) if latest.exists() else None
    except OSError:
        return None


def run_batch(
    cfg: RunConfig,
    *,
    run_id: str | None = None,
    llm_client: LLMClient | None = None,
) -> BatchResult:
    """Execute one full batch run. See module docstring for the shape."""
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    prior_findings = _prior_findings_target(cfg.output_dir)

    # Materialize sources for RCA row sampling. Best-effort: a source that
    # cannot be read is logged and skipped here — the orchestrator (which
    # materializes independently from cfg.sources) turns the same failure
    # into a CRITICAL source_unavailable finding, so it is never silent.
    lazy_sources: dict[str, pl.LazyFrame] = {}
    for name, src in cfg.sources.items():
        try:
            lazy_sources[name] = _materialize_source(src)
        except Exception as exc:  # noqa: BLE001 - orchestrator flags it
            logger.exception(
                "batch: failed to materialize source %r for RCA sampling: %s",
                name,
                exc,
            )

    orch = _build_orchestrator(cfg)
    findings = orch.run(
        sources=dict(cfg.sources),
        wiki_root=cfg.wiki_root,
        output_dir=cfg.output_dir,
        run_id=run_id,
        feedback_path=cfg.feedback.path,
        feedback_expiry_days=cfg.feedback.expiry_days,
    )
    actual_run_id = findings[0].run_id if findings else (run_id or "")
    if not actual_run_id:
        # Empty run with autogenerated id: recover it from the latest symlink.
        latest = cfg.output_dir / "findings.latest.json"
        try:
            target = latest.resolve(strict=True).name
            actual_run_id = target.removeprefix("findings.").removesuffix(".json")
        except OSError:
            actual_run_id = "unknown"

    result = BatchResult(
        run_id=actual_run_id,
        findings=findings,
        findings_path=cfg.output_dir / f"findings.{actual_run_id}.json",
    )

    # --- batch RCA: one investigation per Finding Group (ADR 0003) -------
    if cfg.rca.enabled and findings:
        wiki = WikiCache.from_dir(cfg.wiki_root) if cfg.wiki_root else WikiCache()
        # One RCAAgent per model (tiered routing); clients are cached so a run
        # builds at most one subprocess client per model.
        client_cache: dict[str | None, LLMClient | None] = {}
        agent_cache: dict[str | None, RCAAgent] = {}

        def _agent_for(model: str | None) -> RCAAgent:
            if model not in agent_cache:
                agent_cache[model] = RCAAgent(
                    repo_root=cfg.rca.repo_root,
                    client=_client_for_model(model, llm_client, client_cache),
                    output_dir=cfg.output_dir,
                )
            return agent_cache[model]

        floor = SEVERITY_ORDER[cfg.rca.severity_floor]

        # Prior run's hypotheses, for reuse (ongoing findings keep last run's
        # RCA — same finding_id = same immutable snapshot, so it still holds).
        prior_rcas: dict[str, RCAResult] = {}
        if cfg.rca.reuse_prior_rca:
            prior_id = _prior_run_id(prior_findings)
            if prior_id:
                prior_rcas = _load_rcas(cfg.output_dir / "rca" / prior_id)

        # Collect groups above the floor, sorted most-important first.
        eligible: list[tuple[tuple[str, str], list[Finding], Finding]] = []
        for key, members in _group_for_rca(findings).items():
            rep = _group_representative(members)
            if SEVERITY_ORDER.get(rep.issue.severity, -1) < floor:
                result.rca_groups_skipped_below_floor += 1
                continue
            eligible.append((key, members, rep))
        eligible.sort(
            key=lambda t: (
                -SEVERITY_ORDER.get(t[2].issue.severity, -1),
                -float(t[2].issue.confidence or 0.0),
                t[0],
            )
        )

        # Reuse first (free), so the investigation cap only bounds NEW calls.
        # Match on ANY member's finding_id, not just the representative's —
        # the rep churns when a new entity joins the group with higher
        # severity, and that must not force a re-investigation of the same
        # ongoing incident.
        fresh: list[tuple[tuple[str, str], list[Finding], Finding]] = []
        for key, members, rep in eligible:
            reused: RCAResult | None = None
            for member in members:
                prior = prior_rcas.get(member.finding_id)
                if prior is not None:
                    reused = dataclasses.replace(prior, reused_from=prior.finding_id)
                    break
            if reused is not None:
                _agent_for(cfg.rca.model).save(reused, actual_run_id)
                for member in members:
                    result.rcas[member.finding_id] = reused
                result.rca_groups_reused += 1
            else:
                fresh.append((key, members, rep))

        cap = cfg.rca.max_investigations
        if cap is not None and len(fresh) > cap:
            result.rca_groups_skipped_over_cap = len(fresh) - cap
            fresh = fresh[:cap]

        for key, members, rep in fresh:
            model_used = _rca_model_for(cfg, rep.issue.severity)
            agent = _agent_for(model_used)
            try:
                rca = agent.investigate(
                    rep,
                    wiki,
                    lazy_sources,
                    snapshot_col=cfg.snapshot_col,
                    entity_col=cfg.entity_col,
                    group=members,
                    sample_rows=cfg.rca.sample_rows,
                    max_commits=cfg.rca.max_commits,
                )
            except Exception as exc:  # noqa: BLE001 - one bad group must not kill the brief
                logger.exception("batch: RCA failed for group %s: %s", key, exc)
                continue
            rca.model = model_used
            result.rca_groups_investigated += 1
            # Accumulate this run's NEW spend (reused groups are not counted —
            # they cost nothing this run) BEFORE persistence: the LLM call
            # already happened, so a failed save must not erase the spend.
            # A None cost means the client reported no estimate — that makes
            # the run total unknown, not $0.
            if rca.cost_usd is None:
                result.cost_known = False
            else:
                result.total_cost_usd += rca.cost_usd
            result.total_input_tokens += rca.input_tokens or 0
            result.total_output_tokens += rca.output_tokens or 0
            # The group shares one hypothesis — attach it to every member so
            # each brief card can render it.
            for member in members:
                result.rcas[member.finding_id] = rca
            try:
                agent.save(rca, actual_run_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "batch: failed to persist RCA for group %s: %s", key, exc
                )

    # --- brief ------------------------------------------------------------
    brief_path = cfg.output_dir / f"brief.{actual_run_id}.html"
    render_brief(
        findings,
        result.rcas,
        brief_path,
        prior_findings_path=prior_findings,
        dataset_label=cfg.brief.dataset_label,
        cost_summary=result.cost_summary,
    )
    _atomic_symlink(brief_path.name, cfg.output_dir / "brief.latest.html")
    result.brief_html_path = brief_path

    result.markdown_digest = render_brief_summary(
        findings,
        result.rcas,
        top_n=cfg.brief.top_n,
        dataset_label=cfg.brief.dataset_label,
        cost_summary=result.cost_summary,
    )
    return result
