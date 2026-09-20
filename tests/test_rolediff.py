"""The comparison of what each role can reach, built from hand-made manifests and page files."""

import json

import pytest

from healix.discovery.manifest import Manifest
from healix.rolediff import (
    DIFF_FILENAME,
    RoleOutput,
    build_role_diff,
    reached_urls,
    write_role_diff,
)


def element(tag="button", text=None, visible=True, **attrs):
    return {
        "tag": tag,
        "id": None,
        "name": None,
        "classes": [],
        "attributes": {k.replace("_", "-"): v for k, v in attrs.items()},
        "text_content": text,
        "computed": {"visible": visible, "enabled": True},
        "css_selector": f"{tag}",
        "iframe_path": ["main"],
        "dom_context": {},
    }


def output(tmp_path, role, pages, *, failed=(), variants=None):
    """A role's finished crawl: ``pages`` maps url -> its elements (written to page files)."""
    directory = tmp_path / role
    (directory / "pages").mkdir(parents=True)
    manifest = Manifest("run-1", role=role)
    for index, (url, elements) in enumerate(pages.items(), 1):
        page, _ = manifest.add_page(url, f"hash-{url}-{role}")
        (directory / "pages" / f"{index}.json").write_text(json.dumps({"elements": elements}))
        manifest.mark_extracted(url, f"pages/{index}.json")
    for url in failed:
        manifest.add_failed(url, "403")
    for url, variant in (variants or {}).items():
        manifest.find_by_url(url).variant_urls.append(variant)
    return RoleOutput(manifest, directory)


def shop(role, *extra):
    return [element(text="Home"), element(text="Log out"), *extra]


def test_a_page_only_one_role_reaches_is_reported_with_who_lacks_it(tmp_path):
    admin = output(tmp_path, "admin", {"https://e.com/": shop("admin"), "https://e.com/admin": []})
    user = output(tmp_path, "user", {"https://e.com/": shop("user")})
    diff = build_role_diff({"admin": admin, "user": user}, run_id="r")
    assert diff.page_differences == [
        {"url": "https://e.com/admin", "roles": ["admin"], "missing": ["user"]}
    ]
    assert diff.reached == {"admin": 2, "user": 1}
    assert diff.in_all_roles == 1
    assert diff.only("admin")["pages"] == ["https://e.com/admin"]
    assert diff.only("user")["pages"] == []


def test_pages_both_reach_are_not_differences(tmp_path):
    same = {"https://e.com/": shop("x"), "https://e.com/a": shop("x")}
    diff = build_role_diff(
        {"a": output(tmp_path, "a", same), "b": output(tmp_path, "b", same)}, run_id="r"
    )
    assert diff.differences == 0 and diff.in_all_roles == 2


def test_an_element_only_one_role_sees_on_a_shared_page_is_reported(tmp_path):
    delete = element(text="Delete user", data_testid="delete-user")
    admin = output(tmp_path, "admin", {"https://e.com/users": shop("a", delete)})
    user = output(tmp_path, "user", {"https://e.com/users": shop("u")})
    diff = build_role_diff({"admin": admin, "user": user}, run_id="r")
    assert diff.page_differences == []
    assert diff.element_differences == [
        {
            "url": "https://e.com/users",
            "element": "button:delete-user",
            "roles": ["admin"],
            "missing": ["user"],
        }
    ]
    assert diff.only("admin")["elements"] == [["https://e.com/users", "button:delete-user"]]


def test_a_hidden_element_is_not_a_control_the_role_can_use(tmp_path):
    hidden = element(text="Delete user", data_testid="delete-user", visible=False)
    admin = output(tmp_path, "admin", {"https://e.com/u": shop("a", hidden)})
    user = output(tmp_path, "user", {"https://e.com/u": shop("u")})
    assert build_role_diff({"admin": admin, "user": user}, run_id="r").differences == 0


def test_a_page_that_failed_to_load_for_a_role_is_not_reached_by_it(tmp_path):
    admin = output(tmp_path, "admin", {"https://e.com/": shop("a"), "https://e.com/admin": []})
    user = output(tmp_path, "user", {"https://e.com/": shop("u")}, failed=["https://e.com/admin"])
    assert "https://e.com/admin" not in reached_urls(user.manifest)
    diff = build_role_diff({"admin": admin, "user": user}, run_id="r")
    assert diff.page_differences[0]["missing"] == ["user"]


def test_a_variant_counts_as_reached_but_has_no_elements_to_compare(tmp_path):
    admin = output(tmp_path, "admin", {"https://e.com/p/1": shop("a", element(text="Edit"))})
    user = output(
        tmp_path,
        "user",
        {"https://e.com/p/2": shop("u")},
        variants={"https://e.com/p/2": "https://e.com/p/1"},
    )
    diff = build_role_diff({"admin": admin, "user": user}, run_id="r")
    assert diff.page_differences == [
        {"url": "https://e.com/p/2", "roles": ["user"], "missing": ["admin"]}
    ]
    assert diff.element_differences == []  # /p/1 is only a variant for one of them


def test_three_roles_say_exactly_who_has_what(tmp_path):
    base = {"https://e.com/": shop("x")}
    a = output(tmp_path, "a", {**base, "https://e.com/x": []})
    b = output(tmp_path, "b", {**base, "https://e.com/x": []})
    c = output(tmp_path, "c", base)
    diff = build_role_diff({"a": a, "b": b, "c": c}, run_id="r")
    assert diff.page_differences == [
        {"url": "https://e.com/x", "roles": ["a", "b"], "missing": ["c"]}
    ]
    assert diff.only("a")["pages"] == [] and diff.only("c")["pages"] == []


def test_a_page_file_that_cannot_be_read_is_reported_not_fatal(tmp_path):
    a = output(tmp_path, "a", {"https://e.com/": shop("a")})
    b = output(tmp_path, "b", {"https://e.com/": shop("b")})
    (b.directory / "pages" / "1.json").write_text("not json")
    diff = build_role_diff({"a": a, "b": b}, run_id="r")
    assert diff.unreadable == [{"url": "https://e.com/", "role": "b"}]
    assert diff.element_differences == []


def test_the_written_file_has_a_summary_and_the_differences(tmp_path):
    admin = output(tmp_path, "admin", {"https://e.com/": shop("a"), "https://e.com/admin": []})
    user = output(tmp_path, "user", {"https://e.com/": shop("u")})
    path = write_role_diff(build_role_diff({"admin": admin, "user": user}, run_id="r9"), tmp_path)
    assert path == tmp_path / DIFF_FILENAME
    document = json.loads(path.read_text())
    assert document["run_id"] == "r9" and document["roles"] == ["admin", "user"]
    assert document["summary"] == {
        "pages_reached": {"admin": 2, "user": 1},
        "pages_in_all_roles": 1,
        "page_differences": 1,
        "element_differences": 0,
        "only": {
            "admin": {"pages": 1, "elements": 0},
            "user": {"pages": 0, "elements": 0},
        },
    }
    assert "unreadable" not in document
    assert document["generated_at"].endswith("Z")


@pytest.mark.parametrize("roles", [1, 2])
def test_a_single_role_compares_to_nothing(tmp_path, roles):
    outputs = {
        f"r{i}": output(tmp_path, f"r{i}", {"https://e.com/": shop("x")}) for i in range(roles)
    }
    assert build_role_diff(outputs, run_id="r").differences == 0
