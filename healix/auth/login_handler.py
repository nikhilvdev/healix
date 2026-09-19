"""Automatic login for pages classified ``login``.

There is no login configuration step: when discovery or extraction lands on a page the
classifier calls ``login``, ``LoginHandler.handle_page`` fills and submits it with the
credentials from the environment (``WEBLIB_LOGIN_USERNAME`` / ``WEBLIB_LOGIN_PASSWORD``),
then the crawl carries on. One credential per run.

What it handles
    * a username + password form (optionally in an iframe or shadow root);
    * a "Sign in with SSO" button that redirects to an identity provider and back
      (redirect or POST-back), including identifier-first providers that ask for the
      username and the password on separate screens;
    * **session expiry**: a page that bounces back to a login page after a successful login
      triggers a fresh login, and the crawl resumes.

What it will not do — all of it fails cleanly, never hangs
    * **MFA / one-time codes** cannot be completed automatically: the attempt is aborted with
      ``login_failed(mfa_required)``.
    * **Submit credentials twice in one attempt.** If the site sends us back to the same form,
      that is ``auth_rejected`` — retrying would risk locking the account.
    * **Retry after a failure.** Any failure ends login for the rest of the run; pages that
      need it are recorded as failed.
    * **Loop on expiring sessions.** ``max_logins`` bounds initial login plus re-logins.
    * **Type credentials into an untrusted page.** Credentials go only to in-scope pages, or to
      pages whose URL looks like an OAuth/SSO endpoint. Anywhere else is refused.
    * CAPTCHAs, passkeys/WebAuthn, and hardware keys are not supported; they end as
      ``timeout`` or ``auth_rejected``.

With no credentials set, a login page marks the run **blocked on auth** (``blocked``) instead.

The credentials are never logged, never put in an event, and never written to disk. URLs in
logs and events are stripped of their query string, since SSO redirects carry ``state`` and
``code`` parameters.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from healix.auth.forms import LoginForm, detect_mfa, find_login_form, login_error_visible
from healix.classification import classify, is_oauth_url
from healix.discovery.manifest import normalize_url
from healix.driver.base import Driver, Element
from healix.log import get_logger

logger = get_logger(__name__)

LOGIN_PAGE_TYPE = "login"

USERNAME_ENV = "WEBLIB_LOGIN_USERNAME"
PASSWORD_ENV = "WEBLIB_LOGIN_PASSWORD"

# Outcomes of ``handle_page``.
NOT_NEEDED = "not_needed"
AUTHENTICATED = "authenticated"
FAILED = "failed"
BLOCKED = "blocked"
SKIPPED = "skipped"

# Failure reasons — exactly the ``login_failed`` event's documented reasons.
MFA_REQUIRED = "mfa_required"
TIMEOUT = "timeout"
SELECTOR_NOT_FOUND = "selector_not_found"
AUTH_REJECTED = "auth_rejected"

_MFA, _DONE, _LOGIN, _PENDING = "mfa", "done", "login", "pending"


@dataclass(frozen=True)
class Credentials:
    """One username/password pair. Its ``repr`` never shows either value."""

    username: str
    password: str

    def __repr__(self) -> str:
        return "Credentials(username='***', password='***')"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Credentials | None:
        """Credentials from ``WEBLIB_LOGIN_USERNAME``/``WEBLIB_LOGIN_PASSWORD``, or ``None``."""
        env = os.environ if environ is None else environ
        username, password = env.get(USERNAME_ENV), env.get(PASSWORD_ENV)
        if username and password:
            return cls(username, password)
        return None


@dataclass(frozen=True)
class LoginResult:
    """What ``handle_page`` did.

    ``status`` is one of ``not_needed``, ``authenticated`` (the driver is now at
    ``landing_url``), ``failed`` (``reason`` says why; a ``login_failed`` event was
    raised), ``blocked`` (no credentials), or ``skipped`` (login was already given up on).
    """

    status: str
    reason: str | None = None
    landing_url: str | None = None
    screenshot_ref: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.status == AUTHENTICATED


class Authenticator(Protocol):
    """What discovery and extraction need from a login handler."""

    @property
    def blocked(self) -> bool: ...

    def handle_page(
        self,
        requested_url: str,
        final_url: str,
        elements: Sequence[Element],
        page_type: str,
    ) -> LoginResult: ...

    def report_session_invalid(self, url: str) -> None: ...


LoginFailedCallback = Callable[[str, str, str | None], None]


def redact_url(url: str) -> str:
    """``url`` without userinfo, query, or fragment — safe to log or put in an event."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def _same_page(a: str, b: str) -> bool:
    try:
        return normalize_url(a) == normalize_url(b)
    except ValueError:
        return a == b


