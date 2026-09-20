"""Turn extracted pages into runnable, self-healing Playwright or Selenium scripts.

::

    from healix import Crawler, ScriptGenerator

    run = Crawler("run_config.json").discover_and_extract()
    script = ScriptGenerator(run).to_playwright(style="pom")
    print(script.file_path)

One generator, three styles, two backends:

* ``style="pom"`` — a module of page-object classes, one per page, with a method per element and a
  ``session()`` helper that opens a browser.
* ``style="test"`` — a ``pytest`` module, one test per page, asserting that the page loads and that
  every element is found, visible and (for controls) enabled.
* ``style="action"`` — a plain script: for each page, fill the inputs and click the submit button.
  No assertions.

Every step goes through ``healix.Healer``, so a script keeps working when the page's ids and classes
change: it finds the element again and records the fix. That needs the fingerprints recorded at
extraction; pass ``fingerprint_db`` to have them recorded here too.

Generated scripts never contain secrets. Values for a login page's username and password fields are
read from ``WEBLIB_LOGIN_USERNAME`` / ``WEBLIB_LOGIN_PASSWORD``; a fingerprint database given as a
URL (which usually carries a password) is read from ``HEALIX_FINGERPRINT_DB`` instead of being
written into the file. The output is deterministic: the same manifest gives the same script.
"""

from __future__ import annotations

import json
import keyword
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from healix.discovery.manifest import EXTRACTED, Manifest, ManifestPage
from healix.driver.base import Element
from healix.events import (
    SCRIPT_GENERATED,
    EventCallback,
    EventEmitter,
    WebhookSender,
    validate_webhook_url,
)
from healix.fs import write_text_atomic
from healix.healing import KEEP, FingerprintStore, open_store, record_page
from healix.healing.roles import assign_roles, element_kind
from healix.log import get_logger
from healix.sdk import Run

logger = get_logger(__name__)

BACKENDS = ("playwright", "selenium")
STYLES = ("pom", "test", "action")

DEFAULT_MANIFEST_DIR = "./output"
DEFAULT_FINGERPRINT_DB = "healix.db"
DEFAULT_MAX_ELEMENTS = 200

USERNAME_ENV = "WEBLIB_LOGIN_USERNAME"
PASSWORD_ENV = "WEBLIB_LOGIN_PASSWORD"
FINGERPRINT_DB_ENV = "HEALIX_FINGERPRINT_DB"

_FILENAMES = {
    "pom": "pages_{backend}.py",
    "test": "test_healix_{backend}.py",  # pytest only collects test_*.py
    "action": "actions_{backend}.py",
}
_TEMPLATES = {"pom": "pom.j2", "test": "test.j2", "action": "action.j2"}

_ADAPTERS = {"playwright": "PlaywrightDriverAdapter", "selenium": "SeleniumDriverAdapter"}

_FILL_KINDS = frozenset({"textbox", "searchbox"})
_CLICK_KINDS = frozenset({"button", "link", "checkbox", "radio", "tab", "menuitem", "switch"})
_SUBMIT_TEXT = re.compile(
    r"sign[\s_-]*in|log[\s_-]*in|submit|save|search|continue|next|pay|order|register|"
    r"sign[\s_-]*up|create|send|apply|confirm|go$",
    re.IGNORECASE,
)
_SAMPLES = {
    "email": "user@example.com",
    "tel": "5551234567",
    "number": "1",
    "url": "https://example.com",
    "search": "test",
    "date": "2026-01-01",
}
_DEFAULT_SAMPLE = "sample text"


class GenerationError(ValueError):
    """Script generation cannot proceed (bad input, or nothing to generate)."""


@dataclass(frozen=True)
class ElementSpec:
    """One element a script can act on."""

    role: str
    kind: str
    method: str  # the page-object method name, unique within the page
    action: str  # "fill", "click", or "locate" (present, but not driven)
    value: str | None = None  # Python source for the value to type, when ``action == "fill"``
    visible: bool = True
    enabled: bool = True


