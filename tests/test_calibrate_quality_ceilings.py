"""Tests for the generated-block write path in scripts/calibrate_quality_ceilings.py.

Only `write_constants` is covered: everything upstream of it needs the
network. The block it writes is the tool's one piece of season data frozen in
code, and `fpl doctor` reads the season back out of it (#128), so the write
path is what keeps that check describing reality after a rollover.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from fpl_cli.services.scoring.constants import Position

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load_script() -> ModuleType:
    path = _SCRIPTS_DIR / "calibrate_quality_ceilings.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before execution because the script defines dataclasses, and
    # @dataclass resolves annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_script()

_ANCHORS: dict[tuple[str, Position], float] = {
    (family, position): 10.0 + index
    for index, (family, position) in enumerate(
        (f, p) for f in _mod.FAMILIES for p in _mod.POSITIONS
    )
}


def _constants_copy(tmp_path: Path) -> Path:
    """A throwaway file carrying the real marker block, for the writer to rewrite."""
    real = Path(_mod.__file__).parent.parent / "fpl_cli/services/scoring/constants.py"
    copy = tmp_path / "constants.py"
    copy.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    return copy


def _block(path: Path) -> str:
    match = re.search(
        re.escape(_mod._BEGIN_MARK) + r".*?" + re.escape(_mod._END_MARK),
        path.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None
    return match.group(0)


class TestWriteConstants:
    def test_records_the_season_as_a_value_and_in_the_header(self, tmp_path):
        # The header prose is for a reader, the assignment is for `fpl doctor`
        # — one run writes both, so they can only drift by hand edit.
        path = _constants_copy(tmp_path)
        _mod.write_constants(_ANCHORS, "2024-25", [10, 38], path)
        block = _block(path)
        assert 'CALIBRATION_SEASON = "2024-25"' in block
        assert "against 2024-25" in block

    def test_recalibrating_moves_the_recorded_season(self, tmp_path):
        # The rollover this whole check exists for: re-running against the
        # newly completed season must leave no trace of the previous one.
        path = _constants_copy(tmp_path)
        _mod.write_constants(_ANCHORS, "2026-27", [10, 38], path)
        block = _block(path)
        assert 'CALIBRATION_SEASON = "2026-27"' in block
        assert "2025-26" not in block

    def test_written_block_is_readable_by_the_staleness_check(self, tmp_path):
        # End to end: what the writer emits is what season_start_year parses,
        # so the doctor check cannot be handed a label it raises on.
        from fpl_cli.season import season_start_year

        path = _constants_copy(tmp_path)
        _mod.write_constants(_ANCHORS, "2024-25", [10, 38], path)
        recorded = re.search(r'CALIBRATION_SEASON = "([^"]+)"', _block(path))
        assert recorded is not None
        assert season_start_year(recorded.group(1)) == 2024

    def test_refuses_a_season_that_is_not_a_label(self, tmp_path):
        path = _constants_copy(tmp_path)
        before = path.read_text(encoding="utf-8")
        with pytest.raises(SystemExit, match="calibration season"):
            _mod.write_constants(_ANCHORS, "2024", [10, 38], path)
        assert path.read_text(encoding="utf-8") == before

    def test_refuses_non_finite_anchors(self, tmp_path):
        path = _constants_copy(tmp_path)
        corrupt = {**_ANCHORS, ("target", "GK"): float("inf")}
        with pytest.raises(SystemExit, match="non-finite"):
            _mod.write_constants(corrupt, "2024-25", [10, 38], path)

    def test_missing_marker_block_is_refused(self, tmp_path):
        path = tmp_path / "constants.py"
        path.write_text("# nothing generated here\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="Marker block not found"):
            _mod.write_constants(_ANCHORS, "2024-25", [10, 38], path)
