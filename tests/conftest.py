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
    """Base URL of the fixture site; a second server on another port is a cross-origin host."""
    root = tmp_path_factory.mktemp("site")
    shutil.copytree(SITE, root, dirs_exist_ok=True)
    foreign = _serve(root)
    # A different port is enough to make a different origin.
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
    return (
        f"<!doctype html><html><head><title>{title}</title></head><body><h1>{title}</h1>"
        f'<input name="{input_name}">{anchors}{extra}</body></html>'
    )


@pytest.fixture(scope="session")
def crawl_site_url(tmp_path_factory):
    """A small linked site: deep chain, product template, links in an iframe and a shadow root."""
    root = tmp_path_factory.mktemp("crawl_site")
    (root / "products").mkdir()
    shadow = (
        '<link-host id="lh"></link-host>'
        '<script>document.getElementById("lh").attachShadow({mode:"open"})'
        ".innerHTML = '<a href=\"/shadow-target.html\">shadow link</a>';</script>"
    )
    (root / "index.html").write_text(
        _page(
            "Home",
            "home",
            [
                "/a.html",
                "/products/1.html",
                "/products/2.html",
                "/products/3.html",
                "/products/4.html",
                "https://external.invalid/x",
                "mailto:x@y.z",
                "/files/report.pdf",
                "/a.html?utm_source=mail",
            ],
            extra='<iframe src="/embed.html"></iframe>' + shadow,
        )
    )
    (root / "embed.html").write_text(_page("Embed", "embed", ["/embedded-target.html"]))
    for name, nxt in [("a", "b"), ("b", "c"), ("c", "d"), ("d", None)]:
        (root / f"{name}.html").write_text(
            _page(name.upper(), name, [f"/{nxt}.html"] if nxt else ["/"])
        )
    for i in range(1, 5):
        links = ["/products/1.html"] + (["/hidden.html"] if i == 3 else [])
        (root / "products" / f"{i}.html").write_text(_page(f"Product {i}", "product", links))
    for name in ("hidden", "embedded-target", "shadow-target"):
        (root / f"{name}.html").write_text(_page(name, name))
    server = _serve(root)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def classify_site_url():
    """Realistic page fixtures for each page type, served over HTTP."""
    server = _serve(Path(__file__).parent / "fixtures" / "classify")
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def captured_logs():
    """Capture every Healix log record emitted during a test (all levels), then restore logging."""
    from logquill import CollectingTransport

    from healix.log import configure_logging, get_logger

    root = get_logger()
    saved_transports, saved_level = root.transports, root.level
    transport = CollectingTransport()
    configure_logging(level="trace", transports=[transport])
    yield transport.records
    configure_logging(level=saved_level, transports=saved_transports)


class WebhookReceiver:
    """A local HTTP endpoint that records every POST and answers with scripted statuses."""

    def __init__(self):
        self.requests: list[dict] = []
        self._statuses: list[int] = []
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                receiver.requests.append(
                    {"path": self.path, "headers": dict(self.headers.items()), "body": body}
                )
                status = receiver._statuses.pop(0) if receiver._statuses else 200
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def respond_with(self, *statuses: int) -> None:
        """Statuses for the next requests, in order; after they run out the answer is 200."""
        self._statuses = list(statuses)

    @property
    def payloads(self) -> list[dict]:
        import json

        return [json.loads(r["body"]) for r in self.requests]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def webhook_receiver():
    receiver = WebhookReceiver()
    yield receiver
    receiver.close()
