"""The dependency policy (#81): no lockfile, and every upper bound is deliberate.

A ``requirements.lock`` used to sit at the repo root looking like a pinning
strategy and wasn't one: three runtime dependencies short, generated on macOS
for Python 3.12 so a hash-checked install refused on the Linux/3.11 CI
runner, and read by nothing. Every install resolves fresh from
``pyproject.toml`` -- the resolution ``pip install fplkit`` gives a user -- so
CI tests what users get, and protection for users is an upper bound on the
few dependencies whose next major is a real prospect. The first test keeps a
lockfile from reappearing. The rest hold the table of bounds in
``CONTRIBUTING.md`` to ``pyproject.toml`` in both directions, so a bound
cannot be added without its reason, nor documented without existing.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

# Every lockfile format a Python project might grow. None of them is read by
# anything here, so any one of them is the defect #81 described.
LOCKFILES = [
    "requirements.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "pdm.lock",
]

# `name[extra] specifiers ; marker` -> (name, specifiers). PEP 508 names may
# carry dots, hyphens and underscores; the marker is irrelevant to the bound.
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*([^;]*)")

# A table row in CONTRIBUTING.md's Dependencies section: | `name<bound` | why |
_TABLE_ROW = re.compile(r"^\| `([A-Za-z0-9._-]+)(<[^`]+)` \|", re.MULTILINE)


def _runtime_requirements() -> list[str]:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]["dependencies"]


def _upper_bounds(requirements: list[str]) -> dict[str, str]:
    """Map each requirement that carries an upper bound to that clause."""
    bounds: dict[str, str] = {}
    for requirement in requirements:
        match = _REQUIREMENT.match(requirement)
        assert match is not None, f"unparseable requirement: {requirement!r}"
        name, specifiers = match.group(1).lower(), match.group(2)
        for clause in (c.strip() for c in specifiers.split(",")):
            # `<`, `<=`, `==`, `~=` all cap the version; `>=`, `>`, `!=` do not.
            if clause.startswith(("<", "==", "~=")):
                bounds[name] = clause
    return bounds


def _documented_bounds() -> dict[str, str]:
    text = CONTRIBUTING.read_text(encoding="utf-8")
    start = text.index("## Dependencies")
    end = text.index("\n## ", start + 1)
    return {name.lower(): bound for name, bound in _TABLE_ROW.findall(text[start:end])}


class TestNoLockfile:
    @pytest.mark.parametrize("name", LOCKFILES)
    def test_no_lockfile_checked_in(self, name: str):
        assert not (REPO_ROOT / name).exists(), (
            f"{name} is read by nothing -- every install resolves from pyproject.toml. "
            "See CONTRIBUTING.md (Dependencies) and #81."
        )


class TestUpperBoundsAreDocumented:
    """Each bound in pyproject.toml has a row in CONTRIBUTING.md, and vice versa."""

    def test_the_scan_finds_the_table(self):
        # Guard the guard: an empty table would make the equality below vacuous.
        assert _documented_bounds(), "no `name<bound` rows found under ## Dependencies in CONTRIBUTING.md"

    def test_documented_bounds_match_pyproject(self):
        assert _documented_bounds() == _upper_bounds(_runtime_requirements())

    def test_every_bound_is_an_upper_bound(self):
        # The table documents ceilings; a floor or a pin there would be a different policy.
        for name, bound in _upper_bounds(_runtime_requirements()).items():
            assert bound.startswith("<"), f"{name} is pinned with {bound!r}, not capped"


class TestRequirementParser:
    """The parser is a regex; pin the shapes it must and must not read as a bound."""

    def test_reads_every_operator_that_caps(self):
        reqs = ["a>=1,<2", "b<=3", "c==4.0", "d~=5.1", "e[extra]>=1,<2; python_version < '3.12'"]
        assert _upper_bounds(reqs) == {"a": "<2", "b": "<=3", "c": "==4.0", "d": "~=5.1", "e": "<2"}

    def test_ignores_floors_and_exclusions(self):
        assert _upper_bounds(["a>=1", "b>1,!=1.5", "c", "d; platform_system == 'Windows'"]) == {}
