"""Detection orchestrator — the central run loop for LENS Surveillance v2.

Composes the existing :class:`lens.engine.Suite` for single-source detectors,
plus a separate list of cross-source detectors (which take a ``dict`` of
``LazyFrame`` plus a :class:`lens.wiki.cache.WikiCache`). On each ``.run(...)``
the orchestrator:

  1. Materializes every input source as a ``pl.LazyFrame``.
  2. Runs each single-source detector against each named source via ``Suite``.
  3. Runs each cross-source detector once over the full source dict.
  4. Scores each emitted :class:`lens.types.Issue` via
     :func:`lens.scoring.score_to_severity` and computes the dedup ``finding_id``.
  5. Dedups issues sharing ``(entity_id, field_name, snapshot_date)`` into one
     :class:`lens.types.Finding` whose ``detector_sources`` lists every detector
     that flagged the point.
  6. Writes ``findings.{run_id}.json`` into ``output_dir`` and atomically
     repoints ``findings.latest.json`` at it via ``os.replace`` on a temp
     symlink — safe under concurrent runs writing to the same directory.

Detector failures (a check whose ``.run()`` / ``.run_cross()`` raises) are
logged and skipped; the rest of the run continues.

Note on cross-check signatures: only the ``CrossSourceWikiCheck``-style
signature is supported — ``run_cross(sources: dict[str, LazyFrame], *,
wiki=..., entity_col=..., snapshot_col=...)``. The ``CrossSourceMatchCheck``
two-positional-frame shape is intentionally not wired in here; cross-source
matching belongs in a wiki rule, not in a parallel API surface. A clear error
is raised if a cross check exposes the legacy positional signature.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

# Side-effect imports: registering the built-in checks so callers can refer to
# them by string name (e.g. `add_single("null_check", ...)`). Without these
# the registry would be empty until somebody imports each check module
# manually.
from lens.checks import (  # noqa: F401  (import-for-side-effects)
    crosssource,
    crosssource_wiki,
    snapshot,
    temporal,
)
from lens.checks.base import BaseCheck
from lens.checks.registry import registry
from lens.engine import Suite
from lens.io.base import DataSource
from lens.scoring import score_to_severity
from lens.types import Finding, Issue, Severity, compute_finding_id
from lens.wiki.cache import WikiCache

# Optional check modules — only import if their deps are installed. Wrapped in
# try/except so the orchestrator works in a minimal install.
try:  # pragma: no cover - dep-conditional
    from lens.checks import temporal_stl  # noqa: F401
except Exception:  # noqa: BLE001
    pass

try:  # pragma: no cover - dep-conditional
    from lens.checks import tabpfn_anomaly  # noqa: F401
except Exception:  # noqa: BLE001
    pass

logger = logging.getLogger(__name__)


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.ERROR: 2,
    Severity.CRITICAL: 3,
}


def _empty_wiki_cache() -> WikiCache:
    """Build an empty :class:`WikiCache` (no datasets, no rules, no lineages).

    Used when the caller does not supply a ``wiki_root``; cross-source detectors
    that iterate ``wiki.all_rules()`` will simply yield zero findings.
    """
    return WikiCache()


def _materialize_source(value: DataSource | pl.LazyFrame | pl.DataFrame) -> pl.LazyFrame:
    """Coerce any accepted source representation into a ``pl.LazyFrame``."""
    if isinstance(value, DataSource):
        return value.read()
    if isinstance(value, pl.DataFrame):
        return value.lazy()
    if isinstance(value, pl.LazyFrame):
        return value
    raise TypeError(
        f"Unsupported source type: {type(value).__name__}; "
        "expected DataSource, pl.LazyFrame, or pl.DataFrame"
    )


def _raw_score(issue: Issue) -> float:
    """Pull a detector-native raw score out of ``issue.details``.

    Tries ``score`` → ``z_score`` → ``diff`` in that order; missing keys yield
    ``0.0`` (which maps to ``Severity.INFO`` under every default threshold
    table).
    """
    details = issue.details or {}
    for key in ("score", "z_score", "diff"):
        if key in details:
            try:
                return float(details[key])
            except (TypeError, ValueError):
                continue
    return 0.0


def _detector_key(issue: Issue) -> str:
    """Pick the detector identifier passed to :func:`score_to_severity`.

    Prefers ``issue.detector_source`` (e.g. ``"cross_source_wiki:rule_a"``)
    because it carries rule-slug context; falls back to ``check_name``.
    """
    return issue.detector_source or issue.check_name


def _rescore_issue(issue: Issue) -> Issue:
    """Return a copy of ``issue`` with severity/confidence/finding_id populated."""
    raw = _raw_score(issue)
    detector = _detector_key(issue)
    severity, confidence = score_to_severity(raw, detector)
    finding_id = compute_finding_id(issue.entity_id, issue.field_name, issue.snapshot_date)
    return dataclasses.replace(
        issue,
        severity=severity,
        confidence=confidence,
        finding_id=finding_id,
    )


def _better_issue(left: Issue, right: Issue) -> Issue:
    """Pick the more "representative" issue for a dedup group.

    Highest severity wins; ties broken by highest confidence; final ties keep
    the first-seen (``left``).
    """
    l_sev = _SEVERITY_ORDER.get(left.severity, -1)
    r_sev = _SEVERITY_ORDER.get(right.severity, -1)
    if r_sev > l_sev:
        return right
    if r_sev < l_sev:
        return left
    if right.confidence > left.confidence:
        return right
    return left


def _stable_dedupe(items: list[str]) -> list[str]:
    """Order-preserving dedupe."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _atomic_symlink(target_name: str, link_path: Path) -> None:
    """Atomically point ``link_path`` at ``target_name`` (relative to its parent).

    Writes a temp symlink in the same directory then ``os.replace``-s it on top
    of ``link_path``. POSIX guarantees the rename is atomic when source and
    destination are on the same filesystem.
    """
    link_dir = link_path.parent
    # Use a unique tmp suffix per call so concurrent orchestrator runs don't
    # clobber each other's in-flight symlinks.
    tmp_name = f"{link_path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    tmp_path = link_dir / tmp_name
    # If a stale tmp exists for some reason, drop it first; symlink() refuses
    # to overwrite.
    try:
        tmp_path.unlink()
    except FileNotFoundError:
        pass
    os.symlink(target_name, tmp_path)
    os.replace(tmp_path, link_path)


