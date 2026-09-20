"""What each role can reach: pages and elements, compared across a multi-role run.

Every role of a run crawls the site on its own, into ``<output>/roles/<role>/``. This compares the
results and answers two questions for each pair of roles: *which pages* can one reach that the other
cannot, and on the pages both reach, *which elements* does one see that the other does not.

An element here is a **visible, actionable** one (``healix.healing.roles.is_healable``: a button,
link, field or anything with a test id), identified by its element role (``button:delete-user``),
the same name a fingerprint is stored under. So the diff is about controls a person could use, not
about markup. Nothing is compared by text or position, and an element a role can see but that has no
stable name is compared as its kind alone (``button``), which is coarse.

A page counts as reached by a role when its manifest has the URL (as an entry or as a variant of
one) and it did not fail to load. Elements are compared only for URLs both roles extracted to a file
of their own; a URL one role recorded as a variant of another page has no elements to compare.

The result is written to ``roles-diff.json`` and holds URLs and element names from the output
files, so treat it as sensitive in the same way.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from healix.discovery.manifest import FAILED, Manifest, ManifestPage
from healix.driver.base import Element
from healix.fs import write_json_atomic
from healix.healing.roles import assign_roles
from healix.log import get_logger
from healix.timeutil import iso_utc, utc_now

logger = get_logger(__name__)

DIFF_FILENAME = "roles-diff.json"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RoleOutput:
    """One role's finished crawl: its manifest and the folder its page files are in."""

    manifest: Manifest
    directory: Path


@dataclass
class RoleDiff:
    """The comparison. ``to_dict`` is what ``roles-diff.json`` holds."""

    run_id: str
    roles: list[str]
    reached: dict[str, int]
    in_all_roles: int
    page_differences: list[dict[str, Any]] = field(default_factory=list)
    element_differences: list[dict[str, Any]] = field(default_factory=list)
    unreadable: list[dict[str, str]] = field(default_factory=list)

    @property
    def differences(self) -> int:
        return len(self.page_differences) + len(self.element_differences)

    def only(self, role: str) -> dict[str, list[Any]]:
        """What only ``role`` has: the pages, and the ``(url, element)`` pairs."""
        return {
            "pages": [d["url"] for d in self.page_differences if d["roles"] == [role]],
            "elements": [
                [d["url"], d["element"]] for d in self.element_differences if d["roles"] == [role]
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "generated_at": iso_utc(utc_now()),
            "roles": self.roles,
            "summary": {
                "pages_reached": self.reached,
                "pages_in_all_roles": self.in_all_roles,
                "page_differences": len(self.page_differences),
                "element_differences": len(self.element_differences),
                "only": {
                    role: {kind: len(found) for kind, found in self.only(role).items()}
                    for role in self.roles
                },
            },
            "page_differences": self.page_differences,
            "element_differences": self.element_differences,
        }
        if self.unreadable:
            document["unreadable"] = self.unreadable
        return document


def reached_urls(manifest: Manifest) -> set[str]:
    """Every URL the role got to: its pages and their variants, minus pages that failed to load."""
    urls: set[str] = set()
    for page in manifest.pages:
        if page.status != FAILED:
            urls.add(page.url)
            urls.update(page.variant_urls)
    return urls


def _element_roles(directory: Path, page: ManifestPage) -> set[str] | None:
    """The element roles on an extracted page, or ``None`` if its file cannot be read."""
    if not page.output_file:
        return None
    try:
        document = json.loads((directory / page.output_file).read_text(encoding="utf-8"))
        elements = [
            Element.from_dict(raw, iframe_path=raw.get("iframe_path"))
            for raw in document["elements"]
        ]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warn("could not read a page file for the role diff", error=type(exc).__name__)
        return None
    return {role for role, _ in assign_roles(elements)}


def build_role_diff(outputs: Mapping[str, RoleOutput], *, run_id: str) -> RoleDiff:
    """Compare the roles in ``outputs`` (role name -> its crawl), in the order given."""
    roles = list(outputs)
    reached = {role: reached_urls(out.manifest) for role, out in outputs.items()}
    everything = sorted(set().union(*reached.values())) if reached else []
    everyone = set(roles)

    page_differences = []
    for url in everything:
        have = [role for role in roles if url in reached[role]]
        if set(have) != everyone:
            page_differences.append(
                {"url": url, "roles": have, "missing": [r for r in roles if r not in have]}
            )

    # Elements: only on pages that at least two roles extracted to a file of their own.
    representatives = {
        role: {p.url: p for p in out.manifest.pages if p.output_file and p.status != FAILED}
        for role, out in outputs.items()
    }
    element_differences: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    for url in sorted({u for reps in representatives.values() for u in reps}):
        present = [role for role in roles if url in representatives[role]]
        if len(present) < 2:
            continue
        seen: dict[str, set[str]] = {}
        for role in present:
            found = _element_roles(outputs[role].directory, representatives[role][url])
            if found is None:
                unreadable.append({"url": url, "role": role})
                continue
            seen[role] = found
        compared = [role for role in present if role in seen]
        if len(compared) < 2:
            continue
        for element in sorted(set().union(*(seen[role] for role in compared))):
            have = [role for role in compared if element in seen[role]]
            if len(have) != len(compared):
                element_differences.append(
                    {
                        "url": url,
                        "element": element,
                        "roles": have,
                        "missing": [r for r in compared if r not in have],
                    }
                )

    return RoleDiff(
        run_id=run_id,
        roles=roles,
        reached={role: len(urls) for role, urls in reached.items()},
        in_all_roles=len(set.intersection(*reached.values())) if reached else 0,
        page_differences=page_differences,
        element_differences=element_differences,
        unreadable=unreadable,
    )


def write_role_diff(diff: RoleDiff, output_root: str | Path) -> Path:
    """Write ``roles-diff.json`` under ``output_root``; the path."""
    path = Path(output_root) / DIFF_FILENAME
    write_json_atomic(path, diff.to_dict())
    return path
