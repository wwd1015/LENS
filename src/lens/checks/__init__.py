"""Check framework — base class and registry for all DQ checks."""

from lens.checks.base import BaseCheck
from lens.checks.registry import CheckRegistry, registry

__all__ = ["BaseCheck", "CheckRegistry", "registry"]
