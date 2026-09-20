"""Sequential extraction.

Walks the manifest one page at a time: navigate → wait for load → extract every
element at full detail → write the page's JSON → mark the manifest entry
``extracted`` → next. It is sequential on purpose: it keeps fingerprint-store
writes ordered and makes resumability simple. Do not parallelize without
revisiting that assumption.

**Resumable.** The manifest is saved after every page, so an interrupted run
loses at most the page in flight. Re-running over the same manifest continues
from the pages that are not ``extracted`` — failed pages are retried, and a page
marked ``extracted`` whose output file has gone missing is extracted again.

Output layout (``output_path`` defaults to ``./output/``)::

    output/
      manifest.json
      pages/0001-example.com.json
      pages/0002-example.com-about.json

A manifest entry's ``output_file`` is relative to ``output_path``.

Page JSON keys: ``schema_version``, ``run_id``, ``url``, ``final_url`` (only when
the page redirected), ``page_type``, ``structural_hash``, ``captured_at``,
``element_counts`` and ``elements`` (each in the raw element schema).

Output files contain page markup — attribute values, link URLs, hidden-input
values — so treat them as sensitive. A password field's markup ``value`` is
redacted at extraction time; everything else is recorded as found.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from healix.auth import BLOCKED, FAILED, SKIPPED, Authenticator
from healix.classification import classify
from healix.classification.rules import LOGIN
from healix.discovery.manifest import (
    EXTRACTED,
    Manifest,
    ManifestPage,
    normalize_url,
    structural_hash,
)
from healix.driver.base import Driver, Element
from healix.fs import write_json_atomic
from healix.healing.baseline import KEEP, MODES, record_page
from healix.healing.store import FingerprintStore
from healix.log import get_logger
from healix.platform_adapters import detected_platform
from healix.timeutil import iso_utc, utc_now

logger = get_logger(__name__)

SCHEMA_VERSION = 1
PAGES_DIR = "pages"
MANIFEST_FILENAME = "manifest.json"

PLATFORM_DETECTION_MODES = ("auto", "off")

Classifier = Callable[[list[Element], str], str]


@dataclass
class ExtractionConfig:
    """The ``crawl.extraction`` block of the run config.

    ``sequence`` and ``output_format`` have a single supported value each today;
    they exist so the run config is accepted as documented. ``platform_detection`` is ``"auto"``
    (the platform adapters may add a ``platform_signal`` to elements, and the manifest records
    which platform was seen) or ``"off"`` (no platform signals; ``platform_detected`` stays
    ``null``). The generic pipeline runs either way.
    """

    sequence: str = "one_by_one"
    output_format: str = "json"
    output_path: str = "./output/"
    iframe_traversal: bool = True
    platform_detection: str = "auto"

    def __post_init__(self) -> None:
        if self.sequence != "one_by_one":
            raise ValueError(f"sequence must be 'one_by_one', got {self.sequence!r}")
        if self.output_format != "json":
            raise ValueError(f"output_format must be 'json', got {self.output_format!r}")
        if self.platform_detection not in PLATFORM_DETECTION_MODES:
            raise ValueError(
                f"platform_detection must be one of {PLATFORM_DETECTION_MODES}, "
                f"got {self.platform_detection!r}"
            )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExtractionConfig:
        return cls(**raw)


@dataclass(frozen=True)
class ExtractedPage:
    """What ``on_page_extracted`` receives — the fields of a ``page_extracted`` event."""

    url: str
    page_type: str
    element_count: int
    output_file: str


# --------------------------------------------------------------------------- #
# Page documents
# --------------------------------------------------------------------------- #


def count_elements(elements: Sequence[Element]) -> dict[str, Any]:
    """Summary counts stored alongside a page's elements.

    ``by_frame`` keys are the ``iframe_path`` joined with ``/`` (``main/panel``).
    """
    return {
        "total": len(elements),
        "visible": sum(1 for e in elements if e.computed.get("visible")),
        "in_shadow_root": sum(1 for e in elements if e.shadow_path),
        "by_tag": dict(sorted(Counter(e.tag for e in elements).items())),
        "by_frame": dict(sorted(Counter("/".join(e.iframe_path) for e in elements).items())),
    }


def build_page_document(
    *,
    run_id: str,
    url: str,
    final_url: str,
    page_type: str,
    elements: Sequence[Element],
    captured_at: str,
) -> dict[str, Any]:
    """The JSON document written for one page."""
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "url": url,
    }
    if final_url != url:
        document["final_url"] = final_url
    document.update(
        {
            "page_type": page_type,
            "structural_hash": structural_hash(elements),
            "captured_at": captured_at,
            "element_counts": count_elements(elements),
            "elements": [e.to_dict() for e in elements],
        }
    )
    return document


def output_filename(index: int, url: str) -> str:
    """Relative path of a page's output file: ``pages/0007-example.com-orders-42.json``.

    ``index`` is the page's 1-based position in the manifest, so names are unique
    and stable; the slug is only for humans.
    """
    parts = urlsplit(url)
    slug = re.sub(r"[^a-z0-9.]+", "-", f"{parts.netloc}{parts.path} {parts.query}".lower())
    slug = slug.strip("-.")[:60].rstrip("-.") or "page"
    return f"{PAGES_DIR}/{index:04d}-{slug}.json"


# --------------------------------------------------------------------------- #
# Extractor
# --------------------------------------------------------------------------- #


class ElementExtractor:
    """Extract every page in a manifest, one at a time, into JSON files.

    ``classifier`` re-classifies each page from its freshly extracted elements, so
    the manifest's provisional discovery-time ``page_type`` becomes the final one;
    it defaults to ``healix.classification.classify`` (``None`` keeps the
    provisional type). ``on_page_extracted`` receives an ``ExtractedPage`` after
    each page is written and recorded.

    With an ``authenticator`` (a ``LoginHandler``) a page that redirects to a login page —
    a session that expired mid-run — triggers a login and is then loaded again; the run
    continues from where it stopped. If that cannot be done the page is marked ``failed``
    (and the manifest ``blocked_on_auth`` when no credentials are set). Login detection
    relies on the classifier, so it needs ``classifier`` to be set.

    With a ``fingerprint_store`` every element a script would act on is fingerprinted as its page
    is extracted — the baseline the healer later repairs against. ``fingerprint_mode="keep"``
    (default) adds only roles the store has not seen, so an existing baseline is never silently
    overwritten; ``"refresh"`` overwrites it.
    """

    def __init__(
        self,
        driver: Driver,
        config: ExtractionConfig | None = None,
        *,
        classifier: Classifier | None = classify,
        on_page_extracted: Callable[[ExtractedPage], None] | None = None,
        clock: Callable[[], datetime] = utc_now,
        authenticator: Authenticator | None = None,
        fingerprint_store: FingerprintStore | None = None,
        fingerprint_mode: str = KEEP,
    ) -> None:
        if fingerprint_mode not in MODES:
            raise ValueError(f"fingerprint_mode must be one of {MODES}, got {fingerprint_mode!r}")
        self.driver = driver
        self.config = config or ExtractionConfig()
        self.classifier = classifier
        self.on_page_extracted = on_page_extracted
        self.clock = clock
        self.authenticator = authenticator
        self.fingerprint_store = fingerprint_store
        self.fingerprint_mode = fingerprint_mode

    def extract(
        self,
        manifest: Manifest | str | os.PathLike[str],
        *,
        manifest_path: str | os.PathLike[str] | None = None,
    ) -> Manifest:
        """Extract every page that is not yet extracted; return the updated manifest.

        ``manifest`` may be a ``Manifest`` or a path to one (which is then also where
        progress is saved unless ``manifest_path`` says otherwise). Progress is saved
        to ``manifest_path`` — by default ``<output_path>/manifest.json`` — after
        every page.
        """
        out_dir = Path(self.config.output_path)
        if not isinstance(manifest, Manifest):
            manifest_path = manifest_path or manifest
            manifest = Manifest.load(manifest)
        save_to = Path(manifest_path) if manifest_path else out_dir / MANIFEST_FILENAME

        todo = self._pending(manifest, out_dir)
        logger.info(
            "extraction started",
            run_id=manifest.run_id,
            pages_total=manifest.pages_discovered,
            pages_remaining=len(todo),
            output_path=str(out_dir),
        )
        extracted = failed = 0
        for index, page in todo:
            if self._extract_page(manifest, page, index, out_dir):
                extracted += 1
            else:
                failed += 1
            manifest.save(save_to)  # after every page: an interruption loses at most one
        manifest.save(save_to)
        logger.info(
            "extraction finished",
            run_id=manifest.run_id,
            extracted=extracted,
            failed=failed,
            pages_extracted=manifest.pages_extracted,
        )
        return manifest

    def _pending(self, manifest: Manifest, out_dir: Path) -> list[tuple[int, ManifestPage]]:
        pending = []
        for index, page in enumerate(manifest.pages, start=1):
            if page.status == EXTRACTED:
                if page.output_file and (out_dir / page.output_file).exists():
                    continue
                logger.warn(
                    "extracted page has no output file; extracting again",
                    url=page.url,
                    output_file=page.output_file,
                )
            pending.append((index, page))
        return pending

    def _load(self, page: ManifestPage) -> tuple[list[Element], str]:
        """Navigate to the page; its elements and the URL it ended up at."""
        self.driver.navigate(page.url)
        final_url = self.driver.current_url
        elements = self.driver.get_elements(iframe_traversal=self.config.iframe_traversal)
        if self.config.platform_detection == "off":
            for element in elements:
                element.platform_signal = None  # whatever the driver was set up to add
        return elements, final_url

    def _fail(self, manifest: Manifest, page: ManifestPage, exc: Exception) -> None:
        logger.warn(
            "could not extract page",
            url=page.url,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        manifest.mark_failed(page.url, str(exc))

    def _extract_page(
        self, manifest: Manifest, page: ManifestPage, index: int, out_dir: Path
    ) -> bool:
        """Extract one page; ``False`` if it failed (and was marked so)."""
        try:
            elements, final_url = self._load(page)
        except Exception as exc:
            self._fail(manifest, page, exc)
            return False

        if self.authenticator is not None:
            page_type = self._classify(elements, final_url, page)
            outcome = self._authenticate(manifest, page, elements, final_url, page_type)
            if outcome is None:
                return False
            elements, final_url = outcome

        previous_type = page.page_type
        page_type = self._classify(elements, final_url, page)
        if manifest.platform_detected is None:
            manifest.platform_detected = detected_platform(elements)
        document = build_page_document(
            run_id=manifest.run_id,
            url=page.url,
            final_url=final_url,
            page_type=page_type,
            elements=elements,
            captured_at=iso_utc(self.clock()),
        )
        if page.structural_hash and document["structural_hash"] != page.structural_hash:
            logger.info(
                "page structure changed since discovery",
                url=page.url,
                discovered=page.structural_hash,
                now=document["structural_hash"],
            )

        # Write the file before recording it: a crash in between just means the page is
        # extracted again on resume, never that the manifest points at a file that isn't there.
        relative = output_filename(index, page.url)
        write_json_atomic(out_dir / relative, document)
        if page_type != previous_type:
            logger.info(
                "page type changed since discovery",
                url=page.url,
                discovered=previous_type,
                now=page_type,
            )
        self._record_fingerprints(page.url, elements)
        page.page_type = page_type
        manifest.mark_extracted(page.url, relative)
        logger.debug(
            "page extracted",
            url=page.url,
            page_type=page_type,
            element_count=len(elements),
            output_file=relative,
        )
        if self.on_page_extracted:
            self.on_page_extracted(
                ExtractedPage(
                    url=page.url,
                    page_type=page_type,
                    element_count=len(elements),
                    output_file=relative,
                )
            )
        return True

    def _record_fingerprints(self, page_url: str, elements: list[Element]) -> None:
        if self.fingerprint_store is None:
            return
        try:
            summary = record_page(
                self.fingerprint_store, page_url, elements, mode=self.fingerprint_mode
            )
        except Exception as exc:  # the page is extracted; a store problem must not lose it
            logger.warn(
                "could not record fingerprints",
                url=page_url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return
        logger.debug(
            "fingerprints recorded",
            url=page_url,
            added=summary.added,
            refreshed=summary.refreshed,
            kept=summary.kept,
        )

    def _classify(self, elements: list[Element], final_url: str, page: ManifestPage) -> str:
        return self.classifier(elements, final_url) if self.classifier else page.page_type

    def _authenticate(
        self,
        manifest: Manifest,
        page: ManifestPage,
        elements: list[Element],
        final_url: str,
        page_type: str,
    ) -> tuple[list[Element], str] | None:
        """Log in if this page needs it. The (elements, URL) to extract, or ``None`` if the
        page could not be reached and was marked failed."""
        assert self.authenticator is not None
        result = self.authenticator.handle_page(page.url, final_url, elements, page_type)
        if self.authenticator.blocked:
            manifest.blocked_on_auth = True
        bounced = not _same_url(page.url, final_url)

        if result.status in (BLOCKED, FAILED, SKIPPED):
            if bounced:
                why = (
                    "no credentials are set"
                    if result.status == BLOCKED
                    else "the login did not succeed"
                )
                manifest.mark_failed(page.url, f"requires authentication, and {why}")
                return None
            return elements, final_url  # the login page itself: extract it as it is
        if not result.authenticated or not bounced:
            return elements, final_url  # extract the login page as it was before logging in

        # We were sent to a login page and have now logged in: load the page we wanted.
        try:
            elements, final_url = self._load(page)
        except Exception as exc:
            self._fail(manifest, page, exc)
            return None
        if self._classify(elements, final_url, page) == LOGIN and not _same_url(
            page.url, final_url
        ):
            self.authenticator.report_session_invalid(final_url)
            manifest.mark_failed(
                page.url, "requires authentication, and the session did not persist"
            )
            return None
        return elements, final_url


def _same_url(a: str, b: str) -> bool:
    try:
        return normalize_url(a) == normalize_url(b)
    except ValueError:
        return a == b
