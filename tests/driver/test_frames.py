from dataclasses import dataclass, field

from healix.driver.base import Frame
from healix.driver.frames import collect_elements, origin_of, walk_frames


@dataclass
class Node:
    url: str
    name: str | None = None
    children: list["Node"] = field(default_factory=list)


def _walk(root):
    return walk_frames(
        root, children_of=lambda n: n.children, url_of=lambda n: n.url, name_of=lambda n: n.name
    )


def test_origin_of():
    assert origin_of("https://Example.com/a?b=1") == "https://example.com"
    assert origin_of("about:blank") is None
    assert origin_of("") is None
    assert origin_of("data:text/html,hi").startswith("opaque:")
    assert origin_of("http://a.com:8080/x") != origin_of("http://a.com/x")


def test_paths_start_at_main_and_nest():
    tree = Node(
        "http://a.com/",
        children=[Node("http://a.com/p", "panel", [Node("http://a.com/f", "form")])],
    )
    assert [f.path for f in _walk(tree)] == [["main"], ["main", "panel"], ["main", "panel", "form"]]


def test_unnamed_and_duplicate_siblings_get_unique_labels():
    tree = Node(
        "http://a.com/",
        children=[Node("http://a.com/1"), Node("http://a.com/2", "x"), Node("http://a.com/3", "x")],
    )
    assert [f.path[-1] for f in _walk(tree)[1:]] == ["iframe[0]", "x", "x#2"]


def test_cross_origin_frame_and_everything_below_it_is_flagged():
    tree = Node(
        "http://a.com/",
        children=[
            Node("http://b.com/", "foreign", [Node("http://a.com/back", "nested")]),
            Node("http://a.com/ok", "ok"),
        ],
    )
    flags = {f.path[-1]: f.same_origin for f in _walk(tree)}
    assert flags == {"main": True, "foreign": False, "nested": False, "ok": True}


def test_about_blank_inherits_parent_origin():
    same = _walk(Node("http://a.com/", children=[Node("about:blank", "blank")]))[1]
    under_foreign = _walk(
        Node("http://a.com/", children=[Node("http://b.com/", "f", [Node("about:blank", "blank")])])
    )[2]
    assert same.same_origin is True
    assert under_foreign.same_origin is False


def test_collect_elements_tags_iframe_path_and_skips_cross_origin_and_failures():
    frames = [
        Frame(["main"], "u"),
        Frame(["main", "a"], "u"),
        Frame(["main", "foreign"], "u", same_origin=False),
        Frame(["main", "broken"], "u"),
    ]

    def run(frame):
        if frame.path[-1] == "broken":
            raise RuntimeError("frame detached")
        return [{"tag": "div", "id": "n-1"}]

    elements = collect_elements(frames, run)
    assert [e.iframe_path for e in elements] == [["main"], ["main", "a"]]
    assert elements[0].id_normalized == "n-{n}"
