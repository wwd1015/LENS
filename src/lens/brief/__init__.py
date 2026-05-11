"""HTML morning brief — analyst-facing artifact for a LENS run.

Renders a list of :class:`lens.types.Finding` plus optional RCA results into a
single self-contained HTML file, with autoescape enabled to neutralize any
LLM-authored hostile content in descriptions / RCA hypotheses.
"""

from lens.brief.html import render_brief

__all__ = ["render_brief"]
