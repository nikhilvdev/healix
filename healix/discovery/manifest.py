"""The page manifest: what discovery found and how far extraction has got.

Owns everything about page identity:

* ``normalize_url`` — strips session/tracking noise so one page has one URL.
* ``template_key``  — collapses volatile path segments (``/product/123`` and
  ``/product/456`` share a key).
* ``structural_hash`` — fingerprint of a page's DOM structure, independent of
  its text, so two pages rendering the same template hash the same.
* ``Manifest`` — read/write, dedup by normalized URL and structural hash, and
  per-page ``pending | extracted | failed`` status for resumability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from healix.driver.base import Element
from healix.fs import write_json_atomic
from healix.ids import normalize_id

PENDING = "pending"
EXTRACTED = "extracted"
FAILED = "failed"
STATUSES = (PENDING, EXTRACTED, FAILED)

UNKNOWN_PAGE_TYPE = "unknown"

# How discovery ended. Anything but ``complete`` means the manifest may be missing pages.
IN_PROGRESS = "in_progress"
COMPLETE = "complete"
MAX_PAGES_REACHED = "max_pages_reached"
INTERRUPTED = "interrupted"

# --------------------------------------------------------------------------- #
# URL identity
# --------------------------------------------------------------------------- #

_DEFAULT_PORTS = {"http": 80, "https": 443}
_NOISE_PARAMS = frozenset(
    {
        "gclid",
        "fbclid",
        "msclkid",
        "dclid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "sessionid",
        "session_id",
        "sid",
        "phpsessid",
        "jsessionid",
        "sessid",
        "cfid",
        "cftoken",
    }
)
_NOISE_PREFIXES = ("utm_",)
_PATH_SESSION_PARAM = re.compile(r";jsessionid=[^/?#]*", re.IGNORECASE)


def _is_noise_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _NOISE_PARAMS or lowered.startswith(_NOISE_PREFIXES)


def normalize_url(url: str) -> str:
    """Canonical form of ``url`` for dedup.

    Lowercases scheme/host, drops default ports and userinfo, strips session and
    tracking parameters, sorts the remaining query, removes a trailing slash and
    the fragment. A fragment that is a client-side route (``#/x``, ``#!/x``) is
    kept, since on hash-routed apps it *is* the page.

    Raises ``ValueError`` for malformed URLs (e.g. a bad port).
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parts.port
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"

    path = _PATH_SESSION_PARAM.sub("", parts.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_noise_param(k)
        )
    )
    fragment = parts.fragment if parts.fragment.startswith(("/", "!")) else ""
    return urlunsplit((scheme, netloc, path, query, fragment))


def template_key(url: str) -> str:
    """Key shared by URLs that differ only in volatile path segments (ids, uuids, numbers)."""
    parts = urlsplit(normalize_url(url))
    path = "/".join(normalize_id(segment) or segment for segment in parts.path.split("/"))
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


# --------------------------------------------------------------------------- #
# Structural hash
# --------------------------------------------------------------------------- #


def structural_hash(elements: Iterable[Element]) -> str:
    """Fingerprint of a page's DOM *structure*.

    Built from the set of distinct element signatures — tag, parent tag,
    normalized id, name, ``type``, ``role``, frame depth, shadow-ness — so text,
    list length and volatile ids don't matter: ``/product/123`` and
    ``/product/456`` on one template hash the same. CSS classes are deliberately
    excluded because they carry per-page state (``active``, ``selected``).
    """
    signatures = {
        (
            e.tag,
            e.dom_context.get("parent_tag") or "",
            e.id_normalized or "",
            e.name or "",
            e.attributes.get("type", ""),
            e.attributes.get("role", ""),
            len(e.iframe_path),
            bool(e.shadow_path),
        )
        for e in elements
    }
    digest = hashlib.sha256(json.dumps(sorted(signatures)).encode()).hexdigest()
    return digest[:16]


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


@dataclass
class ManifestPage:
    url: str
    structural_hash: str | None
    page_type: str = UNKNOWN_PAGE_TYPE
    status: str = PENDING
    output_file: str | None = None
    # Other URLs visited that rendered this same structure (recognised as one page type).
    variant_urls: list[str] = field(default_factory=list)
    error: str | None = None
    # How the page was found when it was not by following a link: ``"click"`` (click-through
    # discovery) and the page whose button led here. Left out of the JSON when unset.
    discovered_via: str | None = None
    discovered_from: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ManifestPage:
        if raw.get("status", PENDING) not in STATUSES:
            raise ValueError(f"invalid page status {raw['status']!r} for {raw.get('url')!r}")
        return cls(
            url=raw["url"],
            structural_hash=raw.get("structural_hash"),
            page_type=raw.get("page_type", UNKNOWN_PAGE_TYPE),
            status=raw.get("status", PENDING),
            output_file=raw.get("output_file"),
            variant_urls=list(raw.get("variant_urls") or []),
            error=raw.get("error"),
            discovered_via=raw.get("discovered_via"),
            discovered_from=raw.get("discovered_from"),
        )

    def to_dict(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "url": self.url,
            "page_type": self.page_type,
            "structural_hash": self.structural_hash,
            "status": self.status,
            "output_file": self.output_file,
            "variant_urls": self.variant_urls,
            "error": self.error,
        }
        if self.discovered_via:
            raw["discovered_via"] = self.discovered_via
            raw["discovered_from"] = self.discovered_from
        return raw