@dataclass(frozen=True)
class Step:
    """One line of an action script."""

    action: str  # "fill" or "click"
    role: str
    value: str | None = None


@dataclass
class PageSpec:
    """One page in a generated script."""

    url: str
    page_type: str
    class_name: str
    function_name: str
    path: str  # the URL's path without a trailing slash: what a test expects the address to be
    elements: list[ElementSpec] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    truncated: int = 0  # healable elements beyond the per-page cap, left out


@dataclass(frozen=True)
class GeneratedScript:
    """The result of one generation: the source, and where it was written (if it was)."""

    backend: str
    style: str
    source: str
    file_path: Path | None
    page_count: int
    element_count: int
    skipped: tuple[tuple[str, str], ...] = ()  # (url, reason) for each page left out
    truncated: int = 0
    warnings: tuple[str, ...] = ()  # problems the script will hit when run, found now

    def __str__(self) -> str:
        return self.source


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


def _literal(value: object) -> str:
    """A Python source literal for ``value``; strings are double-quoted (JSON escapes are valid)."""
    return json.dumps(value, ensure_ascii=False) if isinstance(value, str) else repr(value)


def _snake(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    if not slug:
        return "element"
    if slug[0].isdigit():
        slug = f"_{slug}"
    return f"{slug}_" if keyword.iskeyword(slug) else slug


def _pascal(text: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^a-zA-Z0-9]+", text) if part)


def _page_stem(url: str) -> str:
    """A short readable name for a URL: its last two path segments, or ``home``.

    It always starts with a letter, so it can begin a class or function name.
    """
    segments = [
        re.sub(r"\.[a-z0-9]{1,5}$", "", s, flags=re.IGNORECASE)
        for s in urlsplit(url).path.split("/")
        if s
    ]
    stem = " ".join(segments[-2:])
    if not _pascal(stem):  # empty, or nothing an identifier can use
        return "home"
    return stem if stem[0].isalpha() else f"item {stem}"


def _unique(base: str, taken: set[str]) -> str:
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


# --------------------------------------------------------------------------- #
# Reading pages
# --------------------------------------------------------------------------- #


def _sample_value(element: Element, page_type: str, first_login_field: bool) -> str:
    """Python source for a value to type into ``element``."""
    field_type = element.attributes.get("type", "").lower()
    if field_type == "password":
        return f"os.environ[{_literal(PASSWORD_ENV)}]"
    if page_type == "login" and first_login_field:
        return f"os.environ[{_literal(USERNAME_ENV)}]"
    return _literal(_SAMPLES.get(field_type, _DEFAULT_SAMPLE))


def _is_submit(element: Element) -> bool:
    if element.attributes.get("type", "").lower() == "submit":
        return True
    label = " ".join(
        s
        for s in (
            element.text_content,
            element.attributes.get("value"),
            element.attributes.get("aria-label"),
        )
        if s
    )
    return element.tag in ("button", "input") and bool(_SUBMIT_TEXT.search(label.strip()))


def _describe_page(
    url: str,
    page_type: str,
    elements: Sequence[Element],
    class_name: str,
    function_name: str,
    max_elements: int,
) -> PageSpec:
    """Decide, for one page, what each element is called and what a script does with it."""
    page = PageSpec(
        url=url,
        page_type=re.sub(r"[^a-z0-9_ ]", "", page_type.lower()) or "unknown",
        class_name=class_name,
        function_name=function_name,
        path=urlsplit(url).path.rstrip("/"),
    )
    taken: set[str] = set()
    fills: list[Step] = []
    submit: Step | None = None
    login_field_used = False
    assigned = assign_roles(elements)
    page.truncated = max(0, len(assigned) - max_elements)

    for role, element in assigned[:max_elements]:
        kind = element_kind(element)
        enabled = bool(element.computed.get("enabled", True))
        visible = bool(element.computed.get("visible", True))
        slug = role.split(":", 1)[1] if ":" in role else ""
        suffix = ("_" + role.split("#", 1)[1]) if "#" in role else ""
        slug = slug.split("#", 1)[0]
        writable = element.tag in ("input", "textarea") and "readonly" not in element.attributes

        if kind in _FILL_KINDS and enabled and writable:
            first = (
                page_type == "login"
                and element.attributes.get("type", "").lower() != "password"
                and not login_field_used
            )
            login_field_used = login_field_used or first
            value = _sample_value(element, page_type, first)
            action, verb = "fill", "fill"
            fills.append(Step("fill", role, value))
        elif kind in _CLICK_KINDS and enabled:
            action, value, verb = "click", None, "click"
            if submit is None and _is_submit(element):
                submit = Step("click", role)
        else:
            action, value, verb = "locate", None, "locate"

        method = _unique(f"{verb}_{_snake(slug or kind)}{suffix}", taken)
        page.elements.append(ElementSpec(role, kind, method, action, value, visible, enabled))

    page.steps = fills + ([submit] if submit is not None and fills else [])
    return page


# --------------------------------------------------------------------------- #
# The generator
# --------------------------------------------------------------------------- #

PagesLike = Run | Manifest | str | os.PathLike[str] | Sequence[ManifestPage]


class ScriptGenerator:
    """Generate Playwright or Selenium scripts from a crawl's extracted pages.

    ``source`` is a ``Run`` (from ``Crawler``/``Extractor``), a ``Manifest``, a path to a
    ``manifest.json``, or a list of ``ManifestPage`` (as in ``run.pages``). For a list, the page
    files are read from ``base_dir`` (default ``./output``); for the others, from beside the
    manifest. Only pages whose status is ``extracted`` are used.

    ``fingerprint_db`` — a SQLite path or a ``postgresql://`` URL — is what generated scripts heal
    against (default ``healix.db``), and generating also records the fingerprints there, so a fresh
    script has a baseline to heal from. ``on_event`` and ``webhook_url`` receive a
    ``script_generated`` event for each script.
    """

    def __init__(
        self,
        source: PagesLike,
        *,
        base_dir: str | os.PathLike[str] | None = None,
        fingerprint_db: FingerprintStore | str | os.PathLike[str] | None = None,
        record_fingerprints: bool = True,
        max_elements_per_page: int = DEFAULT_MAX_ELEMENTS,
        run_id: str | None = None,
        on_event: EventCallback | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
    ) -> None:
        if max_elements_per_page < 1:
            raise GenerationError("max_elements_per_page must be at least 1")
        self._pages, self._base_dir, manifest_run_id = self._resolve(source, base_dir)
        self.run_id = run_id or manifest_run_id or "generate"
        self._db = fingerprint_db
        self._record = record_fingerprints
        self._max_elements = max_elements_per_page
        self.on_event = on_event
        self.webhook_url = validate_webhook_url(webhook_url) if webhook_url else None
        self.webhook_secret = webhook_secret or os.environ.get("HEALIX_WEBHOOK_SECRET") or None

    @staticmethod
    def _resolve(
        source: PagesLike, base_dir: str | os.PathLike[str] | None
    ) -> tuple[list[ManifestPage], Path, str | None]:
        if isinstance(source, Run):
            manifest, default_dir = source.manifest, source.manifest_path.parent
        elif isinstance(source, Manifest):
            manifest, default_dir = source, Path(DEFAULT_MANIFEST_DIR)
        elif isinstance(source, str | os.PathLike):
            path = Path(source)
            try:
                manifest = Manifest.load(path)
            except FileNotFoundError:
                raise GenerationError(f"no manifest at {path}") from None
            except (ValueError, KeyError) as exc:
                raise GenerationError(f"could not read the manifest at {path}: {exc!r}") from exc
            default_dir = path.parent
        else:
            pages = list(source)
            if not all(isinstance(p, ManifestPage) for p in pages):
                raise GenerationError("expected a Run, a Manifest, a manifest path or pages")
            return pages, Path(base_dir or DEFAULT_MANIFEST_DIR), None
        return list(manifest.pages), Path(base_dir or default_dir), manifest.run_id

    # -- public ---------------------------------------------------------------------- #

    def to_playwright(
        self, style: str = "pom", *, output: str | os.PathLike[str] | None = None
    ) -> GeneratedScript:
        """A Playwright script. ``output`` is a directory (or ``.py`` file) to write it to."""
        return self.generate("playwright", style, output=output)

    def to_selenium(
        self, style: str = "pom", *, output: str | os.PathLike[str] | None = None
    ) -> GeneratedScript:
        """A Selenium script. ``output`` is a directory (or ``.py`` file) to write it to."""
        return self.generate("selenium", style, output=output)

    def generate(
        self, backend: str, style: str, *, output: str | os.PathLike[str] | None = None
    ) -> GeneratedScript:
        """Build the script; write it if ``output`` is given, and emit ``script_generated``."""
        if backend not in BACKENDS:
            raise GenerationError(f"backend must be one of {BACKENDS}, got {backend!r}")
        if style not in STYLES:
            raise GenerationError(f"style must be one of {STYLES}, got {style!r}")

        specs, skipped, loaded = self._read_pages()
        if not specs:
            if (
                skipped
                and len(skipped) == len(self._pages)
                and all(reason.startswith("status is") for _, reason in skipped)
            ):
                raise GenerationError(
                    "none of the pages is extracted yet: run the extraction first "
                    f"(for example {skipped[0][1]} for {skipped[0][0]})"
                )
            detail = f" ({len(skipped)} skipped: {skipped[0][1]})" if skipped else ""
            raise GenerationError(f"no page has elements to script{detail}")
        if style == "action":  # a page with nothing to fill or click has nothing to do
            specs = [p for p in specs if p.steps]
            if not specs:
                raise GenerationError("no page has an input to fill: an action script needs one")
        self._record_fingerprints(loaded)
        source = self._render(backend, style, specs)
        compile(source, f"<healix {backend} {style}>", "exec")  # never hand back broken code

        element_count = sum(len(p.steps if style == "action" else p.elements) for p in specs)
        path = self._write(source, backend, style, output)
        script = GeneratedScript(
            backend=backend,
            style=style,
            source=source,
            file_path=path,
            page_count=len(specs),
            element_count=element_count,
            skipped=tuple(skipped),
            truncated=sum(p.truncated for p in specs),
            warnings=self._fingerprint_warnings(specs),
        )
        logger.info(
            "script generated",
            backend=backend,
            style=style,
            pages=script.page_count,
            elements=element_count,
        )
        self._emit(script)
        return script

    # -- reading ---------------------------------------------------------------------- #

    def _read_pages(
        self,
    ) -> tuple[list[PageSpec], list[tuple[str, str]], list[tuple[str, list[Element]]]]:
        specs: list[PageSpec] = []
        skipped: list[tuple[str, str]] = []
        loaded: list[tuple[str, list[Element]]] = []
        class_names: set[str] = set()
        function_names: set[str] = set()
        for page in self._pages:
            if page.status != EXTRACTED or not page.output_file:
                skipped.append((page.url, f"status is {page.status}"))
                continue
            try:
                url, elements = self._load(page)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                skipped.append((page.url, f"could not read {page.output_file}: {exc}"))
                continue
            stem = _page_stem(url)
            spec = _describe_page(
                url,
                page.page_type,
                elements,
                _unique(f"{_pascal(stem)}Page", class_names),
                _unique(_snake(stem), function_names),
                self._max_elements,
            )
            if not spec.elements:
                skipped.append((page.url, "no interactive elements"))
                continue
            specs.append(spec)
            loaded.append((url, elements))
        return specs, skipped, loaded

    def _load(self, page: ManifestPage) -> tuple[str, list[Element]]:
        assert page.output_file is not None
        base = self._base_dir.resolve()
        path = (base / page.output_file).resolve()
        if not path.is_relative_to(base):
            raise ValueError("the page file is outside the output directory")
        document = json.loads(path.read_text(encoding="utf-8"))
        raw = document["elements"]
        if not isinstance(raw, list):
            raise ValueError("'elements' is not a list")
        return document.get("final_url") or page.url, [Element.from_dict(e) for e in raw]

    def _record_fingerprints(self, loaded: list[tuple[str, list[Element]]]) -> None:
        """Seed the fingerprint store the scripts will heal against."""
        if not self._record:
            return
        from healix.discovery.manifest import normalize_url

        store = self._db if isinstance(self._db, FingerprintStore) else None
        opened = store is None
        if store is None:
            store = open_store(self._db if self._db is not None else DEFAULT_FINGERPRINT_DB)
        try:
            for url, elements in loaded:
                record_page(store, normalize_url(url), elements, mode=KEEP)
        finally:
            if opened:
                store.close()

    def _fingerprint_warnings(self, pages: list[PageSpec]) -> tuple[str, ...]:
        """Say so now if the database the script heals against has nothing for its pages.

        Without fingerprints the script stops at its first step, which is a confusing place to find
        out. Only a local SQLite file is checked; a Postgres URL is not connected to.
        """
        if self._record or isinstance(self._db, FingerprintStore):
            return ()
        database, from_env = self._database()
        if from_env:
            return ()
        path = Path(database)
        if not path.exists():
            found = 0
        else:
            from healix.discovery.manifest import normalize_url

            store = open_store(path)
            try:
                found = sum(len(store.fingerprints(normalize_url(p.url))) for p in pages)
            finally:
                store.close()
        if found:
            return ()
        return (
            f"{database} has no fingerprints for these pages, so the script cannot find elements "
            "until some are recorded: generate without --no-record, or point --fingerprint-db at "
            "the database the crawl recorded into",
        )

    # -- rendering -------------------------------------------------------------------- #

    def _database(self) -> tuple[str, bool]:
        """The database the script opens, and whether it must come from the environment."""
        if isinstance(self._db, FingerprintStore):
            return DEFAULT_FINGERPRINT_DB, False
        db = str(self._db) if self._db is not None else DEFAULT_FINGERPRINT_DB
        return (DEFAULT_FINGERPRINT_DB, True) if "://" in db else (db, False)

    def _render(self, backend: str, style: str, pages: list[PageSpec]) -> str:
        from jinja2 import Environment, PackageLoader, StrictUndefined

        from healix import __version__

        env = Environment(
            loader=PackageLoader("healix.generation", "templates"),
            undefined=StrictUndefined,
            autoescape=False,  # this is Python source, not HTML
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters["py"] = _literal
        database, from_env = self._database()
        values: list[str | None] = []  # a test script types nothing
        if style == "action":
            values = [step.value for page in pages for step in page.steps]
        elif style == "pom":
            values = [e.value for page in pages for e in page.elements]
        uses_env = any("os.environ" in (v or "") for v in values)
        return env.get_template(_TEMPLATES[style]).render(
            backend=backend,
            adapter=_ADAPTERS[backend],
            style=style,
            pages=pages,
            version=__version__,
            database=database,
            database_from_env=from_env,
            database_env=FINGERPRINT_DB_ENV,
            uses_env=uses_env or from_env,
        )

    def _write(
        self, source: str, backend: str, style: str, output: str | os.PathLike[str] | None
    ) -> Path | None:
        if output is None:
            return None
        target = Path(output)
        if target.suffix != ".py":
            target = target / _FILENAMES[style].format(backend=backend)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(target, source)
        return target

    def _emit(self, script: GeneratedScript) -> None:
        sender = None
        if self.webhook_url:
            sender = WebhookSender(self.webhook_url, secret=self.webhook_secret)
        try:
            EventEmitter(self.run_id, on_event=self.on_event, sender=sender).emit(
                SCRIPT_GENERATED,
                backend=script.backend,
                style=script.style,
                file_path=str(script.file_path) if script.file_path else None,
                element_count=script.element_count,
            )
        finally:
            if sender is not None:
                sender.close()
