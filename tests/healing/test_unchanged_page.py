"""Every element recorded from a page must be found again on the same, unchanged page.

Found by generating tests against a real book shop: image-only links and icon-only buttons have
no text, label, name or test id, so they could only be identified by position and the scorer's
damping rejected even the element that was recorded. The fixture below has the same shapes.
"""

import http.server
import threading

import pytest

from healix import Healer, SQLiteFingerprintStore

CARDS_LIST = [
    f"""<li><article>
      <a href="catalogue/book-{n}_{1000 + n}/index.html"
         style="display:block;width:80px;height:80px"></a>
      <button type="button" class="btn icon"><svg width="10" height="10"></svg></button>
      <h3><a href="catalogue/book-{n}_{1000 + n}/index.html" title="Book {n}">Book {n}</a></h3>
    </article></li>"""
    for n in range(1, 7)
]


def page(reverse=False):
    cards = CARDS_LIST[::-1] if reverse else CARDS_LIST
    return f"""<!doctype html><html><head><title>Shop</title></head><body>
<nav><a href="/">Home</a><a href="/basket">Basket</a><a href="/help"><svg width="8"></svg></a></nav>
<ol>{"".join(cards)}</ol>
<form><input name="q" type="search"><button type="submit">Search</button></form>
<div id="example"><input type="text" disabled></div>
</body></html>""".encode()


class Shop(str):
    """The shop's URL, which can also be told to list its books in the opposite order."""

    reverse = False


@pytest.fixture
def shop():
    holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = page(holder["url"].reverse)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    holder["url"] = Shop(f"http://127.0.0.1:{server.server_address[1]}/")
    yield holder["url"]
    server.shutdown()
    server.server_close()


def test_every_recorded_element_is_found_again_on_an_unchanged_page(shop, tmp_path, use_backend):
    db = tmp_path / "fp.db"
    with Healer(db) as healer:
        summary = healer.learn(shop)
        assert (
            summary.added == 24
        )  # nav, image links, title links, icon buttons, the form, a bare box
    with SQLiteFingerprintStore(db) as store:
        roles = [f.element_role for f in store.fingerprints()]
    with Healer(db) as healer:
        healer.driver.navigate(shop)
        failures = {}
        for role in roles:
            try:
                result = healer.resolve(shop, role, navigate=False)
            except Exception as exc:  # noqa: BLE001 - collect them all, then fail once
                failures[role] = str(exc)[:120]
                continue
            assert result.outcome == "exact", (role, result.outcome)
        assert failures == {}
        assert healer.history() == []  # nothing was healed: nothing had changed


def test_the_image_link_and_the_icon_button_are_among_what_was_checked(shop, tmp_path, use_backend):
    db = tmp_path / "fp.db"
    with Healer(db) as healer:
        healer.learn(shop)
    with SQLiteFingerprintStore(db) as store:
        roles = {f.element_role for f in store.fingerprints()}
    assert "link:index-html" in roles and "button" in roles


def test_a_position_that_now_holds_a_different_book_is_never_returned_as_the_recorded_one(
    shop, tmp_path, use_backend
):
    """The relaxation must not turn "the selector matched something" back into "found it"."""
    db = tmp_path / "fp.db"
    with Healer(db) as healer:
        healer.learn(shop)
    shop.reverse = True  # the same URLs, in the opposite order: position 1 is now the last book
    with Healer(db) as healer:
        try:
            result = healer.resolve(shop, "link:index-html")  # recorded as book 1's image link
        except Exception:  # noqa: BLE001 - refusing is fine; guessing is not
            return
        assert "book-1_1001" in result.element.attributes["href"]