@dataclass
class Manifest:
    run_id: str
    start_urls: list[str] = field(default_factory=list)
    discovery_status: str = IN_PROGRESS
    platform_detected: str | None = None
    # A login page was reached but no credentials were set: pages behind it were not reachable.
    blocked_on_auth: bool = False
    # The user role this manifest was crawled as (multi-role runs); left out of the JSON when unset.
    role: str | None = None
    # What click-through discovery did (``clicks``, ``pages_found``, ``skipped_unsafe``,
    # ``blocked_writes``); ``None`` when it was not on, and then left out of the JSON.
    click_discovery: dict[str, int] | None = None
    pages: list[ManifestPage] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_url: dict[str, ManifestPage] = {}
        self._by_hash: dict[str, ManifestPage] = {}
        for page in self.pages:
            self._index(page)

    def _index(self, page: ManifestPage) -> None:
        self._by_url[page.url] = page
        for variant in page.variant_urls:
            self._by_url[variant] = page
        if page.structural_hash:
            self._by_hash.setdefault(page.structural_hash, page)

    # -- counts ------------------------------------------------------------- #

    @property
    def pages_discovered(self) -> int:
        return len(self.pages)

    @property
    def pages_extracted(self) -> int:
        return sum(1 for p in self.pages if p.status == EXTRACTED)

    # -- lookup & dedup ----------------------------------------------------- #

    def find_by_url(self, url: str) -> ManifestPage | None:
        """The page recorded for ``url``, whether as its own entry or as a variant of one."""
        return self._by_url.get(normalize_url(url))

    def add_page(
        self,
        url: str,
        page_hash: str | None,
        page_type: str = UNKNOWN_PAGE_TYPE,
        *,
        dedupe_structural: bool = True,
    ) -> tuple[ManifestPage, bool]:
        """Record a visited page, deduping by normalized URL and (optionally) structural hash.

        Returns ``(page, is_new)``. When the URL was already recorded, or its
        structure matches an existing page, the existing page is returned with
        ``is_new=False`` (a structural match adds ``url`` to its ``variant_urls``).
        """
        url = normalize_url(url)
        existing = self._by_url.get(url)
        if existing is not None:
            return existing, False
        if dedupe_structural and page_hash:
            representative = self._by_hash.get(page_hash)
            if representative is not None:
                representative.variant_urls.append(url)
                self._by_url[url] = representative
                return representative, False
        page = ManifestPage(url=url, structural_hash=page_hash, page_type=page_type)
        self.pages.append(page)
        self._index(page)
        return page, True

    def add_failed(self, url: str, error: str) -> ManifestPage:
        """Record a URL that could not be loaded, so it is visible and retried on resume."""
        page, is_new = self.add_page(url, None)
        if is_new:
            page.status = FAILED
            page.error = error
        return page

    # -- status tracking / resumability ------------------------------------- #

    def _require(self, url: str) -> ManifestPage:
        page = self.find_by_url(url)
        if page is None:
            raise KeyError(f"{url!r} is not in the manifest")
        return page

    def mark_extracted(self, url: str, output_file: str) -> ManifestPage:
        page = self._require(url)
        page.status, page.output_file, page.error = EXTRACTED, output_file, None
        return page

    def mark_failed(self, url: str, error: str) -> ManifestPage:
        page = self._require(url)
        page.status, page.error = FAILED, error
        return page

    def remaining_pages(self) -> list[ManifestPage]:
        """Pages still to extract, in manifest order: everything not yet ``extracted``.

        This is what a re-run of the same ``run_id`` continues from — failed
        pages are retried, extracted pages are not redone.
        """
        return [p for p in self.pages if p.status != EXTRACTED]

    # -- persistence -------------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "start_urls": self.start_urls,
            "discovery_status": self.discovery_status,
            "pages_discovered": self.pages_discovered,
            "pages_extracted": self.pages_extracted,
            "platform_detected": self.platform_detected,
            "blocked_on_auth": self.blocked_on_auth,
            **({"role": self.role} if self.role else {}),
            **({"click_discovery": self.click_discovery} if self.click_discovery else {}),
            "pages": [p.to_dict() for p in self.pages],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Manifest:
        return cls(
            run_id=raw["run_id"],
            start_urls=list(raw.get("start_urls") or []),
            discovery_status=raw.get("discovery_status", COMPLETE),
            platform_detected=raw.get("platform_detected"),
            blocked_on_auth=bool(raw.get("blocked_on_auth", False)),
            role=raw.get("role") or None,
            click_discovery=raw.get("click_discovery") or None,
            pages=[ManifestPage.from_dict(p) for p in raw.get("pages") or []],
        )

    def save(self, path: str | os.PathLike[str]) -> None:
        """Write atomically, so a crash mid-write never leaves a truncated manifest."""
        write_json_atomic(path, self.to_dict())

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Manifest:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
