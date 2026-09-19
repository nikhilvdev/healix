"""The run configuration: one JSON document, safe to commit.

::

    {
      "mode": "guided | autonomous",
      "base_url": "https://example.com",
      "start_url": "https://example.com/login",
      "crawl": {
        "discovery":  {"domain_scope": "same_domain", "max_pages": 50, ...},
        "extraction": {"output_path": "./output/", "iframe_traversal": true, ...}
      },
      "backend": "playwright | selenium"
    }

* **guided** — ``start_url`` is given; the crawl starts there (and from ``base_url`` too,
  if both are given).
* **autonomous** — only ``base_url`` is given; the crawl discovers from scratch.

``mode`` may be omitted: it is guided if ``start_url`` is present, else autonomous.

The config never carries secrets. Any key that looks like a credential is rejected with
a pointer to ``.env`` — the file is meant to be committed to a repo.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from healix.discovery.crawler import DiscoveryConfig
from healix.extraction import ExtractionConfig

MODES = ("guided", "autonomous")
BACKENDS = ("playwright", "selenium")

_TOP_LEVEL_KEYS = frozenset({"mode", "base_url", "start_url", "crawl", "backend"})
_CRAWL_KEYS = frozenset({"discovery", "extraction"})
_SECRET_KEY = re.compile(
    r"pass(word|wd)|secret|token|api[-_]?key|credential|username", re.IGNORECASE
)


class ConfigError(ValueError):
    """The run configuration is invalid."""


def _reject_secrets(node: Any, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY.search(str(key)):
                raise ConfigError(
                    f"{here!r} looks like a credential. The run config must be safe to commit, so "
                    "secrets and credentials belong in the environment (a git-ignored .env file), "
                    "never in this file."
                )
            _reject_secrets(value, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _reject_secrets(item, f"{path}[{i}]")


def _check_url(name: str, value: Any) -> str:
    parts = urlsplit(value) if isinstance(value, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(f"{name} must be an absolute http(s) URL, got {value!r}")
    return str(value)


def _block(name: str, raw: Any, cls: type[Any]) -> Any:
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be an object")
    try:
        return cls.from_dict(raw)
    except TypeError as exc:  # an unknown key
        raise ConfigError(f"{name}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"{name}: {exc}") from exc


@dataclass(frozen=True)
class RunConfig:
    mode: str
    base_url: str | None
    start_url: str | None
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    backend: str = "playwright"

    @property
    def start_urls(self) -> list[str]:
        """Where discovery begins: the start URL (guided) and/or the base URL."""
        urls = [self.start_url, self.base_url] if self.mode == "guided" else [self.base_url]
        return list(dict.fromkeys(u for u in urls if u))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, require_start: bool = True) -> RunConfig:
        """Validate and build a config. ``require_start=False`` is for extraction-only use."""
        if not isinstance(raw, dict):
            raise ConfigError("the run config must be a JSON object")
        _reject_secrets(raw)
        if unknown := set(raw) - _TOP_LEVEL_KEYS:
            raise ConfigError(
                f"unknown config keys: {sorted(unknown)}; allowed: {sorted(_TOP_LEVEL_KEYS)}"
            )
        crawl = raw.get("crawl", {})
        if not isinstance(crawl, dict):
            raise ConfigError("crawl must be an object")
        if unknown := set(crawl) - _CRAWL_KEYS:
            raise ConfigError(
                f"unknown crawl keys: {sorted(unknown)}; allowed: {sorted(_CRAWL_KEYS)}"
            )

        base_url = _check_url("base_url", raw["base_url"]) if raw.get("base_url") else None
        start_url = _check_url("start_url", raw["start_url"]) if raw.get("start_url") else None
        mode = raw.get("mode") or ("guided" if start_url else "autonomous")
        if mode not in MODES:
            raise ConfigError(f"mode must be one of {MODES}, got {mode!r}")
        if require_start:
            if mode == "guided" and not start_url:
                raise ConfigError("guided mode needs a start_url")
            if mode == "autonomous" and not base_url:
                raise ConfigError("autonomous mode needs a base_url")
        backend = raw.get("backend", "playwright")
        if backend not in BACKENDS:
            raise ConfigError(f"backend must be one of {BACKENDS}, got {backend!r}")

        return cls(
            mode=mode,
            base_url=base_url,
            start_url=start_url,
            discovery=_block("crawl.discovery", crawl.get("discovery"), DiscoveryConfig),
            extraction=_block("crawl.extraction", crawl.get("extraction"), ExtractionConfig),
            backend=backend,
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str], *, require_start: bool = True) -> RunConfig:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        return cls.from_dict(raw, require_start=require_start)

    @classmethod
    def coerce(
        cls,
        config: RunConfig | dict[str, Any] | str | os.PathLike[str] | None,
        *,
        require_start: bool = True,
    ) -> RunConfig:
        """Accept a ``RunConfig``, a dict, or a path to a JSON file (``None`` -> defaults)."""
        if isinstance(config, RunConfig):
            return config
        if config is None:
            return cls(mode="autonomous", base_url=None, start_url=None)
        if isinstance(config, dict):
            return cls.from_dict(config, require_start=require_start)
        return cls.load(config, require_start=require_start)