class DetectionOrchestrator:
    """Central run loop. Owns a :class:`Suite` plus a list of cross-source checks.

    Example::

        orch = (
            DetectionOrchestrator(entity_col="loan_id", snapshot_col="as_of_date")
            .add_single("null_check", fields=["balance"])
            .add_cross("cross_source_wiki")
        )
        findings = orch.run(
            sources={"loan_pool": lf_a, "senior_debt": lf_b},
            wiki_root=Path("lens-wiki/"),
            output_dir=Path("./out"),
        )
    """

    def __init__(
        self,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> None:
        self.entity_col = entity_col
        self.snapshot_col = snapshot_col
        self._suite = Suite(entity_col=entity_col, snapshot_col=snapshot_col)
        self._cross_checks: list[BaseCheck] = []

    # ------------------------------------------------------------------
    # Builder API
    # ------------------------------------------------------------------

    def add_single(self, check: str | BaseCheck, **kwargs: Any) -> DetectionOrchestrator:
        """Add a single-source check (delegates to the internal ``Suite``)."""
        self._suite.add(check, **kwargs)
        return self

    def add_cross(self, check: str | BaseCheck, **kwargs: Any) -> DetectionOrchestrator:
        """Add a cross-source check.

        Strings are instantiated via the global registry; passed instances are
        used directly. The check must expose a ``run_cross(sources, *, wiki,
        entity_col, snapshot_col)`` method (the ``CrossSourceWikiCheck``
        signature).
        """
        if isinstance(check, str):
            instance = registry.create(check, **kwargs)
        else:
            instance = check
        self._cross_checks.append(instance)
        return self

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(
        self,
        sources: dict[str, DataSource | pl.LazyFrame | pl.DataFrame],
        wiki_root: Path | str | None = None,
        output_dir: Path | str | None = None,
        run_id: str | None = None,
    ) -> list[Finding]:
        """Execute every configured detector and persist a normalized findings file.

        Args:
            sources: Mapping of source name → DataSource / LazyFrame / DataFrame.
            wiki_root: Root of the ``lens-wiki/`` tree. If ``None``, cross-source
                detectors run against an empty :class:`WikiCache`.
            output_dir: Directory to write ``findings.{run_id}.json`` and
                ``findings.latest.json`` into. Created if it doesn't exist.
            run_id: Optional explicit run id. If ``None``, a unique id is
                generated as ``YYYYMMDDTHHMMSS-<8 hex chars>``.

        Returns:
            The deduplicated ``list[Finding]`` (also written to disk).
        """
        if output_dir is None:
            raise ValueError("output_dir is required")
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if run_id is None:
            run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"

        # Build the wiki cache (empty if no root supplied).
        if wiki_root is None:
            wiki_cache = _empty_wiki_cache()
        else:
            wiki_cache = WikiCache.from_dir(Path(wiki_root))

        # Materialize sources up front; once materialized, we hand the same
        # LazyFrame reference to both single- and cross-source runs.
        lazy_sources: dict[str, pl.LazyFrame] = {}
        for name, src in sources.items():
            try:
                lazy_sources[name] = _materialize_source(src)
            except Exception as exc:  # noqa: BLE001 - log + continue per spec
                logger.exception(
                    "orchestrator: failed to materialize source %r: %s", name, exc
                )

        all_issues: list[Issue] = []

        # --- single-source detectors ------------------------------------
        # Run each registered single-source check against each source. We
        # iterate per-check rather than calling Suite.run once-per-source
        # because we need fine-grained failure isolation: a single bad check
        # must not knock out the rest of the run.
        for check in self._suite._checks:  # noqa: SLF001 - intentional composition
            for source_name, lf in lazy_sources.items():
                try:
                    result = check.run(
                        lf,
                        entity_col=self.entity_col,
                        snapshot_col=self.snapshot_col,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "orchestrator: check %r failed on source %r: %s",
                        check.name,
                        source_name,
                        exc,
                    )
                    continue
                for issue in result.issues:
                    # Stamp the source name into details (rebuild — Issue is frozen).
                    new_details = dict(issue.details or {})
                    new_details.setdefault("__source__", source_name)
                    all_issues.append(dataclasses.replace(issue, details=new_details))

        # --- cross-source detectors ------------------------------------
        for check in self._cross_checks:
            run_cross = getattr(check, "run_cross", None)
            if run_cross is None:
                logger.error(
                    "orchestrator: cross check %r has no run_cross() method; skipping",
                    getattr(check, "name", type(check).__name__),
                )
                continue

            # Defensive signature check — we only support the
            # CrossSourceWikiCheck-style signature (sources dict + wiki kwarg).
            try:
                sig = inspect.signature(run_cross)
                params = sig.parameters
            except (TypeError, ValueError):
                params = {}  # type: ignore[assignment]

            param_names = list(params)
            # The wiki-style API: first positional is the dict, and `wiki` is a
            # keyword param. The legacy positional API takes two LazyFrames.
            has_wiki_kwarg = "wiki" in param_names
            looks_like_two_frame = (
                len(param_names) >= 2
                and param_names[0] in ("source_a",)
                and param_names[1] in ("source_b",)
            )

            if looks_like_two_frame and not has_wiki_kwarg:
                logger.error(
                    "orchestrator: cross check %r exposes the legacy two-frame "
                    "run_cross(source_a, source_b) signature, which the "
                    "orchestrator does not support. Wrap the comparison in a "
                    "wiki rule (cross_source_wiki) instead.",
                    getattr(check, "name", type(check).__name__),
                )
                continue

            try:
                result = run_cross(
                    lazy_sources,
                    wiki=wiki_cache,
                    entity_col=self.entity_col,
                    snapshot_col=self.snapshot_col,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "orchestrator: cross check %r failed: %s",
                    getattr(check, "name", type(check).__name__),
                    exc,
                )
                continue
            all_issues.extend(result.issues)

        # --- score + dedup ---------------------------------------------
        scored = [_rescore_issue(issue) for issue in all_issues]

        groups: dict[str, list[Issue]] = {}
        for issue in scored:
            groups.setdefault(issue.finding_id, []).append(issue)

        detected_at = datetime.now(UTC)
        findings: list[Finding] = []
        for finding_id, group in groups.items():
            # Pick the representative issue (highest severity / confidence).
            rep = group[0]
            for other in group[1:]:
                rep = _better_issue(rep, other)
            detector_sources = _stable_dedupe(
                [i.detector_source or i.check_name for i in group]
            )
            findings.append(
                Finding(
                    issue=rep,
                    detector_sources=detector_sources,
                    detected_at=detected_at,
                    run_id=run_id,
                )
            )

        # Stable output order: by finding_id (deterministic, doesn't leak
        # iteration order from the dict).
        findings.sort(key=lambda f: f.finding_id)

        # --- write findings file + atomic symlink ----------------------
        run_file = output_dir / f"findings.{run_id}.json"
        with run_file.open("w", encoding="utf-8") as fh:
            json.dump(
                [_finding_to_jsonable(f) for f in findings],
                fh,
                default=str,
                indent=2,
            )

        latest_link = output_dir / "findings.latest.json"
        _atomic_symlink(run_file.name, latest_link)

        return findings


def _finding_to_jsonable(f: Finding) -> dict[str, Any]:
    """Serialize a :class:`Finding` into a JSON-safe dict."""
    issue = f.issue
    return {
        "finding_id": f.finding_id,
        "issue": {
            "check_name": issue.check_name,
            "severity": issue.severity.value,
            "entity_id": issue.entity_id,
            "field_name": issue.field_name,
            "snapshot_date": (
                issue.snapshot_date.isoformat()
                if hasattr(issue.snapshot_date, "isoformat") and issue.snapshot_date is not None
                else issue.snapshot_date
            ),
            "description": issue.description,
            "details": issue.details,
            "confidence": issue.confidence,
            "detector_source": issue.detector_source,
            "finding_id": issue.finding_id,
        },
        "detector_sources": list(f.detector_sources),
        "detected_at": f.detected_at.isoformat() if f.detected_at is not None else None,
        "run_id": f.run_id,
    }
