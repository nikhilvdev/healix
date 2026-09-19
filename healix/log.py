"""Healix's logging, built on `logquill <https://pypi.org/project/logquill/>`_.

Modules get a logger with ``get_logger(__name__)`` and log structured records —
a short constant message plus keyword metadata — rather than formatted strings::

    logger = get_logger(__name__)
    logger.warn("could not load page", url=url, error=str(exc))

By default Healix logs ``WARN`` and above as JSON lines on **stderr** (stdout is
left free for program output), at the level given by ``HEALIX_LOG_LEVEL`` if set.
Use ``configure_logging`` to change the level or send records elsewhere::

    from logquill import FileTransport
    configure_logging(level="debug", transports=[FileTransport("healix.log")])

``configure_logging(transports=[])`` silences Healix entirely.

logquill's ``Logger.child()`` copies its parent's transport list, so a logger
created at import time would not see a later reconfiguration. ``configure_logging``
therefore updates every logger handed out here, not just the root.
"""

from __future__ import annotations

import os
import sys
import warnings
from collections.abc import Mapping, Sequence

from logquill import ConsoleTransport, Level, Logger
from logquill.levels import parse_level
from logquill.transports.transport import Transport

ROOT_NAME = "healix"
LEVEL_ENV_VAR = "HEALIX_LOG_LEVEL"
DEFAULT_LEVEL = Level.WARN


def level_from_env(environ: Mapping[str, str]) -> Level:
    """The level named by ``HEALIX_LOG_LEVEL``, or ``WARN`` if unset or not a valid level."""
    raw = environ.get(LEVEL_ENV_VAR)
    if not raw:
        return DEFAULT_LEVEL
    try:
        return parse_level(raw)
    except ValueError:
        warnings.warn(
            f"ignoring invalid {LEVEL_ENV_VAR}={raw!r}; using {DEFAULT_LEVEL.name}",
            stacklevel=2,
        )
        return DEFAULT_LEVEL


def _default_transport() -> Transport:
    return ConsoleTransport(stdout=sys.stderr, stderr=sys.stderr, colorize=sys.stderr.isatty())


_root = Logger(ROOT_NAME, level=level_from_env(os.environ), transports=[_default_transport()])
_loggers: dict[str, Logger] = {}


def get_logger(name: str = ROOT_NAME) -> Logger:
    """The Healix logger for ``name`` — normally ``__name__``.

    ``"healix.discovery.crawler"`` yields a logger of that name, derived from the
    root ``healix`` logger. The same name always returns the same logger.
    """
    if name == ROOT_NAME:
        return _root
    relative = name.removeprefix(f"{ROOT_NAME}.")
    if relative not in _loggers:
        _loggers[relative] = _root.child(relative)
    return _loggers[relative]


def configure_logging(
    level: int | str | Level | None = None,
    transports: Sequence[Transport] | None = None,
) -> Logger:
    """Change the level and/or transports of every Healix logger.

    ``None`` leaves that setting as it is. Returns the root ``healix`` logger.
    """
    if transports is not None:
        _root.transports = list(transports)
    if level is not None:
        _root.set_level(level)
    for child in _loggers.values():
        child.transports = _root.transports
        child.set_level(_root.level)
    return _root
