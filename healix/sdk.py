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

A run config with ``roles`` is crawled once per role, each with its own credentials and its own
fresh browser, and the results compared: use ``RoleCrawler`` for that (``Crawler`` refuses such a
config, so a single-credential caller is never handed a different kind of result).

The SDK does not read ``.env`` for you: call ``dotenv.load_dotenv()`` first if you keep the
login credentials or ``HEALIX_WEBHOOK_SECRET`` there. (The CLI does.)

Events describe the work done by *this* call: resuming a run whose discovery already
finished does not re-emit ``page_discovered`` for those pages.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from healix.auth import ANONYMOUS, Credentials, LoginHandler, credential_env_names
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
    ROLES_COMPARED,
    RUN_COMPLETE,
    EventCallback,
    EventEmitter,
    open_sender,
    validate_outbox,
    validate_webhook_url,
)
from healix.extraction import ElementExtractor, ExtractedPage
from healix.extraction.element_extractor import MANIFEST_FILENAME
from healix.healing import KEEP, REFRESH, FingerprintStore, open_store
from healix.log import get_logger
from healix.rolediff import RoleDiff, RoleOutput, build_role_diff, write_role_diff

logger = get_logger(__name__)

WEBHOOK_SECRET_ENV = "HEALIX_WEBHOOK_SECRET"

ConfigLike = RunConfig | dict[str, Any] | str | os.PathLike[str]


class RunConflictError(ValueError):
    """The output directory already holds a different run than the one requested."""


class RoleCredentialsError(ValueError):
    """A role has no credentials to log in with."""


CredentialsLike = Credentials | Mapping[str, Credentials]


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
        summary: dict[str, Any] = {
            "run_id": self.run_id,
            "discovery_status": self.discovery_status,
            "pages_discovered": self.pages_discovered,
            "pages_extracted": self.pages_extracted,
            "pages_failed": self.pages_failed,
            "platform_detected": self.platform_detected,
            "blocked_on_auth": self.blocked_on_auth,
            "manifest_path": str(self.manifest_path),
        }
        if self.manifest.role:
            summary["role"] = self.manifest.role
        return summary


@dataclass
class MultiRoleRun:
    """The outcome of a ``RoleCrawler`` call: one ``Run`` per role, and how they compare.

    ``diff`` is ``None`` when fewer than two roles have a result to compare. ``diff_path`` is the
    ``roles-diff.json`` written next to the per-role folders.
    """

    run_id: str
    runs: dict[str, Run]
    output_path: Path
    diff: RoleDiff | None = None
    diff_path: Path | None = None

    @property
    def roles(self) -> list[str]:
        return list(self.runs)

    @property
    def pages_failed(self) -> int:
        return sum(run.pages_failed for run in self.runs.values())

    @property
    def blocked_on_auth(self) -> bool:
        """Some role reached a login page it could not get past."""
        return any(run.blocked_on_auth for run in self.runs.values())

    def summary(self) -> dict[str, Any]:
        """A plain-dict overview: each role's summary, and the size of the difference."""
        return {
            "run_id": self.run_id,
            "roles": {role: run.summary() for role, run in self.runs.items()},
            "pages_failed": self.pages_failed,
            "blocked_on_auth": self.blocked_on_auth,
            "differences": self.diff.differences if self.diff else None,
            "diff_path": str(self.diff_path) if self.diff_path else None,
        }


def _load_manifest(path: Path) -> Manifest:
    try:
        return Manifest.load(path)
    except FileNotFoundError:
        raise
    except (ValueError, KeyError) as exc:
        raise ValueError(f"could not read the manifest at {path}: {exc!r}") from exc


