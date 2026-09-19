"""A synthetic ``Element`` factory for tests that don't need a browser."""

from __future__ import annotations

import itertools

from healix.driver.base import Element

_ids = itertools.count(1)


def el(
    tag,
    *,
    text=None,
    sel=None,
    classes=(),
    id=None,
    name=None,
    visible=True,
    enabled=True,
    iframe=("main",),
    **attrs,
):
    """An Element. ``attrs`` become HTML attributes (``aria_label`` -> ``aria-label``)."""
    return Element.from_dict(
        {
            "tag": tag,
            "id": id,
            "name": name,
            "classes": list(classes),
            "attributes": {k.replace("_", "-"): v for k, v in attrs.items()},
            "text_content": text,
            "computed": {"visible": visible, "enabled": enabled},
            "css_selector": sel or f"html > body > {tag}:nth-of-type({next(_ids)})",
        },
        iframe_path=list(iframe),
    )
