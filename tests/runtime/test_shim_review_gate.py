"""Every registered shim must be reviewed before its ``review_by`` release ships.

A shim is a patch over a model gap that the flywheel is expected to retire
(``docs/SHIMS.md``). This gate reads every ``register_shim(...)`` call in the
package statically, so a shim in a module nothing imports at test time is
still checked, and fails once the package version reaches a shim's review
release. The fix is to delete the shim or re-justify it with a later release.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest
from packaging.version import Version

from omnigent.version import VERSION

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "omnigent"
_REQUIRED_KEYWORDS = ("reason", "model_gap", "added_in", "review_by", "owner")


@dataclass(frozen=True)
class _ShimCall:
    name: str
    location: str
    keywords: dict[str, str]


def _string_value(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _collect_shim_calls(package_root: Path) -> list[_ShimCall]:
    """Return every literal ``register_shim`` call under ``package_root``."""
    calls: list[_ShimCall] = []
    for path in sorted(package_root.rglob("*.py")):
        if path.name == "shims.py" and path.parent.name == "runtime":
            continue
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if func_name != "register_shim" or not node.args:
                continue
            name = _string_value(node.args[0])
            keywords = {
                keyword.arg: value
                for keyword in node.keywords
                if keyword.arg and (value := _string_value(keyword.value)) is not None
            }
            location = f"{path.relative_to(package_root.parent)}:{node.lineno}"
            calls.append(
                _ShimCall(name=name or "<non-literal>", location=location, keywords=keywords)
            )
    return calls


_SHIM_CALLS = _collect_shim_calls(_PACKAGE_ROOT)


def test_the_package_registers_shims() -> None:
    # Guards the scanner itself: an empty result would make every check vacuous.
    assert _SHIM_CALLS, "no register_shim(...) calls found under omnigent/"


@pytest.mark.parametrize("shim", _SHIM_CALLS, ids=lambda shim: shim.name)
def test_shim_is_registered_with_literal_metadata(shim: _ShimCall) -> None:
    assert shim.name != "<non-literal>", f"{shim.location}: shim name must be a string literal"
    missing = [key for key in _REQUIRED_KEYWORDS if not shim.keywords.get(key)]
    assert not missing, f"{shim.location}: {shim.name} is missing literal {missing}"
    Version(shim.keywords["added_in"])
    Version(shim.keywords["review_by"])


@pytest.mark.parametrize("shim", _SHIM_CALLS, ids=lambda shim: shim.name)
def test_shim_review_release_has_not_arrived(shim: _ShimCall) -> None:
    current_release = Version(Version(VERSION).base_version)
    review_release = Version(shim.keywords["review_by"])
    assert current_release < review_release, (
        f"{shim.location}: shim {shim.name!r} was due for review in {review_release} and "
        f"the package is now {VERSION}. Check GET /v1/shims: delete it if it no longer "
        "fires, otherwise re-justify it with a later review_by."
    )


def test_shim_names_are_unique() -> None:
    names = [shim.name for shim in _SHIM_CALLS]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"shim names registered more than once: {duplicates}"


def test_gate_flags_an_overdue_shim(tmp_path: Path) -> None:
    module = tmp_path / "omnigent" / "patched.py"
    module.parent.mkdir()
    module.write_text(
        'SHIM = register_shim("demo.overdue", reason="r", model_gap="g", '
        'added_in="0.1.0", review_by="0.2.0", owner="runtime")\n',
        "utf-8",
    )
    (found,) = _collect_shim_calls(module.parent)
    assert found.name == "demo.overdue"
    with pytest.raises(AssertionError, match=r"was due for review in 0\.2\.0"):
        test_shim_review_release_has_not_arrived(found)
