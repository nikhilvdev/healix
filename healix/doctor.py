"""``healix doctor``: is this machine ready to run Healix?

::

    healix doctor            # check what is installed; launches nothing
    healix doctor --launch   # also open each ready browser, load a page and read its elements

The checks are the core dependencies, each browser backend (its Python package and the browser it
drives), the optional Postgres driver, and whether login credentials are set (never their values).
A backend that is not installed is reported, not failed: Healix needs at least one usable backend,
and the exit status is ``0`` exactly when it has one.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from healix import __version__
from healix.auth import PASSWORD_ENV, USERNAME_ENV
from healix.driver.factory import BACKENDS, create_driver, diagnose_backend

OK, FAIL, MISSING, INFO = "ok", "fail", "missing", "info"

_CORE = ("logquill", "python-dotenv", "jinja2")
_SMOKE_PAGE = "data:text/html,<title>healix</title><input name=q><button>go</button>"
_MIN_PYTHON = (3, 10)


@dataclass(frozen=True)
class Check:
    """One line of the report."""

    name: str
    status: str  # ok | fail | missing | info
    detail: str
    hint: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[Check, ...]
    ready_backends: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Healix can run: nothing it needs is broken, and at least one backend works."""
        return bool(self.ready_backends) and not any(
            c.status == FAIL and c.name in _CORE + ("python",) for c in self.checks
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "healix": __version__,
            "ok": self.ok,
            "ready_backends": list(self.ready_backends),
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "hint": c.hint}
                for c in self.checks
            ],
        }


def _check_python() -> Check:
    found = ".".join(str(n) for n in sys.version_info[:3])
    if sys.version_info[:2] < _MIN_PYTHON:
        need = ".".join(str(n) for n in _MIN_PYTHON)
        return Check("python", FAIL, f"{found}, but Healix needs {need} or newer")
    return Check("python", OK, found)


def _check_core() -> list[Check]:
    checks = []
    for package in _CORE:
        try:
            checks.append(Check(package, OK, version(package)))
        except PackageNotFoundError:
            checks.append(Check(package, FAIL, "not installed", f"pip install {package}"))
    return checks


def _check_backend(backend: str, *, launch: bool) -> tuple[list[Check], bool]:
    report = diagnose_backend(backend)
    if not report.installed:
        return [Check(backend, MISSING, report.problem or "not installed", report.hint)], False
    if not report.ready:
        detail = f"{backend} {report.version}: {report.problem}" if report.version else ""
        return [Check(backend, FAIL, detail or str(report.problem), report.hint)], False
    detail = f"{report.version}, browser at {report.browser}"
    checks = [Check(backend, OK, detail)]
    if launch:
        checks.append(_launch(backend))
        return checks, checks[-1].status == OK
    return checks, True


def _launch(backend: str) -> Check:
    """Really open the browser, load a page, and read its elements."""
    name = f"{backend} launch"
    started = time.monotonic()
    try:
        with create_driver(backend, headless=True) as driver:
            driver.navigate(_SMOKE_PAGE)
            elements = driver.get_elements()
    except Exception as exc:
        message = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
        return Check(name, FAIL, message, "run healix doctor again with --log-level debug")
    if not elements:
        return Check(name, FAIL, "the browser loaded a test page but read no elements")
    took = time.monotonic() - started
    return Check(name, OK, f"opened a page and read {len(elements)} elements in {took:.1f}s")


def _check_postgres() -> Check:
    try:
        return Check("postgres", OK, f"psycopg {version('psycopg')} (optional)")
    except PackageNotFoundError:
        return Check(
            "postgres",
            MISSING,
            "psycopg is not installed (only needed to keep fingerprints in PostgreSQL)",
            "pip install 'healix[postgres]'",
        )


def _check_credentials(environ: Mapping[str, str]) -> Check:
    have = [name for name in (USERNAME_ENV, PASSWORD_ENV) if environ.get(name)]
    if len(have) == 2:
        return Check("credentials", OK, f"{USERNAME_ENV} and {PASSWORD_ENV} are set")
    return Check(
        "credentials",
        INFO,
        "login credentials are not set; a run that reaches a login page will stop there",
        f"set {USERNAME_ENV} and {PASSWORD_ENV} in the environment or a .env file",
    )


def run_doctor(*, launch: bool = False, environ: Mapping[str, str] | None = None) -> DoctorReport:
    """Run every check. ``launch`` also opens each ready browser (slower, but proves it works)."""
    checks = [_check_python(), *_check_core()]
    ready: list[str] = []
    for backend in BACKENDS:
        backend_checks, is_ready = _check_backend(backend, launch=launch)
        checks.extend(backend_checks)
        if is_ready:
            ready.append(backend)
    checks.append(_check_postgres())
    checks.append(_check_credentials(os.environ if environ is None else environ))
    return DoctorReport(tuple(checks), tuple(ready))


_MARKS = {OK: "ok", FAIL: "FAIL", MISSING: "--", INFO: "info"}


def format_report(report: DoctorReport) -> str:
    """The report as text for a terminal."""
    lines = [f"healix {__version__} doctor", ""]
    width = max(len(c.name) for c in report.checks)
    for check in report.checks:
        lines.append(f"  {_MARKS[check.status]:<4}  {check.name:<{width}}  {check.detail}")
        if check.hint and check.status != OK:
            lines.append(f"  {'':<4}  {'':<{width}}  -> {check.hint}")
    lines.append("")
    if report.ok:
        lines.append(f"Ready. Usable backends: {', '.join(report.ready_backends)}.")
    elif report.ready_backends:
        lines.append("Not ready: fix the FAIL lines above.")
    else:
        lines.append("Not ready: no browser backend is usable. Fix one of the lines above.")
    return "\n".join(lines)
