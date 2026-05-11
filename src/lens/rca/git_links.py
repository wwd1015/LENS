"""Commit-URL resolution for git remotes.

Given a commit SHA and a repository root, derive an HTTP URL pointing at the
commit on its hosting provider (GitHub or GitLab). The hosting form lives in
the ``remote.origin.url`` git config, which can be either SSH-style
(``git@github.com:org/repo.git``) or HTTPS (``https://github.com/org/repo.git``).
Anything we don't recognize returns ``None`` rather than guessing — RCA
references should never link out to a wrong URL.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


_SSH_RE = re.compile(r"^git@(?P<host>[^:]+):(?P<path>.+?)(?:\.git)?$")
_HTTPS_RE = re.compile(r"^https?://(?P<host>[^/]+)/(?P<path>.+?)(?:\.git)?$")


def _read_remote_url(repo_root: Path) -> str | None:
    """Return the ``remote.origin.url`` value, or ``None`` on any git failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        logger.debug("git_links: could not read remote.origin.url: %s", exc)
        return None
    url = result.stdout.strip()
    return url or None


def _parse_remote(url: str) -> tuple[str, str] | None:
    """Split a remote URL into (host, ``org/repo``). Returns ``None`` on no match."""
    m = _SSH_RE.match(url)
    if m is None:
        m = _HTTPS_RE.match(url)
    if m is None:
        return None
    return m.group("host"), m.group("path")


def commit_url(sha: str, repo_root: Path) -> str | None:
    """Build a hosting-provider commit URL for ``sha``.

    Recognized hosts:

    - ``github.com`` — ``https://github.com/<org>/<repo>/commit/<sha>``
    - ``gitlab.com`` — ``https://gitlab.com/<org>/<repo>/-/commit/<sha>``
      (note GitLab's ``-/`` path segment).

    Returns ``None`` for any other host, an unparseable remote URL, or any
    underlying git failure. Callers should treat ``None`` as "no clickable
    URL is available" and surface the bare SHA instead.
    """
    if not sha:
        return None
    remote = _read_remote_url(Path(repo_root))
    if remote is None:
        return None
    parsed = _parse_remote(remote)
    if parsed is None:
        return None
    host, path = parsed
    path = path.rstrip("/")
    if host == "github.com":
        return f"https://github.com/{path}/commit/{sha}"
    if host == "gitlab.com":
        return f"https://gitlab.com/{path}/-/commit/{sha}"
    return None
