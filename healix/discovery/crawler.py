"""Discovery: find every reachable page.

Walks links from the start point(s), visiting every distinct in-scope page.
There is no depth cutoff: it runs until the frontier is empty or the
``max_pages`` safety ceiling is hit. Extraction is a separate step and lives elsewhere;
discovery only records what exists.

Links come from ``Driver.get_elements()``, so anchors inside same-origin
iframes and open shadow roots are found with no extra code. Navigation that
only happens through script (buttons, router pushes with no ``<a href>``) is not
discoverable this way.
"""

from __future__ import annotations

import os
import uuid
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from healix.classification import classify
from healix.discovery import manifest as m
from healix.discovery.manifest import (
    Manifest,
    ManifestPage,
    normalize_url,
    structural_hash,
    template_key,
)
from healix.driver.base import Driver, Element
from healix.log import get_logger

logger = get_logger(__name__)

DOMAIN_SCOPES = ("same_domain", "same_origin")
DEDUPE_MODES = ("url_normalized", "url_normalized_and_structural_hash")

# Links to these are resources, not pages.
_NON_PAGE_EXTENSIONS = frozenset(
    {
        "pdf",
        "zip",
        "gz",
        "tar",
        "rar",
        "7z",
        "exe",
        "dmg",
        "msi",
        "apk",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "webp",
        "ico",
        "bmp",
        "mp3",
        "mp4",
        "mov",
        "avi",
        "webm",
        "wav",
        "css",
        "js",
        "json",
        "xml",
        "csv",
        "xls",
        "xlsx",
        "doc",
        "docx",
        "ppt",
        "pptx",
        "woff",
        "woff2",
        "ttf",
        "eot",
    }
)

# (elements, final URL) -> page type. ``healix.classification.classify`` fits this shape.
Classifier = Callable[[list[Element], str], str]


@dataclass
class DiscoveryConfig:
    """The ``crawl.discovery`` block of the run config.

    ``template_sample_size``: after this many URLs of one path template have been
    visited (``/product/1``, ``/product/2``, ...), further URLs of that template
    are deferred until everything else has been visited. Nothing is dropped — it
    only keeps a large family of look-alike pages from using up ``max_pages``
    ahead of distinct pages. ``0`` disables deferral.
    """

    domain_scope: str = "same_domain"
    max_pages: int = 50
    dedupe_by: str = "url_normalized_and_structural_hash"
    template_sample_size: int = 3

    def __post_init__(self) -> None:
        if self.domain_scope not in DOMAIN_SCOPES:
            raise ValueError(
                f"domain_scope must be one of {DOMAIN_SCOPES}, got {self.domain_scope!r}"
            )
        if self.dedupe_by not in DEDUPE_MODES:
            raise ValueError(f"dedupe_by must be one of {DEDUPE_MODES}, got {self.dedupe_by!r}")
        if self.max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if self.template_sample_size < 0:
            raise ValueError("template_sample_size cannot be negative")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DiscoveryConfig:
        return cls(**raw)