class LoginHandler:
    """Log in when a crawl lands on a login page. See the module docstring."""

    def __init__(
        self,
        driver: Driver,
        credentials: Credentials | None,
        *,
        in_scope: Callable[[str], bool],
        on_login_failed: LoginFailedCallback | None = None,
        screenshots_dir: str | os.PathLike[str] | None = None,
        timeout: float = 30.0,
        max_steps: int = 6,
        max_logins: int = 3,
        poll_interval: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.driver = driver
        self.credentials = credentials
        self.in_scope = in_scope
        self.on_login_failed = on_login_failed
        self.screenshots_dir = Path(screenshots_dir) if screenshots_dir else None
        self.timeout = timeout
        self.max_steps = max_steps
        self.max_logins = max_logins
        self.poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock

        self.authenticated = False
        self.blocked = False
        self.gave_up = False
        self.logins = 0
        self._failures = 0
        self._last_elements: Sequence[Element] = ()

    # -- the hook the crawlers call ------------------------------------------------ #

    def handle_page(
        self,
        requested_url: str,
        final_url: str,
        elements: Sequence[Element],
        page_type: str,
    ) -> LoginResult:
        """Log in if this page is a login page that needs it; otherwise do nothing.

        Call after loading ``requested_url`` (which may have redirected to ``final_url``) and
        classifying it. A *bounce* — the requested page redirected to a login page — always
        needs a login (that is session expiry, once we have authenticated before). A login
        page requested directly needs one only if we have not authenticated yet.
        """
        if page_type != LOGIN_PAGE_TYPE:
            return LoginResult(NOT_NEEDED)
        bounced = not _same_page(requested_url, final_url)
        if self.authenticated and not bounced:
            return LoginResult(NOT_NEEDED)
        if self.gave_up:
            return LoginResult(SKIPPED)
        if self.credentials is None:
            if not self.blocked:
                logger.warn(
                    "login page reached but no credentials are set; the run is blocked on auth",
                    url=redact_url(final_url),
                    set_env=[USERNAME_ENV, PASSWORD_ENV],
                )
            self.blocked = True
            return LoginResult(BLOCKED)
        if self.logins >= self.max_logins:
            return self._fail(
                final_url,
                AUTH_REJECTED,
                detail=f"the session ended again after {self.logins} logins; not trying again",
            )
        return self._login(final_url, elements)

    def report_session_invalid(self, url: str) -> None:
        """The crawler logged in but the session did not hold (the page still wants a login)."""
        self.authenticated = False
        if not self.gave_up:
            self._fail(url, AUTH_REJECTED, detail="the session did not persist after logging in")

    # -- one login ----------------------------------------------------------------- #

    def _login(self, url: str, elements: Sequence[Element]) -> LoginResult:
        self.logins += 1
        logger.info("login started", url=redact_url(url), attempt=self.logins)
        reason = self._flow(url, elements)
        if reason is not None:
            return self._fail(self._safe_current_url(url), reason)
        self.authenticated = True
        landing = self._safe_current_url(url)
        logger.info("login succeeded", landing=redact_url(landing), attempt=self.logins)
        return LoginResult(AUTHENTICATED, landing_url=landing)

    def _flow(self, url: str, elements: Sequence[Element]) -> str | None:
        """Drive the login to the end. ``None`` on success, else the failure reason."""
        deadline = self._clock() + self.timeout
        done: set[str] = set()  # each kind of step happens at most once per login
        for _ in range(self.max_steps):
            kind = self._assess(url, elements)
            if kind == _MFA:
                return MFA_REQUIRED
            if kind == _DONE:
                return None
            before = (url, self._signature(url, elements))
            if kind == _LOGIN:
                if not self._trusted(url):
                    logger.warn(
                        "refusing to enter credentials on an untrusted page",
                        url=redact_url(url),
                    )
                    return SELECTOR_NOT_FOUND
                form = find_login_form(elements)
                action = self._choose(form)
                if action is None:
                    return SELECTOR_NOT_FOUND
                if action in done:
                    # The site sent us back to the same step: it said no. Do not resubmit.
                    return AUTH_REJECTED
                done.add(action)
                if not self._perform(action, form):
                    return SELECTOR_NOT_FOUND
            observed = self._wait_for_change(before, deadline)
            if observed is None:
                return AUTH_REJECTED if login_error_visible(self._last_elements) else TIMEOUT
            url, elements = observed
        return TIMEOUT

    def _assess(self, url: str, elements: Sequence[Element]) -> str:
        if detect_mfa(elements):
            return _MFA
        if classify(elements, url) == LOGIN_PAGE_TYPE:
            return _LOGIN
        if self.in_scope(url) or not is_oauth_url(url):
            return _DONE
        return _PENDING  # still at an identity provider (consent, POST-back): keep waiting

    @staticmethod
    def _choose(form: LoginForm) -> str | None:
        if form.password is not None and form.submit is not None:
            return "password"
        if form.password is None and form.username is not None and form.submit is not None:
            return "identifier"
        if form.password is None and form.sso:
            return "sso"
        return None

    def _perform(self, action: str, form: LoginForm) -> bool:
        creds = self.credentials
        assert creds is not None  # handle_page() never gets here without credentials
        try:
            if action == "password":
                assert form.password is not None and form.submit is not None
                if form.username is not None:
                    self.driver.write(creds.username, into=form.username)
                self.driver.write(creds.password, into=form.password)
                self.driver.click(form.submit)
            elif action == "identifier":
                assert form.username is not None and form.submit is not None
                self.driver.write(creds.username, into=form.username)
                self.driver.click(form.submit)
            else:
                self.driver.click(form.sso[0])
        except Exception as exc:
            # Only the type: an exception message must never be able to echo a credential.
            logger.warn(
                "could not operate the login form", action=action, error_type=type(exc).__name__
            )
            return False
        logger.debug("login step submitted", action=action)
        return True

    # -- observing the page --------------------------------------------------------- #

    def _observe(self) -> tuple[str, list[Element]] | None:
        try:
            self.driver.settle()
            url = self.driver.current_url
            return url, self.driver.get_elements()
        except Exception as exc:  # mid-navigation: the page is not readable yet
            logger.debug("page not readable yet", error_type=type(exc).__name__)
            return None

    def _wait_for_change(
        self, before: tuple[str, str], deadline: float
    ) -> tuple[str, list[Element]] | None:
        while True:
            observed = self._observe()
            if observed is not None:
                url, elements = observed
                self._last_elements = elements
                if (url, self._signature(url, elements)) != before:
                    return observed
            if self._clock() >= deadline:
                return None
            self._sleep(self.poll_interval)

    @staticmethod
    def _signature(url: str, elements: Sequence[Element]) -> str:
        texts = sorted(
            (e.text_content or "")
            for e in elements
            if e.text_content and e.computed.get("visible", True)
        )
        return hashlib.sha1("\n".join([url, *texts]).encode()).hexdigest()

    def _trusted(self, url: str) -> bool:
        return self.in_scope(url) or is_oauth_url(url)

    def _safe_current_url(self, fallback: str) -> str:
        try:
            return self.driver.current_url
        except Exception:
            return fallback

    # -- failure -------------------------------------------------------------------- #

    def _fail(self, url: str, reason: str, *, detail: str | None = None) -> LoginResult:
        self.gave_up = True
        self._failures += 1
        screenshot_ref = self._screenshot()
        safe_url = redact_url(url)
        logger.warn(
            "login failed",
            url=safe_url,
            reason=reason,
            detail=detail,
            screenshot=screenshot_ref,
        )
        if self.on_login_failed is not None:
            try:
                self.on_login_failed(safe_url, reason, screenshot_ref)
            except Exception as exc:
                logger.warn("login_failed callback raised", error_type=type(exc).__name__)
        return LoginResult(FAILED, reason=reason, screenshot_ref=screenshot_ref)

    def _screenshot(self) -> str | None:
        """Save a screenshot of the failed login; its path relative to the output directory."""
        if self.screenshots_dir is None:
            return None
        try:
            image = self.driver.screenshot()
            self.screenshots_dir.mkdir(parents=True, exist_ok=True)
            name = f"login-failed-{self._failures}.png"
            (self.screenshots_dir / name).write_bytes(image)
            return f"{self.screenshots_dir.name}/{name}"
        except Exception as exc:
            logger.warn("could not save the login screenshot", error_type=type(exc).__name__)
            return None
