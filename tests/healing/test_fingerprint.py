import pytest

from healix.driver.base import Element
from healix.healing.fingerprint import Fingerprint


def _fingerprint(**overrides):
    base = {
        "page_url": "http://x/",
        "element_role": "textbox",
        "tag": "input",
        "id": "user-4471",
        "id_normalized": "user-{n}",
        "name": "username",
        "attributes": {"data-testid": "login-username", "aria-label": "Username"},
        "css_selector": "#user-4471",
        "xpath": "//input[1]",
        "text_content": "hello",
    }
    base.update(overrides)
    return Fingerprint(**base)


def test_locators_follow_priority_order():
    strategies = [s.strategy for s in _fingerprint().locators()]
    assert strategies == [
        "stable_attr:data-testid",
        "id",
        "name",
        "aria_label",
        "css",
        "xpath",
        "normalized_id",
        "text",
    ]


def test_normalized_id_skipped_when_identical_to_raw_id():
    strategies = [s.strategy for s in _fingerprint(id="login", id_normalized="login").locators()]
    assert "normalized_id" not in strategies


def test_attribute_values_are_escaped():
    locators = {s.strategy: s for s in _fingerprint(id='a"b\\c').locators()}
    assert locators["id"].value == '[id="a\\"b\\\\c"]'


def test_from_element_copies_identity():
    element = Element.from_dict(
        {"tag": "input", "id": "user-4471", "name": "username", "attributes": {"role": "textbox"}}
    )
    fp = Fingerprint.from_element(element, "http://x/")
    assert (fp.tag, fp.id, fp.id_normalized, fp.element_role) == (
        "input",
        "user-4471",
        "user-{n}",
        "textbox",
    )


# --- persistence shape ------------------------------------------------------------------- #


def test_fingerprint_round_trips_through_a_dict_including_shadow_path():
    element = Element.from_dict(
        {
            "tag": "input",
            "id": "user-4471",
            "name": "u",
            "classes": ["a"],
            "attributes": {"type": "text"},
            "xpath": "/html[1]",
            "css_selector": "#host #x",
            "shadow_path": ["#host"],
            "dom_context": {"parent_tag": "form"},
        },
        iframe_path=["main", "f"],
    )
    fp = Fingerprint.from_element(element, "https://e.com/", "textbox:u")
    assert fp.shadow_path == ["#host"] and fp.iframe_path == ["main", "f"]
    assert Fingerprint.from_dict(fp.to_dict()) == fp


def test_from_dict_ignores_unknown_keys_and_tolerates_missing_ones():
    fp = Fingerprint.from_dict({"page_url": "u", "element_role": "r", "field_from_the_future": 1})
    assert (fp.page_url, fp.element_role, fp.classes, fp.shadow_path) == ("u", "r", [], [])


def test_key_and_element_key():
    fp = Fingerprint(page_url="https://e.com/a", element_role="button:go")
    assert fp.key == ("https://e.com/a", "button:go")
    assert fp.element_key == "https://e.com/a#button:go"


def test_describe_locator_names_the_highest_priority_locator():
    from healix.healing.fingerprint import describe_locator

    fp = Fingerprint(
        page_url="u", element_role="r", id="x", name="n", attributes={"data-testid": "t"}
    )
    assert describe_locator(fp) == 'stable_attr:data-testid=[data-testid="t"]'
    assert describe_locator(Fingerprint(page_url="u", element_role="r")) == ""


def test_the_healing_package_exports_lazily_but_completely():
    import healix.healing as healing

    for name in healing.__all__:
        assert getattr(healing, name) is not None
    with pytest.raises(AttributeError):
        healing.NoSuchThing  # noqa: B018
    assert "Resolver" in healing.__all__ and "SQLiteFingerprintStore" in healing.__all__
