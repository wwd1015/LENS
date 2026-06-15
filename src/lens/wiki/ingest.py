"""LLM-driven ingestion worker for `lens-wiki/rules/*.md` pages.

Given production SQL and lineage YAML, this module asks an LLM to extract a
structured rule page that conforms to the wiki's schema. Hard safety
boundary: every input path passes through `safety.assert_safe_to_send`
unless the operator opts in to `allow_secrets=True`.

If T2.5's spike gate determined that auto-extraction quality is too low,
`load_hand_authored` provides a thinner alternative that copies and validates
human-authored rule pages instead.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from lens.wiki.prompts import RULE_EXTRACTION_PROMPT
from lens.wiki.safety import assert_safe_to_send

logger = logging.getLogger(__name__)


SCHEMA_EXAMPLE_RELPATH = "rules/senior-debt-equals-pool-x-rate.md"
"""Path under `wiki_root` to the hand-authored exemplar page. The ingestion
worker reads it at runtime so prompt evolution doesn't drift from the actual
shipped schema."""


class LLMClient(Protocol):
    """Minimal LLM interface — `prompt -> completion`. Tests pass stubs."""

    def complete(self, prompt: str) -> str: ...


@dataclass
class CallCost:
    """Per-call cost + token usage from `claude -p --output-format json`.

    ``cost_usd`` is Claude Code's own client-side ESTIMATE (computed from a
    bundled price table), not authoritative billing — which is exactly the
    right granularity for tracking per-run spend. We deliberately read the
    number the CLI already computes rather than maintain our own price table.
    A zero-filled instance is the graceful-degradation value when the CLI did
    not return parseable JSON.
    """

    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_claude_json(stdout: str) -> tuple[str, CallCost]:
    """Parse `claude -p --output-format json` stdout into ``(text, CallCost)``.

    Degrades gracefully: if ``stdout`` is not the expected JSON object (an older
    CLI emitting plain text, an error banner, or a future format change), the
    raw stdout is returned as the completion text with a zero-cost record — the
    caller still gets a usable string and never crashes on cost accounting.
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return stdout, CallCost()
    if not isinstance(payload, dict):
        return stdout, CallCost()

    # `result` carries the model's text; `structured_output` is used instead
    # when the call was made with --json-schema (we don't, but stay robust).
    text = payload.get("result")
    if text is None:
        text = payload.get("structured_output")
    if not isinstance(text, str):
        text = "" if text is None else json.dumps(text)

    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    cost = CallCost(
        cost_usd=_coerce_float(payload.get("total_cost_usd")),
        input_tokens=_coerce_int(usage.get("input_tokens")),
        output_tokens=_coerce_int(usage.get("output_tokens")),
        cache_read_input_tokens=_coerce_int(usage.get("cache_read_input_tokens")),
        cache_creation_input_tokens=_coerce_int(usage.get("cache_creation_input_tokens")),
    )
    return text, cost


class ClaudeCodeClient:
    """Default `LLMClient` implementation backed by Claude Code headless mode.

    LENS runs in environments where the only available authentication is the
    user's Claude Code SSO session — there is no Anthropic API key. To use that
    session, this client shells out to `claude -p "<prompt>" --output-format json`
    as a subprocess and reads stdout. The JSON envelope carries both the model's
    text (``result``) and Claude Code's per-call cost estimate
    (``total_cost_usd`` + ``usage``), so every completion records its own cost
    as a side effect — see :attr:`last_call` / :attr:`total_cost_usd`. Tests use
    a stub and never invoke this class.

    Parameters
    ----------
    executable:
        Path to the `claude` binary. Defaults to whichever `claude` is on PATH.
    timeout_s:
        Subprocess timeout in seconds.
    extra_args:
        Optional list of additional flags passed to `claude` (e.g.
        `--model opus`). The prompt and `--output-format json` are always
        appended.
    """

    def __init__(
        self,
        executable: str = "claude",
        timeout_s: int = 180,
        extra_args: list[str] | None = None,
    ) -> None:
        self.executable = executable
        self.timeout_s = timeout_s
        self.extra_args = list(extra_args or [])
        # Cost accounting side-channel: the LLMClient Protocol stays
        # `complete(prompt) -> str`, but a real Claude Code call also records
        # what it cost here. Callers that care (batch RCA) read `last_call`
        # right after `complete`; stubs simply don't set these attributes.
        self.calls: list[CallCost] = []
        self.last_call: CallCost | None = None

    @property
    def total_cost_usd(self) -> float:
        """Sum of every recorded call's estimated cost this client made."""
        return sum(c.cost_usd for c in self.calls)

    def complete(self, prompt: str) -> str:
        cmd = [self.executable, "-p", prompt, "--output-format", "json"]
        if self.extra_args:
            cmd.extend(self.extra_args)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=True,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"`{self.executable}` not on PATH; install Claude Code or set "
                f"`executable=` on ClaudeCodeClient"
            ) from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"claude headless run failed (rc={e.returncode}): {e.stderr.strip()}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude headless run timed out after {self.timeout_s}s") from e
        text, cost = _parse_claude_json(result.stdout)
        self.calls.append(cost)
        self.last_call = cost
        return text


