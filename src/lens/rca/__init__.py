"""LENS RCA — per-finding root-cause analysis backed by Claude Code headless.

Public surface:
    - :class:`RCAAgent`: orchestrates evidence-gathering and the LLM call.
    - :func:`commit_url`: derive a hosting-provider commit URL from a SHA.
"""

from lens.rca.agent import RCAAgent
from lens.rca.git_links import commit_url

__all__ = ["RCAAgent", "commit_url"]
