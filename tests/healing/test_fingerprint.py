from healix.driver.base import Element
from healix.healing.fingerprint import Fingerprint


def _fingerprint(**overrides):
    base = dict(
        page_url="http://x/",
        element_role="textbox",
        tag="input",
        id="user-4471",
        id_normalized="user-{n}",
        name="username",
        attributes={"data-testid": "login-username", "aria-label": "Username"},
        css_selector="#user-4471",
        xpath="//input[1]",
        text_content="hello",
    )
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
    assert (fp.tag, fp.id, fp.id_normalized, fp.element_role) == ("input", "user-4471", "user-{n}", "textbox")