def _slugify(name: str) -> str:
    """Lowercase, hyphenated, alphanumeric-only slug. Strips edge hyphens."""
    lowered = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "rule"


def _extract_markdown(response: str) -> str:
    """Pull the first fenced block out of an LLM response, or pass through.

    Many LLMs wrap markdown output in ```markdown ... ``` fences. If a fence
    exists, take its contents; otherwise treat the whole response as the page.
    """
    fence = re.search(r"```(?:[a-zA-Z]+)?\n(.*?)```", response, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return response.strip()


def _parse_frontmatter(md: str) -> dict:
    """Parse the YAML frontmatter dict out of a markdown page.

    Returns an empty dict if there's no frontmatter or it's malformed; callers
    inspect the returned dict for the fields they require.
    """
    if not md.startswith("---"):
        return {}
    parts = md.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


class _TransientIngestError(Exception):
    """Internal — raised when the response is parseable as text but the
    extracted page is missing required fields. Triggers a retry."""


class IngestionWorker:
    """Drives the LLM-based rule-page extraction pipeline.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root. Every input path is validated
        against this root by `safety.assert_safe_to_send`.
    wiki_root:
        Absolute path to the `lens-wiki/` directory. Pages are written under
        `wiki_root/rules/`.
    client:
        An `LLMClient`. Defaults to `ClaudeCodeClient()` (shells out to the
        `claude` CLI in headless mode, using the user's existing Claude Code
        SSO session — no API key required). Tests pass a stub.
    max_retries:
        How many times to retry the LLM on transient failure (exception or
        unparseable response). Retries with a small linear back-off; tests
        can monkey-patch `time.sleep` to zero.
    """

    def __init__(
        self,
        repo_root: Path,
        wiki_root: Path,
        client: LLMClient | None = None,
        max_retries: int = 3,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.wiki_root = Path(wiki_root)
        self.client = client if client is not None else ClaudeCodeClient()
        self.max_retries = max_retries

    def _read_schema_example(self) -> str:
        schema_path = self.wiki_root / SCHEMA_EXAMPLE_RELPATH
        if not schema_path.is_file():
            raise FileNotFoundError(
                f"schema example missing at {schema_path}; ingestion cannot "
                f"build a rule-extraction prompt without it"
            )
        return schema_path.read_text()

    def _build_prompt(
        self,
        sql: str,
        lineage: str,
        schema_example: str,
    ) -> str:
        return RULE_EXTRACTION_PROMPT.format(
            sql=sql,
            lineage=lineage,
            schema_example=schema_example,
        )

    def _call_with_retries(self, prompt: str) -> tuple[str, dict, str]:
        """Call the LLM, retrying on exception or missing-required-field.

        Returns `(page_markdown, frontmatter_dict, raw_response)` on success.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                raw = self.client.complete(prompt)
                page = _extract_markdown(raw)
                fm = _parse_frontmatter(page)
                eq = fm.get("equation") or {}
                lhs = eq.get("lhs") or {}
                if not lhs.get("table"):
                    raise _TransientIngestError("response missing equation.lhs.table")
                return page, fm, raw
            except Exception as e:  # noqa: BLE001 — retry on anything LLM-side
                last_exc = e
                logger.warning(
                    "ingestion attempt %d/%d failed: %s",
                    attempt,
                    self.max_retries,
                    e,
                )
                if attempt < self.max_retries:
                    # Small linear back-off; tests monkeypatch time.sleep.
                    time.sleep(0.1 * attempt)
        assert last_exc is not None
        raise last_exc

    def ingest(
        self,
        dataset_name: str,
        code_paths: list[Path],
        lineage_yaml: Path | None = None,
        *,
        allow_secrets: bool = False,
    ) -> list[Path]:
        """Extract a rule page for `dataset_name` and write it under the wiki.

        Parameters
        ----------
        dataset_name:
            Logical dataset label, used for logging only. The on-disk filename
            comes from the extracted frontmatter `name`.
        code_paths:
            One or more production code files (typically SQL) describing the
            transformation. Concatenated in order into the prompt.
        lineage_yaml:
            Optional `LINEAGE.yaml` fragment for the dataset.
        allow_secrets:
            If False (default), every input path passes through
            `assert_safe_to_send` first; an unsafe path aborts the whole call.
            If True, the safety gate is skipped — callers must be confident
            the input is non-sensitive.

        Returns
        -------
        list[Path]
            On-disk paths of every rule page written (currently always a
            single-element list; reserved for future multi-rule responses).
        """
        # Pre-flight safety: every path that will be sent to the LLM.
        if not allow_secrets:
            for p in code_paths:
                assert_safe_to_send(p, self.repo_root)
            if lineage_yaml is not None:
                assert_safe_to_send(lineage_yaml, self.repo_root)

        schema_example = self._read_schema_example()
        sql = "\n\n".join(Path(p).read_text() for p in code_paths)
        lineage = Path(lineage_yaml).read_text() if lineage_yaml is not None else ""
        prompt = self._build_prompt(sql, lineage, schema_example)

        logger.info(
            "ingesting dataset=%s code_paths=%d lineage=%s",
            dataset_name,
            len(code_paths),
            lineage_yaml,
        )
        page, fm, _raw = self._call_with_retries(prompt)

        name = fm.get("name") or dataset_name
        slug = _slugify(str(name))
        rules_dir = self.wiki_root / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        out_path = rules_dir / f"{slug}.md"
        out_path.write_text(page)
        logger.info("wrote %s", out_path)
        return [out_path]


def load_hand_authored(rules_dir: Path, wiki_root: Path) -> list[Path]:
    """Copy hand-authored rule pages into the wiki, validating each.

    The T2.5 fallback mode: instead of asking an LLM to extract rules, an
    analyst hand-authors pages in `rules_dir`. This function validates each
    page (frontmatter must contain `name` and `equation.lhs.table`) and copies
    it under `wiki_root/rules/`.

    Parameters
    ----------
    rules_dir:
        Directory containing the hand-authored `*.md` pages.
    wiki_root:
        Wiki destination. Pages land in `wiki_root/rules/`.

    Returns
    -------
    list[Path]
        Destination paths of every copied page.

    Raises
    ------
    ValueError
        If any page lacks the required frontmatter fields.
    """
    src = Path(rules_dir)
    if not src.is_dir():
        raise FileNotFoundError(f"hand-authored rules dir not found: {src}")
    dst_dir = Path(wiki_root) / "rules"
    dst_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for page_path in sorted(src.glob("*.md")):
        text = page_path.read_text()
        fm = _parse_frontmatter(text)
        if not fm.get("name"):
            raise ValueError(f"{page_path}: missing required frontmatter field 'name'")
        eq = fm.get("equation") or {}
        lhs = eq.get("lhs") or {}
        if not lhs.get("table"):
            raise ValueError(
                f"{page_path}: missing required frontmatter field 'equation.lhs.table'"
            )
        dst = dst_dir / page_path.name
        shutil.copyfile(page_path, dst)
        written.append(dst)
    return written
