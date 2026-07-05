"""Global check registry for discovery and instantiation."""

from __future__ import annotations

import logging
from typing import Any, Type

from lens.checks.base import BaseCheck

logger = logging.getLogger(__name__)


class CheckRegistry:
    """Registry that maps check names to check classes.

    Use as a decorator::

        @registry.register
        class MyCheck(BaseCheck):
            name = "my_check"
            ...
    """

    def __init__(self) -> None:
        self._checks: dict[str, Type[BaseCheck]] = {}

    def register(self, cls: Type[BaseCheck]) -> Type[BaseCheck]:
        name = cls.name or cls.__name__
        existing = self._checks.get(name)
        if existing is not None and existing is not cls:
            # Last-import-wins is the historical behavior; keep it, but a
            # silent overwrite (e.g. a forgotten `name` attribute falling
            # back to the class name) should at least leave a trace.
            logger.warning(
                "check registry: name %r re-registered by %s (was %s)",
                name,
                cls.__qualname__,
                existing.__qualname__,
            )
        self._checks[name] = cls
        return cls

    def get(self, name: str) -> Type[BaseCheck]:
        if name not in self._checks:
            raise KeyError(f"Check '{name}' not found in registry. Available: {list(self._checks)}")
        return self._checks[name]

    def create(self, name: str, **kwargs: Any) -> BaseCheck:
        return self.get(name)(**kwargs)

    def list_checks(self) -> list[str]:
        return sorted(self._checks.keys())


# Global singleton
registry = CheckRegistry()
