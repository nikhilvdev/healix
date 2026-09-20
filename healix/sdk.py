"""The public SDK: ``Crawler`` and ``Extractor``.

::

    from healix import Crawler

    run = Crawler("run_config.json", on_event=print).discover_and_extract()
    print(run.pages_extracted, "pages ->", run.manifest_path)

Both classes take a run config (a path, a dict, or a ``RunConfig``), an optional
``on_event`` callback and/or ``webhook_url``, and manage their own browser unless you
pass a ``driver`` (in which case its lifecycle is yours). ``on_event`` and the webhook
receive the *same* payload dicts — see ``healix.events``.

Login is automatic: a page the classifier calls ``login`` is filled and submitted with
``credentials`` (default: ``WEBLIB_LOGIN_USERNAME`` / ``WEBLIB_LOGIN_PASSWORD`` from the
environment); pass ``auto_login=False`` to turn it off. See ``healix.auth``.

The SDK does not read ``.env`` for you: call ``dotenv.load_dotenv()`` first if you keep the
login credentials or ``HEALIX_WEBHOOK_SECRET`` there. (The CLI does.)

Events describe the work done by *this* call: resuming a run whose discovery already
finished does not re-emit ``page_discovered`` for those pages.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from healix.auth import Credentials, LoginHandler
from healix.config import RunConfig
from healix.discovery.crawler import DiscoveryCrawler, Scope
from healix.discovery.manifest import (
    COMPLETE,
    FAILED,
    MAX_PAGES_REACHED,
    Manifest,
    ManifestPage,
    normalize_url,
)
from healix.driver.base import Driver
from healix.driver.factory import create_driver
from healix.events import (
    LOGIN_FAILED,
    PAGE_DISCOVERED,
    PAGE_EXTRACTED,
    RUN_COMPLETE,
    EventCallback,
    EventEmitter,
    open_sender,
    validate_outbox,
    validate_webhook_url,
)
from healix.extraction import ElementExtractor, ExtractedPage
from healix.extraction.element_extractor import MANIFEST_FILENAME
from healix.healing import KEEP, FingerprintStore, open_store
from healix.log import get_logger

logger = get_logger(__name__)

WEBHOOK_SECRET_ENV = "HEALIX_WEBHOOK_SECRET"

ConfigLike = RunConfig | dict[str, Any] | str | os.PathLike[str]


class RunConflictError(ValueError):
    """The output directory already holds a different run than the one requested."""


@dataclass
class Run:
    """The outcome of a ``Crawler`` or ``Extractor`` call."""

    run_id: str
    manifest: Manifest
    manifest_path: Path
    output_path: Path

    @property
    def pages(self) -> list[ManifestPage]:
        return self.manifest.pages

    @property
    def pages_discovered(self) -> int:
        return self.manifest.pages_discovered

    @property
    def pages_extracted(self) -> int:
        return self.manifest.pages_extracted

    @property
    def pages_failed(self) -> int:
        return sum(1 for p in self.manifest.pages if p.status == FAILED)

    @property
    def platform_detected(self) -> str | None:
        return self.manifest.platform_detected

    @property
    def discovery_status(self) -> str:
        return self.manifest.discovery_status

    @property
    def blocked_on_auth(self) -> bool:
        """A login page was reached but no credentials were set (see ``healix.auth``)."""
        return self.manifest.blocked_on_auth

    def summary(self) -> dict[str, Any]:
        """A plain-dict overview of the run (the ``run_complete`` fields, plus a little more)."""
        return {
            "run_id": self.run_id,
            "discovery_status": self.discovery_status,
            "pages_discovered": self.pages_discovered,
            "pages_extracted": self.pages_extracted,
            "pages_failed": self.pages_failed,
            "platform_detected": self.platform_detected,
            "blocked_on_auth": self.blocked_on_auth,
            "manifest_path": str(self.manifest_path),
        }


def _load_manifest(path: Path) -> Manifest:
    try:
        return Manifest.load(path)
    except FileNotFoundError:
        raise
    except (ValueError, KeyError) as exc:
        raise ValueError(f"could not read the manifest at {path}: {exc!r}") from exc


class _Runner:
    """What ``Crawler`` and ``Extractor`` share: config, event sinks, and the browser."""

    def __init__(
        self,
        config: ConfigLike | None,
        *,
        require_start: bool,
        on_event: EventCallback | None,
        webhook_url: str | None,
        webhook_secret: str | None,
        webhook_outbox: str | os.PathLike[str] | None,
        driver: Driver | None,
        headless: bool,
        credentials: Credentials | None,
        auto_login: bool,
        fingerprint_store: FingerprintStore | str | os.PathLike[str] | None,
        fingerprint_mode: str,
    ) -> None:
        self.config = RunConfig.coerce(config, require_start=require_start)
        self.on_event = on_event
        self.webhook_url = validate_webhook_url(webhook_url) if webhook_url else None
        self.webhook_secret = webhook_secret or os.environ.get(WEBHOOK_SECRET_ENV) or None
        self.webhook_outbox = validate_outbox(self.webhook_url, webhook_outbox)
        self._driver = driver
        self.headless = headless
        self.credentials = credentials
        self.auto_login = auto_login
        self._fingerprint_store = fingerprint_store
        self.fingerprint_mode = fingerprint_mode
        self._active_store: FingerprintStore | None = None

    @contextlib.contextmanager
    def _session(self, run_id: str) -> Iterator[tuple[Driver, EventEmitter]]:
        """A started driver and an emitter for ``run_id``; everything is released on exit."""
        with contextlib.ExitStack() as stack:
            sender = None
            if self.webhook_url:
                sender = open_sender(
                    self.webhook_url, secret=self.webhook_secret, outbox=self.webhook_outbox
                )
                stack.callback(sender.close)  # runs after the driver is closed
            driver = self._driver
            if driver is None:
                driver = create_driver(
                    self.config.backend,
                    headless=self.headless,
                    platform_detection=self.config.extraction.platform_detection,
                    quiet_ms=self.config.extraction.settle_quiet_ms,
                )
                driver.start()
                stack.callback(driver.close)
            store = self._fingerprint_store
            if store is not None and not isinstance(store, FingerprintStore):
                store = open_store(store)  # a path or URL: opened here, closed here
                stack.callback(store.close)
            self._active_store = store
            yield driver, EventEmitter(run_id, on_event=self.on_event, sender=sender)

    def _login_handler(
        self, driver: Driver, emitter: EventEmitter, scope: Scope, output_path: Path
    ) -> LoginHandler | None:
        """The login handler for this run, or ``None`` when auto-login is off."""
        if not self.auto_login:
            return None
        return LoginHandler(
            driver,
            self.credentials or Credentials.from_env(),
            in_scope=scope.allows,
            on_login_failed=lambda url, reason, ref: self._emit_login_failed(
                emitter, url, reason, ref
            ),
            screenshots_dir=output_path / "screenshots",
        )

    @staticmethod
    def _emit_login_failed(
        emitter: EventEmitter, url: str, reason: str, screenshot_ref: str | None
    ) -> None:
        emitter.emit(LOGIN_FAILED, url=url, reason=reason, screenshot_ref=screenshot_ref)

    @staticmethod
    def _emit_discovered(emitter: EventEmitter, page: ManifestPage) -> None:
        emitter.emit(
            PAGE_DISCOVERED,
            url=page.url,
            page_type=page.page_type,
            structural_hash=page.structural_hash,
        )

    @staticmethod
    def _emit_extracted(emitter: EventEmitter, page: ExtractedPage) -> None:
        emitter.emit(
            PAGE_EXTRACTED,
            url=page.url,
            page_type=page.page_type,
            element_count=page.element_count,
            output_file=page.output_file,
        )

    @staticmethod
    def _emit_complete(emitter: EventEmitter, manifest: Manifest, manifest_path: Path) -> None:
        emitter.emit(
            RUN_COMPLETE,
            pages_discovered=manifest.pages_discovered,
            pages_extracted=manifest.pages_extracted,
            platform_detected=manifest.platform_detected,
            manifest_path=str(manifest_path),
        )


class Crawler(_Runner):
    """Discover every reachable page and (optionally) extract each one.

    ``run_id`` names the run. Re-running with the same ``run_id`` over the same output
    directory *resumes* it: finished discovery is reused, and extraction continues from
    the first page that is not extracted. Without a ``run_id`` a new one is generated — and
    if the output directory already holds a run, that is an error (``RunConflictError``)
    rather than a silent overwrite. After a call, ``run_id`` holds the id that was used.
    """

    def __init__(
        self,
        config: ConfigLike,
        *,
        run_id: str | None = None,
        on_event: EventCallback | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        webhook_outbox: str | os.PathLike[str] | None = None,
        driver: Driver | None = None,
        headless: bool = True,
        credentials: Credentials | None = None,
        auto_login: bool = True,
        fingerprint_store: FingerprintStore | str | os.PathLike[str] | None = None,
        fingerprint_mode: str = KEEP,
    ) -> None:
        super().__init__(
            config,
            require_start=True,
            on_event=on_event,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            webhook_outbox=webhook_outbox,
            driver=driver,
            headless=headless,
            credentials=credentials,
            auto_login=auto_login,
            fingerprint_store=fingerprint_store,
            fingerprint_mode=fingerprint_mode,
        )
        self.run_id = run_id

    def discover(self) -> Run:
        """Discovery only: find the pages and write the manifest; extract nothing."""
        return self._run(extract=False)

    def discover_and_extract(self) -> Run:
        """Discover every page, then extract each one to JSON."""
        return self._run(extract=True)

    def _run(self, *, extract: bool) -> Run:
        config = self.config
        out = Path(config.extraction.output_path)
        manifest_path = out / MANIFEST_FILENAME
        run_id, existing = self._resolve_run(manifest_path)
        self.run_id = run_id

        with self._session(run_id) as (driver, emitter):
            scope = Scope(
                config.discovery.domain_scope, [normalize_url(u) for u in config.start_urls]
            )
            login = self._login_handler(driver, emitter, scope, out)
            reusable = (
                existing is not None
                and existing.discovery_status in (COMPLETE, MAX_PAGES_REACHED)
                and not existing.blocked_on_auth  # what is behind the login was never discovered
            )
            if reusable and existing is not None:
                logger.info(
                    "resuming run; discovery already finished",
                    run_id=run_id,
                    pages_discovered=existing.pages_discovered,
                    pages_extracted=existing.pages_extracted,
                )
                manifest = existing
            else:
                if existing is not None and existing.blocked_on_auth:
                    logger.info("the previous attempt was blocked on auth; discovering again")
                manifest = DiscoveryCrawler(
                    driver,
                    config.discovery,
                    on_page_discovered=lambda page: self._emit_discovered(emitter, page),
                    authenticator=login,
                ).discover(config.start_urls, run_id=run_id, manifest_path=manifest_path)

            if extract:
                manifest = ElementExtractor(
                    driver,
                    config.extraction,
                    on_page_extracted=lambda page: self._emit_extracted(emitter, page),
                    authenticator=login,
                    fingerprint_store=self._active_store,
                    fingerprint_mode=self.fingerprint_mode,
                ).extract(manifest, manifest_path=manifest_path)

            self._emit_complete(emitter, manifest, manifest_path)
        return Run(run_id, manifest, manifest_path, out)

    def _resolve_run(self, manifest_path: Path) -> tuple[str, Manifest | None]:
        if not manifest_path.exists():
            return self.run_id or uuid.uuid4().hex[:12], None
        existing = _load_manifest(manifest_path)
        if self.run_id is None:
            raise RunConflictError(
                f"{manifest_path} already holds run {existing.run_id!r}. Pass run_id="
                f"{existing.run_id!r} to resume it, or use a different output path for a new run."
            )
        if self.run_id != existing.run_id:
            raise RunConflictError(
                f"{manifest_path} holds run {existing.run_id!r}, not {self.run_id!r}. "
                "Use a different output path for a new run."
            )
        return existing.run_id, existing


class Extractor(_Runner):
    """Extract the pages of an existing manifest to JSON (resumably).

    ``extract`` takes a ``Manifest`` or a path to one. Given a path and no explicit
    ``config``, output goes next to the manifest; with a ``config``, to its
    ``crawl.extraction.output_path``. Progress is saved back to the manifest after every
    page, and only pages that are not yet extracted are processed.
    """

    def __init__(
        self,
        config: ConfigLike | None = None,
        *,
        on_event: EventCallback | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        webhook_outbox: str | os.PathLike[str] | None = None,
        driver: Driver | None = None,
        headless: bool = True,
        credentials: Credentials | None = None,
        auto_login: bool = True,
        fingerprint_store: FingerprintStore | str | os.PathLike[str] | None = None,
        fingerprint_mode: str = KEEP,
    ) -> None:
        super().__init__(
            config,
            require_start=False,
            on_event=on_event,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            webhook_outbox=webhook_outbox,
            driver=driver,
            headless=headless,
            credentials=credentials,
            auto_login=auto_login,
            fingerprint_store=fingerprint_store,
            fingerprint_mode=fingerprint_mode,
        )
        self._explicit_config = config is not None

    def extract(self, manifest: Manifest | str | os.PathLike[str]) -> Run:
        extraction = self.config.extraction
        if isinstance(manifest, Manifest):
            loaded = manifest
            manifest_path = Path(extraction.output_path) / MANIFEST_FILENAME
        else:
            manifest_path = Path(manifest)
            loaded = _load_manifest(manifest_path)
            if not self._explicit_config:
                extraction = replace(extraction, output_path=str(manifest_path.parent))

        with self._session(loaded.run_id) as (driver, emitter):
            # With no start URLs on record (a hand-built manifest), the pages define the scope.
            origins = loaded.start_urls or [p.url for p in loaded.pages]
            scope = Scope(self.config.discovery.domain_scope, [normalize_url(u) for u in origins])
            login = self._login_handler(driver, emitter, scope, Path(extraction.output_path))
            result = ElementExtractor(
                driver,
                extraction,
                on_page_extracted=lambda page: self._emit_extracted(emitter, page),
                authenticator=login,
                fingerprint_store=self._active_store,
                fingerprint_mode=self.fingerprint_mode,
            ).extract(loaded, manifest_path=manifest_path)
            self._emit_complete(emitter, result, manifest_path)
        return Run(result.run_id, result, manifest_path, Path(extraction.output_path))
