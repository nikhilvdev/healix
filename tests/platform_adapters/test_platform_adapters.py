"""Platform adapters in a real browser, on every backend.

Adapters only ever add ``platform_signal``. So each test also checks the other direction: with the
adapters switched off, or broken, or the platform absent, the elements are exactly what the generic
pipeline produces.
"""

import pytest

from healix.driver.base import Element
from healix.healing.fingerprint import Fingerprint
from healix.platform_adapters import ADAPTERS, PlatformAdapter, detected_platform
from tests.driver.backends import make_driver
from tests.platform_adapters import pages


def page_file(tmp_path, html):
    page = tmp_path / "page.html"
    page.write_text("<!doctype html><html>" + html + "</html>")
    return page.as_uri()


def extract(backend, tmp_path, html, adapters=None):
    """Every element of ``html`` as a fresh browser sees it, with ``adapters`` (default: all).

    A driver is opened and closed per call: Playwright's sync API allows one instance at a time.
    """
    driver = make_driver(backend, platform_adapters=adapters)
    try:
        driver.navigate(page_file(tmp_path, html))
        return driver.get_elements()
    finally:
        driver.close()


def by_id(elements):
    return {e.id: e for e in elements if e.id}


def signals(elements):
    return {id: e.platform_signal for id, e in by_id(elements).items() if e.platform_signal}


def as_dicts(elements, *, drop_signal=False):
    return [{**e.to_dict(), **({"platform_signal": None} if drop_signal else {})} for e in elements]


# --- SAP UI5 ---------------------------------------------------------------------------- #


def sap(control_id, control_type, root):
    return {
        "platform": "sap_ui5",
        "control_id": control_id,
        "control_type": control_type,
        "is_control_root": root,
    }


def test_sap_ui5_controls_get_their_control_id_and_type(backend, tmp_path):
    elements = extract(backend, tmp_path, pages.SAP_UI5_CORE)
    assert signals(elements) == {
        "app": sap("app", "sap.m.App", True),
        "save": sap("save", "sap.m.Button", True),
        "save-inner": sap("save", "sap.m.Button", False),
        "name": sap("name", "sap.m.Input", True),
        "name-inner": sap("name", "sap.m.Input", False),
    }
    # not in a control, or marked as one but unknown to the registry: no signal
    found = by_id(elements)
    assert all(found[i].platform_signal is None for i in ("plain", "orphan", "orphan-child"))


def test_sap_ui5_without_get_core_uses_the_element_registry(backend, tmp_path):
    found = by_id(extract(backend, tmp_path, pages.SAP_UI5_ELEMENT))
    assert found["save"].platform_signal == sap("save", "sap.m.Button", True)
    assert found["save-inner"].platform_signal == sap("save", "sap.m.Button", False)


@pytest.mark.parametrize(
    "html", [pages.SAP_WITHOUT_UI5_REGISTRY, pages.SAP_BROKEN_REGISTRY, pages.NOT_SAP]
)
def test_sap_ui5_adds_nothing_when_it_cannot_look_a_control_up(backend, tmp_path, html):
    assert signals(extract(backend, tmp_path, html)) == {}


# --- Salesforce ------------------------------------------------------------------------- #


def lwc(component, host, **aura):
    return {
        "platform": "salesforce_lwc",
        "component": component,
        "is_component_host": host,
        "aura_attributes": aura,
    }


@pytest.mark.parametrize(
    "html", [pages.SALESFORCE_WITH_AURA_GLOBAL, pages.SALESFORCE_BY_MARKER_ONLY]
)
def test_salesforce_components_are_named_and_aura_attributes_captured(backend, tmp_path, html):
    elements = extract(backend, tmp_path, html)
    assert signals(elements) == {
        "host": lwc("c-parent", True),
        "top-level": lwc("lightning-badge", True),
        "light": lwc(None, False, **{"data-aura-class": "cCmp"}),
        "field": lwc("lightning-input", True, **{"data-aura-rendered-by": "1:0"}),
        # in a component's template: the component that rendered it is its shadow host
        "inner": lwc("c-parent", False),
        "in-shadow": lwc("c-parent", False),
    }
    assert by_id(elements)["plain"].platform_signal is None


def test_salesforce_adds_nothing_when_the_platform_is_not_there(backend, tmp_path):
    assert signals(extract(backend, tmp_path, pages.NOT_SALESFORCE)) == {}


# --- additive only ---------------------------------------------------------------------- #

ALL_PAGES = [
    pages.SAP_UI5_CORE,
    pages.SALESFORCE_WITH_AURA_GLOBAL,
    pages.NOT_SAP,
    pages.NOT_SALESFORCE,
]


@pytest.mark.parametrize("html", ALL_PAGES)
def test_the_generic_pipeline_alone_gives_the_same_elements_minus_the_signal(
    backend, tmp_path, html
):
    generic = extract(backend, tmp_path, html, adapters=())
    assert all(e.platform_signal is None for e in generic)
    assert as_dicts(generic) == as_dicts(extract(backend, tmp_path, html), drop_signal=True)


def test_an_adapter_that_throws_cannot_break_extraction(backend, tmp_path):
    broken = [
        PlatformAdapter("throws_per_element", "true", "(el) => { throw new Error('boom'); }"),
        PlatformAdapter(
            "throws_on_detect", "(() => { throw new Error('boom'); })()", "(el) => ({})"
        ),
    ]
    generic = extract(backend, tmp_path, pages.NOT_SAP, adapters=())
    assert as_dicts(extract(backend, tmp_path, pages.NOT_SAP, adapters=broken)) == as_dicts(generic)


def test_the_first_adapter_with_a_signal_wins(backend, tmp_path):
    first = PlatformAdapter("first", "true", "(el) => el.id === 'plain' ? {tag: 'one'} : null")
    second = PlatformAdapter("second", "true", "(el) => ({tag: 'two'})")
    found = by_id(extract(backend, tmp_path, pages.NOT_SAP, adapters=[first, second]))
    assert found["plain"].platform_signal == {"platform": "first", "tag": "one"}
    assert found["save"].platform_signal == {"platform": "second", "tag": "two"}


def test_both_built_in_adapters_are_registered():
    assert [a.name for a in ADAPTERS] == ["sap_ui5", "salesforce_lwc"]


# --- the signal travels with the element ------------------------------------------------- #


def test_the_signal_reaches_fingerprints_and_found_elements(backend, tmp_path):
    driver = make_driver(backend)
    try:
        driver.navigate(page_file(tmp_path, pages.SAP_UI5_CORE))
        save = by_id(driver.get_elements())["save"]
        fingerprint = Fingerprint.from_element(save, "u")
        assert fingerprint.platform_signal == sap("save", "sap.m.Button", True)
        assert Fingerprint.from_dict(fingerprint.to_dict()) == fingerprint
        found = driver.find(fingerprint)  # located again, described again, signal included
        assert found.platform_signal == save.platform_signal
    finally:
        driver.close()


def test_the_detected_platform_comes_from_the_elements_that_have_a_signal():
    plain = Element(tag="p")
    sap_element = Element(tag="button", platform_signal={"platform": "sap_ui5"})
    other = Element(tag="a", platform_signal={"platform": "salesforce_lwc"})
    assert detected_platform([plain]) is None
    assert detected_platform([]) is None
    assert detected_platform([plain, sap_element, other]) == "sap_ui5"
