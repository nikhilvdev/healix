"""What ``healix doctor`` learns about a backend, without launching it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackendReport:
    """The state of one browser backend.

    ``installed`` — the Python package can be imported. ``browser`` — where the browser it needs
    was found. ``problem`` — why it cannot be used, if it cannot; ``hint`` — how to fix that.
    """

    backend: str
    installed: bool
    version: str | None = None
    browser: str | None = None
    problem: str | None = None
    hint: str | None = None

    @property
    def ready(self) -> bool:
        return self.installed and self.problem is None
