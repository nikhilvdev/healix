"""Extracted pages on disk, built from synthetic elements, for the generation tests (no browser)."""

from __future__ import annotations

import json
from pathlib import Path

from healix.discovery.manifest import EXTRACTED, PENDING, Manifest, ManifestPage
from tests.elements import el

BASE = "http://shop.example.test"


def login_elements():
    return [
        el("input", id="u1", name="username", type="text", data_testid="login-username"),
        el("input", id="p1", name="password", type="password", value="hunter2-do-not-leak"),
        el("input", id="rm", name="remember", type="checkbox"),
        el("button", text="Sign in", type="submit", id="go"),
        el("a", text="Forgot password?", href=f"{BASE}/forgot"),
    ]


def order_elements():
    return [
        el("input", name="email", type="email"),
        el("textarea", name="notes"),
        el("input", name="locked", type="text", readonly=""),
        el("input", name="frozen", type="text", enabled=False),
        el("button", text="Save order"),
    ]


def home_elements():
    return [el("a", text="Home", href=f"{BASE}/")]


DEFAULT_PAGES = {
    f"{BASE}/login": ("login", login_elements),
    f"{BASE}/orders/new": ("form", order_elements),
    f"{BASE}/": ("nav_shell", home_elements),
}


def write_run(out: Path, pages=None, *, status: str = EXTRACTED, run_id: str = "run-1") -> Path:
    """Write ``pages`` ({url: (page_type, elements-or-factory)}) and a manifest; return its path."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "pages").mkdir(exist_ok=True)
    entries = []
    for index, (url, (page_type, elements)) in enumerate((pages or DEFAULT_PAGES).items(), 1):
        elements = elements() if callable(elements) else elements
        name = f"pages/{index:04d}.json"
        document = {"url": url, "elements": [e.to_dict() for e in elements]}
        (out / name).write_text(json.dumps(document))
        entries.append(
            ManifestPage(
                url=url,
                structural_hash=f"hash{index}",
                page_type=page_type,
                status=status,
                output_file=name if status == EXTRACTED else None,
            )
        )
    manifest = Manifest(run_id=run_id, pages=entries)
    manifest.save(out / "manifest.json")
    return out / "manifest.json"


__all__ = ["BASE", "DEFAULT_PAGES", "EXTRACTED", "PENDING", "write_run"]
