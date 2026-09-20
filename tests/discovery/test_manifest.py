import json

import pytest

from healix.discovery.manifest import (
    EXTRACTED,
    FAILED,
    PENDING,
    Manifest,
    normalize_url,
    structural_hash,
    template_key,
)
from healix.driver.base import Element


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("HTTPS://Example.COM:443/a/b/", "https://example.com/a/b"),
        ("http://example.com:80", "http://example.com/"),
        ("http://example.com:8080/x", "http://example.com:8080/x"),
        ("https://e.com/p?b=2&a=1", "https://e.com/p?a=1&b=2"),
        ("https://e.com/p?utm_source=x&id=7&gclid=abc&PHPSESSID=zz", "https://e.com/p?id=7"),
        ("https://e.com/p;jsessionid=ABC123?x=1", "https://e.com/p?x=1"),
        ("https://e.com/p#section", "https://e.com/p"),
        ("https://e.com/app#/orders/5", "https://e.com/app#/orders/5"),
        ("https://user:pw@e.com/p", "https://e.com/p"),
        ("https://e.com/p?a=", "https://e.com/p?a="),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_url_rejects_malformed_port():
    with pytest.raises(ValueError):
        normalize_url("http://e.com:notaport/")


def test_template_key_collapses_volatile_segments_only():
    assert template_key("https://e.com/product/123") == template_key("https://e.com/product/456")
    assert template_key("https://e.com/product/123/") == "https://e.com/product/{n}"
    assert (
        template_key("https://e.com/orders/123e4567-e89b-12d3-a456-426614174000")
        == "https://e.com/orders/{uuid}"
    )
    assert template_key("https://e.com/about") != template_key("https://e.com/contact")
    # query values are kept: ?page=2 and ?page=3 are different pages, not one template
    assert template_key("https://e.com/list?page=2") != template_key("https://e.com/list?page=3")


def _el(tag, **kw):
    return Element.from_dict({"tag": tag, **kw})


def test_structural_hash_ignores_text_list_length_volatile_ids_and_class_state():
    a = [
        _el("h1", text_content="Widget"),
        _el("li", id="row-1"),
        _el("li", id="row-2"),
        _el("a", classes=["active"]),
    ]
    b = [
        _el("h1", text_content="Gadget"),
        _el("li", id="row-7"),
        _el("li", id="row-8"),
        _el("li", id="row-9"),
        _el("a"),
    ]
    assert structural_hash(a) == structural_hash(b)


def test_structural_hash_differs_for_different_structure():
    form = [_el("input", name="email", attributes={"type": "email"})]
    other = [_el("input", name="email", attributes={"type": "password"})]
    assert structural_hash(form) != structural_hash(other)
    assert structural_hash(form) != structural_hash([])


def test_add_page_dedupes_by_url_then_structure():
    m = Manifest("r1")
    first, new = m.add_page("https://e.com/product/1", "h1", "detail")
    assert new and first.status == PENDING

    again, new = m.add_page("https://e.com/product/1/?utm_source=x", "h1")
    assert again is first and not new

    variant, new = m.add_page("https://e.com/product/2", "h1")
    assert variant is first and not new
    assert first.variant_urls == ["https://e.com/product/2"]
    assert m.find_by_url("https://e.com/product/2") is first
    assert m.pages_discovered == 1

    _, new = m.add_page("https://e.com/about", "h2")
    assert new and m.pages_discovered == 2


def test_structural_dedupe_can_be_disabled():
    m = Manifest("r1")
    m.add_page("https://e.com/a", "h1")
    _, new = m.add_page("https://e.com/b", "h1", dedupe_structural=False)
    assert new and m.pages_discovered == 2


def test_status_tracking_and_resume_set():
    m = Manifest("r1")
    for name in "abcd":
        m.add_page(f"https://e.com/{name}", f"h-{name}")
    m.mark_extracted("https://e.com/a", "out/a.json")
    m.mark_failed("https://e.com/b", "timeout")
    assert [p.url for p in m.remaining_pages()] == [
        "https://e.com/b",
        "https://e.com/c",
        "https://e.com/d",
    ]
    assert m.pages_extracted == 1
    assert m.find_by_url("https://e.com/a").output_file == "out/a.json"
    assert m.find_by_url("https://e.com/b").status == FAILED

    m.mark_extracted("https://e.com/b", "out/b.json")  # a retry succeeding clears the error
    assert m.find_by_url("https://e.com/b").error is None
    with pytest.raises(KeyError):
        m.mark_extracted("https://e.com/nope", "x")


def test_add_failed_records_unloadable_url():
    m = Manifest("r1")
    page = m.add_failed("https://e.com/down", "net::ERR_CONNECTION_REFUSED")
    assert (page.status, page.structural_hash) == (FAILED, None)
    assert m.remaining_pages() == [page]


def test_save_load_roundtrip_preserves_state_and_dedup_index(tmp_path):
    m = Manifest("run-9", start_urls=["https://e.com/"], platform_detected=None)
    m.add_page("https://e.com/product/1", "h1", "detail")
    m.add_page("https://e.com/product/2", "h1")
    m.add_failed("https://e.com/down", "boom")
    m.mark_extracted("https://e.com/product/1", "out/p1.json")

    path = tmp_path / "nested" / "manifest.json"
    m.save(path)
    loaded = Manifest.load(path)

    assert loaded.to_dict() == m.to_dict()
    assert loaded.find_by_url("https://e.com/product/2").url == "https://e.com/product/1"
    _, new = loaded.add_page("https://e.com/product/3", "h1")
    assert not new  # the hash index survived the round trip
    assert not list(tmp_path.glob("nested/.*tmp"))  # no temp files left behind


def test_manifest_json_has_documented_run_level_fields(tmp_path):
    m = Manifest("run-1")
    m.add_page("https://e.com/", "h")
    m.save(tmp_path / "m.json")
    raw = json.loads((tmp_path / "m.json").read_text())
    assert {
        "run_id",
        "pages_discovered",
        "pages_extracted",
        "platform_detected",
        "pages",
    } <= raw.keys()
    assert {"url", "page_type", "structural_hash", "status", "output_file"} <= raw["pages"][
        0
    ].keys()
    assert raw["pages_discovered"] == 1 and raw["pages_extracted"] == 0


def test_load_rejects_invalid_status(tmp_path):
    (tmp_path / "m.json").write_text(
        json.dumps({"run_id": "r", "pages": [{"url": "u", "status": "bogus"}]})
    )
    with pytest.raises(ValueError):
        Manifest.load(tmp_path / "m.json")


def test_extracted_constant_used_by_resume():
    m = Manifest("r")
    p, _ = m.add_page("https://e.com/x", "h")
    p.status = EXTRACTED
    assert m.remaining_pages() == []


def test_a_manifests_role_survives_saving_and_is_left_out_when_there_is_none(tmp_path):
    from healix.discovery.manifest import Manifest

    plain = Manifest("r")
    assert "role" not in plain.to_dict()
    named = Manifest("r", role="admin")
    named.save(tmp_path / "m.json")
    assert Manifest.load(tmp_path / "m.json").role == "admin"
    assert Manifest.from_dict({"run_id": "old"}).role is None
