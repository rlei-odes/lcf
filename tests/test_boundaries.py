"""Architectural boundaries, asserted rather than hoped for.

ARCHITECTURE §3 says spec/ and engine/ know nothing beyond data. The value of that
rule is that the whole deterministic core stays testable without a database — which
is only true for as long as nobody imports one into it.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src/lcf"

FORBIDDEN = {
    "spec": ("sqlalchemy", "boto3", "lcf.models", "lcf.services", "lcf.storage"),
    "engine": ("sqlalchemy", "boto3", "lcf.models", "lcf.services", "lcf.storage"),
}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def python_files(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_pure_packages_stay_pure(package):
    offences = []
    for path in python_files(package):
        for imported in imports_of(path):
            for banned in FORBIDDEN[package]:
                if imported == banned or imported.startswith(banned + "."):
                    offences.append(f"{path.relative_to(SRC)} imports {imported}")
    assert offences == [], offences


def test_spec_does_not_import_engine():
    """The spec is data. It must not depend on the thing that interprets it."""
    for path in python_files("spec"):
        assert not any(i.startswith("lcf.engine") for i in imports_of(path))


def test_engine_may_import_spec():
    """The dependency is allowed in exactly one direction — confirm it exists."""
    found = {i for path in python_files("engine") for i in imports_of(path)}
    assert any(i.startswith("lcf.spec") for i in found)


def test_deterministic_checks_import_nothing_heavy():
    """The check module in particular: a dict in, a result out."""
    heavy = {"sqlalchemy", "boto3", "openai", "httpx", "loguru"}
    found = imports_of(SRC / "engine/checks/deterministic.py")
    assert not (found & heavy), found & heavy
