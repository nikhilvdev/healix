"""Shared fixtures: the fixture site served over HTTP on two origins."""

from __future__ import annotations

import functools
import http.server
import shutil
import threading
from pathlib import Path

import pytest

SITE = Path(__file__).parent / "fixtures" / "site"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass


def _serve(directory: Path) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture(scope="session")
def site_url(tmp_path_factory):
    """Base URL of the fixture site. A second server on another port acts as a cross-origin host."""
    root = tmp_path_factory.mktemp("site")
    shutil.copytree(SITE, root, dirs_exist_ok=True)
    foreign = _serve(root)
    # "localhost" vs "127.0.0.1" would also differ, but a different port is enough for a different origin.
    cross_origin = f"http://127.0.0.1:{foreign.server_address[1]}"
    index = root / "index.html"
    index.write_text(index.read_text().replace("{{CROSS_ORIGIN}}", cross_origin))
    main = _serve(root)
    yield f"http://127.0.0.1:{main.server_address[1]}"
    for server in (main, foreign):
        server.shutdown()
        server.server_close()


def _page(title: str, input_name: str, links: list[str] = (), extra: str = "") -> str:
    anchors = "".join(f'<a href="{href}">{href}</a>\n' for href in links)
    return f"<!doctype html><html><head><title>{title}</title></head><body><h1>{title}</h1>" \
           f'<input name="{input_name}">{anchors}{extra}</body></html>'


@pytest.fixture(scope="session")
def crawl_site_url(tmp_path_factory):
    """A small linked site: a deep chain, a product template, links in an iframe and a shadow root."""
    root = tmp_path_factory.mktemp("crawl_site")
    (root / "products").mkdir()
    shadow = (
        '<link-host id="lh"></link-host><script>document.getElementById("lh").attachShadow({mode:"open"})'
        '.innerHTML = \'<a href="/shadow-target.html">shadow link</a>\';</script>'
    )
    (root / "index.html").write_text(_page(
        "Home", "home",
        ["/a.html", "/products/1.html", "/products/2.html", "/products/3.html", "/products/4.html",
         "https://external.invalid/x", "mailto:x@y.z", "/files/report.pdf", "/a.html?utm_source=mail"],
        extra='<iframe src="/embed.html"></iframe>' + shadow,
    ))
    (root / "embed.html").write_text(_page("Embed", "embed", ["/embedded-target.html"]))
    for name, nxt in [("a", "b"), ("b", "c"), ("c", "d"), ("d", None)]:
        (root / f"{name}.html").write_text(_page(name.upper(), name, [f"/{nxt}.html"] if nxt else ["/"]))
    for i in range(1, 5):
        links = ["/products/1.html"] + (["/hidden.html"] if i == 3 else [])
        (root / "products" / f"{i}.html").write_text(_page(f"Product {i}", "product", links))
    for name in ("hidden", "embedded-target", "shadow-target"):
        (root / f"{name}.html").write_text(_page(name, name))
    server = _serve(root)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
