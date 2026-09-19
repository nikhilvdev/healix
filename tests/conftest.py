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