class _Runner:
    """What ``Crawler``, ``RoleCrawler`` and ``Extractor`` share: config, events, the browser."""

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
        credentials: CredentialsLike | None,
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
        self.run_id: str | None = None

    @contextlib.contextmanager
    def _events(self, run_id: str, role: str | None = None) -> Iterator[EventEmitter]:
        """An emitter for ``run_id`` (stamping ``role``, if any); the webhook is closed on exit."""
        with contextlib.ExitStack() as stack:
            sender = None
            if self.webhook_url:
                sender = open_sender(
                    self.webhook_url, secret=self.webhook_secret, outbox=self.webhook_outbox
                )
                stack.callback(sender.close)
            yield EventEmitter(run_id, role=role, on_event=self.on_event, sender=sender)

    @contextlib.contextmanager
    def _session(
        self, run_id: str, role: str | None = None
    ) -> Iterator[tuple[Driver, EventEmitter]]:
        """A started driver and an emitter for ``run_id``; everything is released on exit.

        A driver the runner starts is always a fresh browser, which is what keeps one role's
        login from being carried into the next role's crawl.
        """
        with self._events(run_id, role) as emitter, contextlib.ExitStack() as stack:
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
            yield driver, emitter

    def _credentials_for(self, role: str | None) -> Credentials | None:
        """The credentials to log in with: the caller's, else the environment's."""
        if role == ANONYMOUS:
            return None
        if isinstance(self.credentials, Mapping):
            given = self.credentials.get(role) if role else None
            return given or Credentials.from_env(role=role)
        return self.credentials or Credentials.from_env(role=role)

    def _login_handler(
        self,
        driver: Driver,
        emitter: EventEmitter,
        scope: Scope,
        output_path: Path,
        role: str | None = None,
    ) -> LoginHandler | None:
        """The login handler for this run (or role), or ``None`` when it should not log in."""
        if not self.auto_login or role == ANONYMOUS:
            return None
        return LoginHandler(
            driver,
            self._credentials_for(role),
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

    def _crawl_one(self, config: RunConfig, role: str | None, *, extract: bool) -> Run:
        """Discover (and optionally extract) into ``config``'s output folder, as ``role``."""
        out = Path(config.extraction.output_path)
        manifest_path = out / MANIFEST_FILENAME
        run_id, existing = self._resolve_run(manifest_path)
        self.run_id = run_id

        with self._session(run_id, role) as (driver, emitter):
            scope = Scope(
                config.discovery.domain_scope, [normalize_url(u) for u in config.start_urls]
            )
            login = self._login_handler(driver, emitter, scope, out, role)
            reusable = (
                existing is not None
                and existing.discovery_status in (COMPLETE, MAX_PAGES_REACHED)
                and not existing.blocked_on_auth  # what is behind the login was never discovered
            )
            if reusable and existing is not None:
                logger.info(
                    "resuming run; discovery already finished",
                    run_id=run_id,
                    role=role,
                    pages_discovered=existing.pages_discovered,
                    pages_extracted=existing.pages_extracted,
                )
                manifest = existing
                manifest.role = role
            else:
                if existing is not None and existing.blocked_on_auth:
                    logger.info("the previous attempt was blocked on auth; discovering again")
                manifest = DiscoveryCrawler(
                    driver,
                    config.discovery,
                    on_page_discovered=lambda page: self._emit_discovered(emitter, page),
                    authenticator=login,
                ).discover(config.start_urls, run_id=run_id, manifest_path=manifest_path, role=role)

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


class Crawler(_Runner):
    """Discover every reachable page and (optionally) extract each one.

    ``run_id`` names the run. Re-running with the same ``run_id`` over the same output
    directory *resumes* it: finished discovery is reused, and extraction continues from
    the first page that is not extracted. Without a ``run_id`` a new one is generated — and
    if the output directory already holds a run, that is an error (``RunConflictError``)
    rather than a silent overwrite. After a call, ``run_id`` holds the id that was used.

    A config with ``roles`` is not for this class; see ``RoleCrawler``.
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
        if self.config.roles:
            raise ValueError(
                "this run config has roles, which need RoleCrawler (the CLI picks it for you): "
                "Crawler is one credential, one result"
            )
        self.run_id = run_id

    def discover(self) -> Run:
        """Discovery only: find the pages and write the manifest; extract nothing."""
        return self._crawl_one(self.config, None, extract=False)

    def discover_and_extract(self) -> Run:
        """Discover every page, then extract each one to JSON."""
        return self._crawl_one(self.config, None, extract=True)


class RoleCrawler(_Runner):
    """Crawl the site once per role in the run config's ``roles``, and compare what each reached.

    Each role logs in with its own credentials (``WEBLIB_LOGIN_USERNAME_<ROLE>`` and
    ``WEBLIB_LOGIN_PASSWORD_<ROLE>``, or a ``credentials`` mapping of role name to ``Credentials``)
    in a browser of its own, so one role's session never reaches another's. ``anonymous`` is a
    reserved role that never logs in. Roles run one after another, in the order listed.

    Output goes to ``<output_path>/roles/<role>/`` (a manifest and page files each), and
    ``roles-diff.json`` in ``<output_path>`` says which pages and elements each role can reach
    (``healix.rolediff``). Every event carries the ``role`` it happened under, and a
    ``roles_compared`` event announces the diff.

    ``only`` runs just those roles (to retry one); the diff still covers every role that has a
    result on disk. ``run_id`` works as for ``Crawler``, shared by all roles, and re-running with it
    resumes each role where it stopped. A caller-supplied ``driver`` is refused, and so is
    ``fingerprint_mode="refresh"``: the first would let the roles share a session, the second
    would let whichever role ran last overwrite the others' fingerprints.
    """

    def __init__(
        self,
        config: ConfigLike,
        *,
        run_id: str | None = None,
        only: Sequence[str] | None = None,
        on_event: EventCallback | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        webhook_outbox: str | os.PathLike[str] | None = None,
        headless: bool = True,
        credentials: Mapping[str, Credentials] | None = None,
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
            driver=None,
            headless=headless,
            credentials=credentials,
            auto_login=auto_login,
            fingerprint_store=fingerprint_store,
            fingerprint_mode=fingerprint_mode,
        )
        if not self.config.roles:
            raise ValueError('this run config has no roles: add "roles": ["admin", "standard"]')
        if fingerprint_mode == REFRESH:
            raise ValueError(
                "fingerprint_mode='refresh' would let the last role overwrite the others' "
                "fingerprints; use 'keep' (each element is recorded by the first role to see it)"
            )
        chosen = tuple(only) if only is not None else self.config.roles
        if unknown := [r for r in chosen if r not in self.config.roles]:
            raise ValueError(f"roles {unknown} are not in the run config's roles")
        self.only = tuple(r for r in self.config.roles if r in chosen)  # config order
        self.run_id = run_id

    def discover(self) -> MultiRoleRun:
        """Discovery only, for each role."""
        return self._run(extract=False)

    def discover_and_extract(self) -> MultiRoleRun:
        """Discover and extract, for each role, then compare them."""
        return self._run(extract=True)

    def _run(self, *, extract: bool) -> MultiRoleRun:
        config = self.config
        root = Path(config.extraction.output_path)
        self._check_credentials(self.only)
        self.run_id = self._resolve_roles_run_id(root)

        runs: dict[str, Run] = {}
        for role in self.only:
            role_out = root / "roles" / role
            role_config = replace(
                config, extraction=replace(config.extraction, output_path=str(role_out))
            )
            logger.info("crawling as a role", run_id=self.run_id, role=role)
            runs[role] = self._crawl_one(role_config, role, extract=extract)

        diff, diff_path = self._compare(root, self.run_id)
        return MultiRoleRun(self.run_id, runs, root, diff, diff_path)

    def _check_credentials(self, roles: Sequence[str]) -> None:
        """Fail before any browser starts if a role has nothing to log in with."""
        if not self.auto_login:
            return
        missing = [r for r in roles if r != ANONYMOUS and self._credentials_for(r) is None]
        if missing:
            wanted = "; ".join(
                f"{role}: {' and '.join(credential_env_names(role))}" for role in missing
            )
            raise RoleCredentialsError(
                f"no credentials for role(s) {missing}. Set, in the environment or a .env file: "
                f"{wanted}"
            )

    def _resolve_roles_run_id(self, root: Path) -> str:
        """The one run id every role shares; an existing run must be resumed on purpose."""
        found: dict[str, str] = {}
        for role in self.config.roles:
            path = root / "roles" / role / MANIFEST_FILENAME
            if path.exists():
                found[role] = _load_manifest(path).run_id
        if not found:
            return self.run_id or uuid.uuid4().hex[:12]
        if len(set(found.values())) > 1:
            raise RunConflictError(
                f"the roles under {root / 'roles'} hold different runs ({found}); "
                "use a different output path for a new run."
            )
        existing = next(iter(found.values()))
        if self.run_id is None:
            raise RunConflictError(
                f"{root / 'roles'} already holds run {existing!r}. Pass run_id={existing!r} to "
                "resume it, or use a different output path for a new run."
            )
        if self.run_id != existing:
            raise RunConflictError(
                f"{root / 'roles'} holds run {existing!r}, not {self.run_id!r}. "
                "Use a different output path for a new run."
            )
        return existing

    def _compare(self, root: Path, run_id: str) -> tuple[RoleDiff | None, Path | None]:
        """Compare every configured role that has a result on disk, and announce it."""
        outputs: dict[str, RoleOutput] = {}
        for role in self.config.roles:
            directory = root / "roles" / role
            if (directory / MANIFEST_FILENAME).exists():
                outputs[role] = RoleOutput(_load_manifest(directory / MANIFEST_FILENAME), directory)
        if len(outputs) < 2:
            return None, None
        diff = build_role_diff(outputs, run_id=run_id)
        diff_path = write_role_diff(diff, root)
        logger.info(
            "roles compared",
            run_id=run_id,
            roles=diff.roles,
            page_differences=len(diff.page_differences),
            element_differences=len(diff.element_differences),
        )
        with self._events(run_id) as emitter:
            emitter.emit(
                ROLES_COMPARED,
                roles=diff.roles,
                diff_path=str(diff_path),
                differences=diff.differences,
            )
        return diff, diff_path


class Extractor(_Runner):
    """Extract the pages of an existing manifest to JSON (resumably).

    ``extract`` takes a ``Manifest`` or a path to one. Given a path and no explicit
    ``config``, output goes next to the manifest; with a ``config``, to its
    ``crawl.extraction.output_path``. Progress is saved back to the manifest after every
    page, and only pages that are not yet extracted are processed.

    ``role`` says which user role's credentials to log in with. It defaults to the role the
    manifest was crawled as (a manifest under ``roles/<role>/`` knows its own), so finishing one
    role of a multi-role run is just ``Extractor().extract("output/roles/admin/manifest.json")``.
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
        credentials: CredentialsLike | None = None,
        auto_login: bool = True,
        fingerprint_store: FingerprintStore | str | os.PathLike[str] | None = None,
        fingerprint_mode: str = KEEP,
        role: str | None = None,
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
        self.role = role

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

        role = self.role or loaded.role
        with self._session(loaded.run_id, role) as (driver, emitter):
            # With no start URLs on record (a hand-built manifest), the pages define the scope.
            origins = loaded.start_urls or [p.url for p in loaded.pages]
            scope = Scope(self.config.discovery.domain_scope, [normalize_url(u) for u in origins])
            login = self._login_handler(driver, emitter, scope, Path(extraction.output_path), role)
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
