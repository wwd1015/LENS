"""Secret-file allowlist for the wiki ingestion worker.

The spec (§6) promises "LLM calls only on repo-resident code (no secret
exfiltration)." The original plan blindly read paths — this module enforces
the boundary. Every path the ingestion worker would send to an LLM passes
through `is_safe_to_send` (or `assert_safe_to_send`) first.

A path is unsafe if:
  - It resolves outside `repo_root`.
  - Its (case-insensitive) path string matches one of the secret patterns
    (`.env*`, `credentials*`, `secrets*`, `*.pem`, `*.key`).
  - Its content matches a credentials regex (AWS access key, generic
    `api_key=...`, `password=...`, `secret=...`).

Rejection is logged at WARNING level with a reason so the operator can see
why a file was skipped.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


class UnsafePathError(ValueError):
    """Raised by `assert_safe_to_send` when a path fails the secret allowlist.

    Subclasses ValueError so callers that don't care about the distinction can
    still catch broad exceptions.
    """


_SECRET_PATH_PATTERNS: list[str] = [
    "**/.env*",
    "**/credentials*",
    "**/secrets*",
    "**/*.pem",
    "**/*.key",
]
"""Glob-style patterns for path names that should never be sent to an LLM."""


_SECRET_CONTENT_REGEXES: list[re.Pattern[str]] = [
    # AWS access key id — `AKIA` + 16 uppercase alphanumerics.
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Generic credential-looking assignment: `api_key = "..."`, `password: ...`,
    # `secret = '...'`. Case-insensitive; value must be ≥16 chars of base64-ish.
    re.compile(
        r"(?i)(api[_-]?key|password|secret)[\s:=]+['\"]?[A-Za-z0-9_/+=-]{16,}"
    ),
    # OpenAI keys: `sk-` followed by ≥20 base64-ish chars.
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    # GitHub personal-access / fine-grained tokens: `ghp_`, `gho_`, `ghu_`,
    # `ghs_`, `ghr_` prefixes with ≥30 base64-ish chars.
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{30,}\b"),
    # Slack bot/user/app tokens: `xox[abprs]-` followed by digits and base64-ish.
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    # JWTs: three base64url segments separated by dots, starting with `eyJ`
    # (decodes to `{"`). Catches OIDC/JWT bearer tokens.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
]
"""Regexes matched against file content; any hit fails the safety check."""


def _matches_secret_pattern(path_str: str) -> str | None:
    """Return the matching glob pattern, or None if path is path-name-safe.

    Matching is case-insensitive — `.ENV` and `Credentials.json` should both
    trip the filter on case-insensitive filesystems (macOS) and case-sensitive
    ones (Linux).
    """
    lowered = path_str.lower()
    for pattern in _SECRET_PATH_PATTERNS:
        if fnmatch.fnmatch(lowered, pattern.lower()):
            return pattern
    return None


def _matches_secret_content(path: Path) -> str | None:
    """Return the matching regex pattern source, or None if content is safe.

    Reads as text with `errors="ignore"` — binary blobs simply won't match.
    Returns None for files that don't exist or can't be read; callers should
    have already verified existence.
    """
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    for regex in _SECRET_CONTENT_REGEXES:
        if regex.search(text):
            return regex.pattern
    return None


def is_safe_to_send(path: Path, repo_root: Path) -> bool:
    """Return True if `path` may be sent to an LLM, False otherwise.

    The check has three layers:
      1. Path must resolve inside `repo_root.resolve()`.
      2. Path name must not match any `_SECRET_PATH_PATTERNS` glob.
      3. File content must not match any `_SECRET_CONTENT_REGEXES`.

    Any failure is logged at WARNING with the reason; the function returns
    False rather than raising so callers can iterate over many paths.
    """
    resolved_root = repo_root.resolve()
    try:
        resolved_path = path.resolve()
    except OSError as e:
        logger.warning("unsafe path %s: could not resolve (%s)", path, e)
        return False

    # Layer 1: containment.
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        logger.warning(
            "unsafe path %s: outside repo_root %s", resolved_path, resolved_root
        )
        return False

    # Layer 2: path-name pattern.
    pattern_hit = _matches_secret_pattern(str(resolved_path))
    if pattern_hit is not None:
        logger.warning(
            "unsafe path %s: matched secret path pattern %r",
            resolved_path,
            pattern_hit,
        )
        return False

    # Layer 3: content regex (only if file exists and is readable).
    if resolved_path.is_file():
        content_hit = _matches_secret_content(resolved_path)
        if content_hit is not None:
            logger.warning(
                "unsafe path %s: content matched credential pattern %r",
                resolved_path,
                content_hit,
            )
            return False

    return True


def assert_safe_to_send(path: Path, repo_root: Path) -> None:
    """Raise `UnsafePathError` if `path` is not safe to send to an LLM.

    Mirrors `is_safe_to_send` but raises with a concrete reason. Use this in
    the ingestion worker where unsafe input should abort the whole call rather
    than silently skip one file.
    """
    resolved_root = repo_root.resolve()
    try:
        resolved_path = path.resolve()
    except OSError as e:
        raise UnsafePathError(f"could not resolve {path}: {e}") from e

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as e:
        raise UnsafePathError(
            f"{resolved_path} is outside repo_root {resolved_root}"
        ) from e

    pattern_hit = _matches_secret_pattern(str(resolved_path))
    if pattern_hit is not None:
        raise UnsafePathError(
            f"{resolved_path} matched secret path pattern {pattern_hit!r}"
        )

    if resolved_path.is_file():
        content_hit = _matches_secret_content(resolved_path)
        if content_hit is not None:
            raise UnsafePathError(
                f"{resolved_path} content matched credential pattern "
                f"{content_hit!r}"
            )
