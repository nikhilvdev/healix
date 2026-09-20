"""The import rules in ARCHITECTURE.md, checked against the source itself."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parents[1] / "healix"
BROWSER_LIBRARIES = {"playwright", "selenium"}
ADAPTERS = {"healix/driver/playwright_adapter.py", "healix/driver/selenium_adapter.py"}
ADAPTER_MODULES = {"healix.driver.playwright_adapter", "healix.driver.selenium_adapter"}
# The only modules that may name an adapter module: the factory, and the lazy exports.
MAY_NAME_ADAPTERS = {"healix/driver/factory.py", "healix/driver/__init__.py"}


def modules():
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path.relative_to(PACKAGE.parent).as_posix(), ast.parse(path.read_text())


def imported_names(tree):
    """Every module name imported anywhere in ``tree`` (including inside functions)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def test_only_the_two_adapters_import_a_browser_library():
    offenders = {
        name: sorted({m for m in imported_names(tree) if m.split(".")[0] in BROWSER_LIBRARIES})
        for name, tree in modules()
        if name not in ADAPTERS
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_the_adapters_do_import_their_libraries():
    """If this fails, the test above has stopped meaning anything."""
    found = {
        name: {m.split(".")[0] for m in imported_names(tree)} & BROWSER_LIBRARIES
        for name, tree in modules()
        if name in ADAPTERS
    }
    assert found == {
        "healix/driver/playwright_adapter.py": {"playwright"},
        "healix/driver/selenium_adapter.py": {"selenium"},
    }


def test_only_the_factory_and_the_lazy_exports_name_an_adapter_module():
    offenders = {
        name
        for name, tree in modules()
        if name not in ADAPTERS | MAY_NAME_ADAPTERS and ADAPTER_MODULES & set(imported_names(tree))
    }
    assert offenders == set()


@pytest.mark.parametrize(
    "package", ["classification", "discovery", "extraction", "auth", "healing", "generation"]
)
def test_domain_packages_do_not_import_the_cli_or_the_public_wrappers(package):
    """Domain logic sits below the SDK, the healer wrapper and the CLI."""
    above = {"healix.cli", "healix.sdk", "healix.healer", "healix.doctor"}
    allowed = {"healix/generation/script_writer.py"}  # it accepts an SDK ``Run`` as its source
    offenders = {
        name: sorted(above & set(imported_names(tree)))
        for name, tree in modules()
        if name.startswith(f"healix/{package}/")
        and name not in allowed
        and above & set(imported_names(tree))
    }
    assert offenders == {}