class DiscoveryCrawler:
    """Discover all reachable pages through a ``Driver`` and produce a ``Manifest``.

    ``classifier`` assigns each page's provisional ``page_type`` from its elements
    and URL; it defaults to the rule-based ``healix.classification.classify``. Pass
    ``None`` to skip classification (every page is then ``"unknown"``).
    ``on_page_discovered`` is called with each new manifest page as it is recorded.
    """

    def __init__(
        self,
        driver: Driver,
        config: DiscoveryConfig | None = None,
        *,
        classifier: Classifier | None = classify,
        on_page_discovered: Callable[[ManifestPage], None] | None = None,
    ) -> None:
        self.driver = driver
        self.config = config or DiscoveryConfig()
        self.classifier = classifier
        self.on_page_discovered = on_page_discovered

    def discover(
        self,
        start_urls: list[str],
        run_id: str | None = None,
        *,
        manifest_path: str | os.PathLike[str] | None = None,
    ) -> Manifest:
        """Crawl from ``start_urls`` and return the manifest.

        With ``manifest_path`` the manifest is also written there when discovery
        ends — including when it is interrupted (``discovery_status`` is then
        ``interrupted``), so a crash doesn't lose what was found.
        """
        if not start_urls:
            raise ValueError("at least one start URL is required")
        config = self.config
        starts = [normalize_url(u) for u in start_urls]
        scope = _Scope(config.domain_scope, starts)
        manifest = Manifest(run_id=run_id or uuid.uuid4().hex[:12], start_urls=list(start_urls))

        logger.info(
            "discovery started",
            run_id=manifest.run_id,
            start_urls=starts,
            max_pages=config.max_pages,
            domain_scope=config.domain_scope,
        )
        queue: deque[str] = deque(dict.fromkeys(starts))
        deferred: deque[str] = deque()
        seen: set[str] = set(queue)  # ever queued
        visited: set[str] = set()  # actually loaded (after redirects)
        template_visits: Counter[str] = Counter()
        visits = 0

        try:
            while (queue or deferred) and visits < config.max_pages:
                if queue:
                    url = queue.popleft()
                    key = template_key(url)
                    if (
                        config.template_sample_size
                        and template_visits[key] >= config.template_sample_size
                    ):
                        deferred.append(url)
                        continue
                else:
                    url = deferred.popleft()
                    key = template_key(url)

                visits += 1
                template_visits[key] += 1
                page_links = self._visit(url, manifest, scope, visited)
                for link in page_links:
                    if link not in seen:
                        seen.add(link)
                        queue.append(link)
            manifest.discovery_status = (
                m.COMPLETE if not (queue or deferred) else m.MAX_PAGES_REACHED
            )
        except BaseException:
            manifest.discovery_status = m.INTERRUPTED
            raise
        finally:
            if manifest_path is not None:
                manifest.save(manifest_path)
            logger.info(
                "discovery finished",
                run_id=manifest.run_id,
                status=manifest.discovery_status,
                visits=visits,
                pages_discovered=manifest.pages_discovered,
            )
        return manifest

    def _visit(self, url: str, manifest: Manifest, scope: _Scope, visited: set[str]) -> list[str]:
        """Load ``url``, record it, and return the in-scope links found on it."""
        try:
            self.driver.navigate(url)
        except Exception as exc:
            logger.warn(
                "could not load page", url=url, error=str(exc), error_type=type(exc).__name__
            )
            manifest.add_failed(url, str(exc))
            visited.add(url)
            return []

        final = _safe_normalize(self.driver.current_url) or url
        visited.add(url)
        if final in visited and final != url:
            logger.debug("redirected to an already-visited page", url=url, final_url=final)
            return []
        visited.add(final)
        if not scope.allows(final):
            logger.info("redirected out of scope; skipping", url=url, final_url=final)
            return []

        try:
            elements = self.driver.get_elements()
        except Exception as exc:
            logger.warn(
                "could not extract elements",
                url=final,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            manifest.add_failed(final, str(exc))
            return []
        page_hash = structural_hash(elements)
        page_type = self.classifier(elements, final) if self.classifier else m.UNKNOWN_PAGE_TYPE
        page, is_new = manifest.add_page(
            final,
            page_hash,
            page_type,
            dedupe_structural=self.config.dedupe_by == "url_normalized_and_structural_hash",
        )
        if is_new:
            logger.debug(
                "page discovered", url=final, page_type=page_type, structural_hash=page_hash
            )
            if self.on_page_discovered:
                self.on_page_discovered(page)
        else:
            logger.debug("same structure as an existing page", url=final, representative=page.url)

        # Links are followed from every visited page, including structural duplicates —
        # a repeated template can still link somewhere new.
        return [link for link in _links(elements) if scope.allows(link)]


class _Scope:
    """Which URLs discovery may visit, derived from the start URLs."""

    def __init__(self, domain_scope: str, start_urls: list[str]) -> None:
        self.domain_scope = domain_scope
        self._hosts = {_host_key(u) for u in start_urls}
        self._origins = {_origin(u) for u in start_urls}

    def allows(self, url: str) -> bool:
        if self.domain_scope == "same_origin":
            return _origin(url) in self._origins
        return _host_key(url) in self._hosts


def _host_key(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _safe_normalize(url: str) -> str | None:
    try:
        return normalize_url(url)
    except ValueError:
        return None


def _is_page_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    last_segment = parts.path.rsplit("/", 1)[-1]
    extension = last_segment.rsplit(".", 1)[-1].lower() if "." in last_segment else ""
    return extension not in _NON_PAGE_EXTENSIONS


def _links(elements: list[Element]) -> list[str]:
    """Normalized, crawlable, de-duplicated link targets in document order."""
    found: dict[str, None] = {}
    for element in elements:
        href = element.computed.get("href")
        if not href:
            continue
        normalized = _safe_normalize(href)
        if normalized and _is_page_url(normalized):
            found[normalized] = None
    return list(found)
