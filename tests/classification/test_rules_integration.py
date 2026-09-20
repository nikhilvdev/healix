"""Classification of realistic pages rendered in a real browser."""

import pytest

from healix.classification import classify_page

# (page, expected type). Most pages sit at neutral URLs so the *element* signals carry the
# decision; the OAuth and detail pages need their URL, which is part of what they test.
CASES = [
    ("case_login_basic.html", "login"),
    ("case_login_no_cues.html", "login"),
    ("oauth/authorize.html", "login"),  # no password field: the OAuth redirect URL + SSO buttons
    ("case_signup.html", "form"),  # password field, but a registration form is not a login
    ("case_dashboard.html", "dashboard"),
    ("case_list_table.html", "list"),
    ("case_list_cards.html", "list"),  # has a header search box; that must not make it "search"
    ("products/123.html", "detail"),
    ("case_form_contact.html", "form"),
    ("case_search.html?q=shoes", "search"),  # also a form and a list; search is more specific
    ("case_checkout.html", "checkout"),  # also a form; checkout is more specific
    ("case_nav_shell.html", "nav_shell"),
    ("case_modal_over_page.html", "modal"),
    ("case_modal_standalone.html", "modal"),
    ("case_cookie_banner_article.html", "unknown"),  # a thin banner must not make it a modal
    ("case_minimal.html", "unknown"),
]


@pytest.mark.parametrize("page, expected", CASES, ids=[c[0] for c in CASES])
def test_classifies_rendered_page(driver, classify_site_url, page, expected):
    driver.navigate(f"{classify_site_url}/{page}")
    result = classify_page(driver.get_elements(), driver.current_url)
    assert result.page_type == expected, f"scores={result.scores} signals={result.signals}"


def test_classification_makes_no_network_calls(driver, classify_site_url, monkeypatch):
    import socket

    driver.navigate(f"{classify_site_url}/case_login_basic.html")
    elements = driver.get_elements()

    def refuse(*args, **kwargs):
        raise AssertionError("classification must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert classify_page(elements, driver.current_url).page_type == "login"
