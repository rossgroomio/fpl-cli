"""Tests for fpl chips command group and chip signal computation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.cli.chips import _compute_chip_signals, chips_group
from fpl_cli.models.chip_plan import ChipPlan, ChipType, PlannedChip, UsedChip


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def chip_plan_file(tmp_path: Path) -> Path:
    return tmp_path / "chip_plan.json"


# --- ChipPlan model tests ---


def _used(chip: str, gw: int) -> UsedChip:
    return UsedChip(chip=ChipType(chip), gameweek=gw)


class TestChipPlanModel:
    def test_empty_plan_has_all_chips_available(self):
        plan = ChipPlan()
        available = plan.get_available_chips(current_gw=1)
        assert len(available) == 4

    def test_one_use_first_half_available_in_second_half(self):
        plan = ChipPlan(chips_used=[_used("wildcard", 5)])
        available = plan.get_available_chips(current_gw=25)
        assert ChipType.WILDCARD in available

    def test_one_use_first_half_unavailable_in_first_half(self):
        plan = ChipPlan(chips_used=[_used("wildcard", 5)])
        available = plan.get_available_chips(current_gw=10)
        assert ChipType.WILDCARD not in available

    def test_gw19_is_first_half(self):
        plan = ChipPlan(chips_used=[_used("wildcard", 19)])
        available = plan.get_available_chips(current_gw=19)
        assert ChipType.WILDCARD not in available

    def test_gw20_is_second_half(self):
        plan = ChipPlan(chips_used=[_used("wildcard", 20)])
        available = plan.get_available_chips(current_gw=25)
        assert ChipType.WILDCARD not in available

    def test_used_both_halves_exhausted_everywhere(self):
        plan = ChipPlan(chips_used=[
            _used("wildcard", 5), _used("wildcard", 25),
        ])
        assert ChipType.WILDCARD not in plan.get_available_chips(current_gw=10)
        assert ChipType.WILDCARD not in plan.get_available_chips(current_gw=30)

    def test_all_exhausted_in_current_half(self):
        plan = ChipPlan(chips_used=[
            _used("wildcard", 21), _used("freehit", 22),
            _used("bboost", 23), _used("3xc", 24),
        ])
        assert plan.get_available_chips(current_gw=25) == []

    def test_load_missing_file(self, chip_plan_file: Path):
        plan = ChipPlan.load(chip_plan_file)
        assert plan.chips == []
        assert plan.chips_used == []

    def test_save_and_load_roundtrip(self, chip_plan_file: Path):
        plan = ChipPlan(
            chips=[PlannedChip(chip=ChipType.WILDCARD, gameweek=26, notes="DGW")],
            chips_used=[_used("bboost", 12)],
        )
        plan.save(chip_plan_file)
        loaded = ChipPlan.load(chip_plan_file)
        assert len(loaded.chips) == 1
        assert loaded.chips[0].gameweek == 26
        assert len(loaded.chips_used) == 1
        assert loaded.chips_used[0].chip == "bboost"
        assert loaded.chips_used[0].gameweek == 12

    def test_load_corrupt_file_returns_empty(self, chip_plan_file: Path):
        chip_plan_file.write_text("not json{{{", encoding="utf-8")
        plan = ChipPlan.load(chip_plan_file)
        assert plan.chips == []

    def test_cleanup_removes_exhausted_plan(self):
        plan = ChipPlan(
            chips=[PlannedChip(chip=ChipType.WILDCARD, gameweek=26)],
            chips_used=[_used("wildcard", 22)],
        )
        cleared = plan.cleanup_exhausted_plans()
        assert len(cleared) == 1
        assert cleared[0].chip == "wildcard"
        assert plan.chips == []

    def test_cleanup_preserves_plan_in_other_half(self):
        plan = ChipPlan(
            chips=[PlannedChip(chip=ChipType.WILDCARD, gameweek=26)],
            chips_used=[_used("wildcard", 8)],
        )
        cleared = plan.cleanup_exhausted_plans()
        assert cleared == []
        assert len(plan.chips) == 1


# --- CLI: bare `fpl chips` ---


class TestChipsBare:
    def test_no_file_shows_no_chips(self, runner: CliRunner):
        with patch.object(ChipPlan, "load", return_value=ChipPlan(current_gw=25)):
            result = runner.invoke(chips_group)
        assert result.exit_code == 0
        assert "No chips planned" in result.output
        assert "Chip Status" in result.output

    def test_with_planned_chips(self, runner: CliRunner):
        plan = ChipPlan(chips=[PlannedChip(chip=ChipType.WILDCARD, gameweek=26)], current_gw=25)
        with patch.object(ChipPlan, "load", return_value=plan):
            result = runner.invoke(chips_group)
        assert result.exit_code == 0
        assert "GW26" in result.output
        assert "Wildcard" in result.output

    def test_shows_used_chips_with_gameweek(self, runner: CliRunner):
        plan = ChipPlan(chips_used=[_used("bboost", 12), _used("wildcard", 5)], current_gw=25)
        with patch.object(ChipPlan, "load", return_value=plan):
            result = runner.invoke(chips_group)
        assert result.exit_code == 0
        assert "Bench Boost (GW12)" in result.output
        assert "Wildcard (GW5)" in result.output

    def test_section_order_available_used_planned(self, runner: CliRunner):
        plan = ChipPlan(
            chips=[PlannedChip(chip=ChipType.FREE_HIT, gameweek=30)],
            chips_used=[_used("wildcard", 22)],
            current_gw=25,
        )
        with patch.object(ChipPlan, "load", return_value=plan):
            result = runner.invoke(chips_group)
        assert result.exit_code == 0
        output = result.output
        avail_pos = output.index("Available:")
        used_pos = output.index("Used:")
        planned_pos = output.index("Planned:")
        assert avail_pos < used_pos < planned_pos

    def test_no_sync_shows_hint(self, runner: CliRunner):
        with patch.object(ChipPlan, "load", return_value=ChipPlan()):
            result = runner.invoke(chips_group)
        assert result.exit_code == 0
        assert "fpl chips sync" in result.output


# --- CLI: `fpl chips --format json` ---


class TestChipsJsonFormat:
    def test_json_output_is_valid(self, runner: CliRunner):
        plan = ChipPlan(
            chips=[PlannedChip(chip=ChipType.FREE_HIT, gameweek=30, notes="BGW")],
            chips_used=[_used("wildcard", 5)],
            current_gw=25,
        )
        with patch.object(ChipPlan, "load", return_value=plan):
            result = runner.invoke(main, ["chips", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "chips"
        assert data["metadata"]["gameweek"] == 25
        assert len(data["data"]["available"]) == 4  # WC used in first half, available again in second
        assert data["data"]["used"] == [{"chip": "wildcard", "gameweek": 5}]
        assert data["data"]["planned"] == [{"chip": "freehit", "gameweek": 30, "notes": "BGW"}]

    def test_json_all_chips_used(self, runner: CliRunner):
        plan = ChipPlan(
            chips_used=[
                _used("wildcard", 21), _used("freehit", 22),
                _used("bboost", 23), _used("3xc", 24),
            ],
            current_gw=25,
        )
        with patch.object(ChipPlan, "load", return_value=plan):
            result = runner.invoke(main, ["chips", "--format", "json"])
        data = json.loads(result.output)
        assert data["data"]["available"] == []

    def test_json_no_planned_chips(self, runner: CliRunner):
        plan = ChipPlan(current_gw=25)
        with patch.object(ChipPlan, "load", return_value=plan):
            result = runner.invoke(main, ["chips", "--format", "json"])
        data = json.loads(result.output)
        assert data["data"]["planned"] == []

    def test_json_current_gw_zero(self, runner: CliRunner):
        plan = ChipPlan()
        with patch.object(ChipPlan, "load", return_value=plan):
            result = runner.invoke(main, ["chips", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["metadata"]["gameweek"] == 0
        assert len(data["data"]["available"]) == 4


# --- CLI: `fpl chips add` ---


class TestChipsAdd:
    def test_add_valid_chip(self, runner: CliRunner):
        plan = ChipPlan(current_gw=25)
        with patch.object(ChipPlan, "load", return_value=plan), \
             patch.object(ChipPlan, "save"):
            result = runner.invoke(chips_group, ["add", "wildcard", "--gw", "26"])
        assert result.exit_code == 0
        assert "Planned Wildcard for GW26" in result.output

    def test_add_chip_already_exhausted(self, runner: CliRunner):
        plan = ChipPlan(
            chips_used=[_used("wildcard", 5), _used("wildcard", 25)],
            current_gw=25,
        )
        with patch.object(ChipPlan, "load", return_value=plan):
            result = runner.invoke(chips_group, ["add", "wildcard", "--gw", "26"])
        assert result.exit_code == 0
        assert "not available" in result.output

    def test_add_chip_gw_conflict(self, runner: CliRunner):
        plan = ChipPlan(
            chips=[PlannedChip(chip=ChipType.WILDCARD, gameweek=26)],
            current_gw=25,
        )
        with patch.object(ChipPlan, "load", return_value=plan):
            result = runner.invoke(chips_group, ["add", "freehit", "--gw", "26"])
        assert result.exit_code == 0
        assert "already has a chip planned" in result.output


# --- CLI: `fpl chips remove` ---


class TestChipsRemove:
    def test_remove_existing_chip(self, runner: CliRunner):
        plan = ChipPlan(chips=[PlannedChip(chip=ChipType.WILDCARD, gameweek=26)])
        with patch.object(ChipPlan, "load", return_value=plan), \
             patch.object(ChipPlan, "save"):
            result = runner.invoke(chips_group, ["remove", "--gw", "26"])
        assert result.exit_code == 0
        assert "Removed chip from GW26" in result.output

    def test_remove_missing_chip(self, runner: CliRunner):
        plan = ChipPlan()
        with patch.object(ChipPlan, "load", return_value=plan):
            result = runner.invoke(chips_group, ["remove", "--gw", "26"])
        assert result.exit_code == 0
        assert "No chip planned" in result.output


# --- CLI: `fpl chips sync` ---


class TestChipsSync:
    def test_sync_populates_chips_used(self, runner: CliRunner):
        plan = ChipPlan()
        mock_client = AsyncMock()
        mock_client.get_manager_history.return_value = {
            "chips": [
                {"name": "wildcard", "event": 5},
                {"name": "bboost", "event": 12},
            ]
        }
        mock_client.get_next_gameweek.return_value = {"id": 25}

        with patch.object(ChipPlan, "load", return_value=plan), \
             patch.object(ChipPlan, "save"), \
             patch("fpl_cli.cli.chips.get_settings", return_value={"fpl": {"classic_entry_id": 123}}), \
             patch("fpl_cli.api.fpl.FPLClient") as mock_fpl_cls:
            mock_fpl_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_fpl_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = runner.invoke(chips_group, ["sync"])

        assert result.exit_code == 0
        assert "Synced 2 used chips" in result.output
        assert "Wildcard (GW5)" in result.output
        assert "Bench Boost (GW12)" in result.output

    def test_sync_auto_cleanup(self, runner: CliRunner):
        plan = ChipPlan(
            chips=[PlannedChip(chip=ChipType.WILDCARD, gameweek=26)],
        )
        mock_client = AsyncMock()
        mock_client.get_manager_history.return_value = {
            "chips": [{"name": "wildcard", "event": 22}],
        }
        mock_client.get_next_gameweek.return_value = {"id": 25}

        with patch.object(ChipPlan, "load", return_value=plan), \
             patch.object(ChipPlan, "save"), \
             patch("fpl_cli.cli.chips.get_settings", return_value={"fpl": {"classic_entry_id": 123}}), \
             patch("fpl_cli.api.fpl.FPLClient") as mock_fpl_cls:
            mock_fpl_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_fpl_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = runner.invoke(chips_group, ["sync"])

        assert result.exit_code == 0
        assert "Cleared planned Wildcard GW26" in result.output
        assert len(plan.chips) == 0

    def test_sync_preserves_plan_in_other_half(self, runner: CliRunner):
        """A first-half wildcard use should not clear a second-half plan."""
        plan = ChipPlan(
            chips=[PlannedChip(chip=ChipType.WILDCARD, gameweek=26)],
        )
        mock_client = AsyncMock()
        mock_client.get_manager_history.return_value = {
            "chips": [{"name": "wildcard", "event": 8}],
        }
        mock_client.get_next_gameweek.return_value = {"id": 25}

        with patch.object(ChipPlan, "load", return_value=plan), \
             patch.object(ChipPlan, "save"), \
             patch("fpl_cli.cli.chips.get_settings", return_value={"fpl": {"classic_entry_id": 123}}), \
             patch("fpl_cli.api.fpl.FPLClient") as mock_fpl_cls:
            mock_fpl_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_fpl_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = runner.invoke(chips_group, ["sync"])

        assert result.exit_code == 0
        assert "Cleared" not in result.output
        assert len(plan.chips) == 1

    def test_sync_no_entry_id(self, runner: CliRunner):
        with patch("fpl_cli.cli.chips.get_settings", return_value={"fpl": {}}):
            result = runner.invoke(chips_group, ["sync"])
        assert result.exit_code == 0
        assert "classic_entry_id not configured" in result.output


# --- Chip signal computation (migrated from test_cli_chip_timing.py) ---


def make_exposure(gw: int, gw_type: str, affected: int, players: list[str], source: str = "confirmed") -> dict:
    return {
        "gw": gw,
        "type": gw_type,
        "affected": affected,
        "starters": min(affected, 11),
        "players": players,
        "source": source,
    }


def make_fdr_by_team(
    team_fixtures: dict[str, list[tuple[int, str, float]]] | None = None,
) -> dict[str, dict]:
    """Build a FixtureAgent-shaped ``fdr_by_team`` map.

    team_fixtures: team_short -> [(gameweek, opponent_short, fdr)]. The ``fdr``
    is that fixture's general FDR for the team - the venue-aware ATK/DEF mean
    the agent records - so a double against weak opponents is a low number.
    ``fdr_by_gameweek`` is derived here the way the agent derives it, so these
    tests exercise the real shape. Teams absent from the map have no fixtures.
    """
    team_fixtures = team_fixtures or {}
    result: dict[str, dict] = {}

    for short, fixtures in team_fixtures.items():
        by_gw: dict[int, list[float]] = {}
        for gw, _opponent, fdr in fixtures:
            by_gw.setdefault(gw, []).append(fdr)

        result[short] = {
            "team_name": short,
            "fixtures": [
                {
                    "gameweek": gw,
                    "opponent": opponent,
                    "is_home": True,
                    "fdr": fdr,
                    "fdr_atk": fdr,
                    "fdr_def": fdr,
                }
                for gw, opponent, fdr in fixtures
            ],
            "fdr_by_gameweek": {
                gw: {"fdr": round(sum(v) / len(v), 2), "fixture_count": len(v)}
                for gw, v in by_gw.items()
            },
        }

    return result


def rated(*teams: str) -> set[str]:
    """The clubs the ratings file knows about, for the TC coverage check."""
    return set(teams)


class TestFHSignals:
    def test_fh_strong_at_5_affected(self):
        exposure = [make_exposure(28, "blank", 5, ["Salah", "Trent", "Arnold", "Nunez", "VVD"])]
        signals = _compute_chip_signals(exposure, {"freehit"}, {}, {}, make_fdr_by_team(), set())
        assert len(signals) == 1
        assert signals[0]["signal"] == "FH"
        assert signals[0]["strength"] == "strong"

    def test_fh_possible_at_4_affected(self):
        exposure = [make_exposure(28, "blank", 4, ["Salah", "Trent", "Arnold", "Nunez"])]
        signals = _compute_chip_signals(exposure, {"freehit"}, {}, {}, make_fdr_by_team(), set())
        assert len(signals) == 1
        assert signals[0]["strength"] == "possible"

    def test_fh_possible_at_3_affected(self):
        exposure = [make_exposure(28, "blank", 3, ["Salah", "Trent", "Arnold"])]
        signals = _compute_chip_signals(exposure, {"freehit"}, {}, {}, make_fdr_by_team(), set())
        assert len(signals) == 1
        assert signals[0]["strength"] == "possible"

    def test_fh_no_signal_at_2_affected(self):
        exposure = [make_exposure(28, "blank", 2, ["Salah", "Trent"])]
        signals = _compute_chip_signals(exposure, {"freehit"}, {}, {}, make_fdr_by_team(), set())
        assert signals == []

    def test_fh_suppressed_when_chip_used(self):
        exposure = [make_exposure(28, "blank", 6, ["a", "b", "c", "d", "e", "f"])]
        signals = _compute_chip_signals(exposure, set(), {}, {}, make_fdr_by_team(), set())
        assert all(s["signal"] != "FH" for s in signals)


class TestBBSignals:
    def test_bb_strong_at_8_starters(self):
        players = [f"p{i}" for i in range(9)]
        exposure = [make_exposure(31, "double", 8, players)]
        signals = _compute_chip_signals(exposure, {"bboost"}, {}, {}, make_fdr_by_team(), set())
        bb = [s for s in signals if s["signal"] == "BB"]
        assert len(bb) == 1
        assert bb[0]["strength"] == "strong"

    def test_bb_possible_at_6_starters(self):
        players = [f"p{i}" for i in range(6)]
        exposure = [make_exposure(31, "double", 6, players)]
        signals = _compute_chip_signals(exposure, {"bboost"}, {}, {}, make_fdr_by_team(), set())
        bb = [s for s in signals if s["signal"] == "BB"]
        assert len(bb) == 1
        assert bb[0]["strength"] == "possible"

    def test_bb_no_signal_at_5_starters(self):
        players = [f"p{i}" for i in range(5)]
        exposure = [make_exposure(31, "double", 5, players)]
        signals = _compute_chip_signals(exposure, {"bboost"}, {}, {}, make_fdr_by_team(), set())
        assert all(s["signal"] != "BB" for s in signals)


class TestTCSignals:
    def test_tc_strong_when_double_averages_leq_3(self):
        exposure = [make_exposure(31, "double", 7, ["Salah", "Diogo"])]
        name_to_tid = {"Salah": 14, "Diogo": 14}
        id_to_short = {14: "LIV"}
        # Two soft opponents: mean 2.5
        fdr = make_fdr_by_team({"LIV": [(31, "SOU", 2.0), (31, "BUR", 3.0)]})
        signals = _compute_chip_signals(
            exposure, {"3xc"}, name_to_tid, id_to_short, fdr, rated("LIV"),
        )
        tc = [s for s in signals if s["signal"] == "TC"]
        assert len(tc) == 1
        assert tc[0]["strength"] == "strong"
        assert tc[0]["detail"] == "Salah (FDR 2.5)"
        assert tc[0]["fixtures_scored"] == 2

    def test_tc_possible_when_double_averages_leq_4(self):
        exposure = [make_exposure(31, "double", 7, ["Saka"])]
        fdr = make_fdr_by_team({"ARS": [(31, "CHE", 3.5), (31, "NEW", 4.5)]})
        signals = _compute_chip_signals(
            exposure, {"3xc"}, {"Saka": 3}, {3: "ARS"}, fdr, rated("ARS"),
        )
        tc = [s for s in signals if s["signal"] == "TC"]
        assert len(tc) == 1
        assert tc[0]["strength"] == "possible"

    def test_tc_no_signal_when_double_averages_above_4(self):
        exposure = [make_exposure(31, "double", 7, ["Saka"])]
        fdr = make_fdr_by_team({"ARS": [(31, "MCI", 5.5), (31, "LIV", 5.0)]})
        signals = _compute_chip_signals(
            exposure, {"3xc"}, {"Saka": 3}, {3: "ARS"}, fdr, rated("ARS"),
        )
        assert all(s["signal"] != "TC" for s in signals)

    def test_tc_picks_easiest_double_not_weakest_club(self):
        """The candidate at the stronger club wins when their double is easier.

        Regression for #201: scoring each candidate on their own club's
        inverted rating handed the signal to the weakest club in the league.
        """
        exposure = [make_exposure(31, "double", 7, ["Salah", "Wood"])]
        name_to_tid = {"Salah": 14, "Wood": 16}
        id_to_short = {14: "LIV", 16: "NFO"}
        fdr = make_fdr_by_team({
            "LIV": [(31, "SOU", 2.0), (31, "BUR", 2.4)],   # mean 2.2
            "NFO": [(31, "MCI", 5.5), (31, "ARS", 5.5)],   # mean 5.5
        })
        signals = _compute_chip_signals(
            exposure, {"3xc"}, name_to_tid, id_to_short, fdr, rated("LIV", "NFO"),
        )
        tc = [s for s in signals if s["signal"] == "TC"]
        assert len(tc) == 1
        assert tc[0]["detail"] == "Salah (FDR 2.2)"

    def test_tc_scores_only_the_doubles_own_gameweek(self):
        """An easy fixture in another GW must not grade this GW's double."""
        exposure = [make_exposure(31, "double", 7, ["Saka"])]
        fdr = make_fdr_by_team({
            "ARS": [(30, "BUR", 1.5), (31, "MCI", 5.5), (31, "LIV", 5.0)],
        })
        signals = _compute_chip_signals(
            exposure, {"3xc"}, {"Saka": 3}, {3: "ARS"}, fdr, rated("ARS"),
        )
        assert all(s["signal"] != "TC" for s in signals)

    def test_tc_skips_candidate_with_no_fixtures_in_gameweek(self):
        """A predicted double beyond the FDR window is not scoreable."""
        exposure = [make_exposure(35, "double", 7, ["Saka"], source="predicted")]
        fdr = make_fdr_by_team({"ARS": [(31, "BUR", 1.5)]})
        signals = _compute_chip_signals(
            exposure, {"3xc"}, {"Saka": 3}, {3: "ARS"}, fdr, rated("ARS"),
        )
        assert all(s["signal"] != "TC" for s in signals)

    def test_tc_skips_candidate_whose_team_has_no_fdr_entry(self):
        """A team absent from fdr_by_team altogether has nothing to score."""
        exposure = [make_exposure(31, "double", 7, ["Saka"])]
        signals = _compute_chip_signals(
            exposure, {"3xc"}, {"Saka": 3}, {3: "ARS"}, make_fdr_by_team(), rated("ARS"),
        )
        assert all(s["signal"] != "TC" for s in signals)

    def test_tc_skips_unrated_club_rather_than_grading_the_neutral_4(self):
        """An unrated club scores a placeholder 4.0, which would clear <= 4.0.

        At the season rollover a ratings file can pass every date check while
        knowing nothing about the promoted clubs, and every fixture involving
        one is scored at the ratings service's neutral 4.0. Grading that would
        fire "possible" off a placeholder rather than a fixture read.
        """
        exposure = [make_exposure(31, "double", 7, ["Wood"])]
        fdr = make_fdr_by_team({"NFO": [(31, "BUR", 4.0), (31, "SOU", 4.0)]})
        signals = _compute_chip_signals(
            exposure, {"3xc"}, {"Wood": 16}, {16: "NFO"}, fdr, rated(),
        )
        assert all(s["signal"] != "TC" for s in signals)

        # The same double fires once the club is rated.
        signals = _compute_chip_signals(
            exposure, {"3xc"}, {"Wood": 16}, {16: "NFO"}, fdr, rated("NFO"),
        )
        assert [s["strength"] for s in signals if s["signal"] == "TC"] == ["possible"]

    def test_tc_flags_a_double_scored_on_one_scheduled_fixture(self):
        """A predicted double is only scoreable on the leg already scheduled.

        The other leg has no fixture to average, so the figure is one match,
        not the pair - the detail has to say so rather than present it as the
        double's mean.
        """
        exposure = [make_exposure(31, "double", 7, ["Saka"], source="predicted")]
        fdr = make_fdr_by_team({"ARS": [(31, "BUR", 2.0)]})
        signals = _compute_chip_signals(
            exposure, {"3xc"}, {"Saka": 3}, {3: "ARS"}, fdr, rated("ARS"),
        )
        tc = [s for s in signals if s["signal"] == "TC"]
        assert len(tc) == 1
        assert tc[0]["detail"] == "Saka (FDR 2.0, 1 of 2 scheduled)"
        assert tc[0]["fixtures_scored"] == 1

    def test_tc_suppressed_when_chip_used(self):
        exposure = [make_exposure(31, "double", 7, ["Saka"])]
        fdr = make_fdr_by_team({"ARS": [(31, "BUR", 1.5), (31, "SOU", 1.5)]})
        signals = _compute_chip_signals(
            exposure, {"bboost"}, {"Saka": 3}, {3: "ARS"}, fdr, rated("ARS"),
        )
        assert all(s["signal"] != "TC" for s in signals)


# --- CLI: `fpl chips timing --format json` ---


_SENTINEL = object()


def _mock_fetch_and_compute(unplayed=None, planned_by_gw=None, signals=_SENTINEL):
    """Return an AsyncMock for _fetch_and_compute with given return values."""
    return AsyncMock(return_value=(
        unplayed or {"freehit", "bboost"},
        planned_by_gw or {},
        signals if signals is not _SENTINEL else [],
    ))


class TestChipsTimingJsonFormat:
    def test_json_output_with_signals(self, runner: CliRunner):
        plan = ChipPlan(
            chips=[PlannedChip(chip=ChipType.BENCH_BOOST, gameweek=33)],
            current_gw=30,
        )
        signals = [
            {"gw": 28, "signal": "FH", "strength": "strong", "source": "confirmed",
             "players": ["Salah", "Trent"], "detail": "5 squad players blank"},
        ]
        mock_client = AsyncMock()
        mock_client.get_next_gameweek.return_value = {"id": 30}

        with patch.object(ChipPlan, "load", return_value=plan), \
             patch("fpl_cli.cli.chips.get_settings", return_value={"fpl": {"classic_entry_id": 123}}), \
             patch("fpl_cli.api.fpl.FPLClient") as mock_fpl_cls, \
             patch("fpl_cli.cli.chips._fetch_and_compute", _mock_fetch_and_compute(
                 unplayed={"freehit", "bboost"}, signals=signals,
             )):
            mock_fpl_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_fpl_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = runner.invoke(main, ["chips", "timing", "--format", "json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "chips-timing"
        assert data["metadata"]["gameweek"] == 30
        assert data["metadata"]["unplayed"] == ["bboost", "freehit"]
        assert data["metadata"]["planned"] == [{"chip": "bboost", "gameweek": 33}]
        assert len(data["data"]) == 1
        assert data["data"][0]["signal"] == "FH"

    def test_json_no_signals(self, runner: CliRunner):
        plan = ChipPlan(current_gw=30)
        mock_client = AsyncMock()
        mock_client.get_next_gameweek.return_value = {"id": 30}

        with patch.object(ChipPlan, "load", return_value=plan), \
             patch("fpl_cli.cli.chips.get_settings", return_value={"fpl": {"classic_entry_id": 123}}), \
             patch("fpl_cli.api.fpl.FPLClient") as mock_fpl_cls, \
             patch("fpl_cli.cli.chips._fetch_and_compute", _mock_fetch_and_compute()):
            mock_fpl_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_fpl_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = runner.invoke(main, ["chips", "timing", "--format", "json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"] == []

    def test_json_error_no_entry_id(self, runner: CliRunner):
        with patch("fpl_cli.cli.chips.get_settings", return_value={"fpl": {}}):
            result = runner.invoke(main, ["chips", "timing", "--format", "json"])
        assert result.exit_code == 1
        # #141: the error envelope rides stdout, same as the success envelope.
        payload = json.loads(result.stdout)
        assert payload["command"] == "chips-timing"
        assert "classic_entry_id" in payload["error"]
        assert "{" not in result.stderr

    def test_json_error_agent_failure(self, runner: CliRunner):
        plan = ChipPlan(current_gw=30)
        mock_client = AsyncMock()
        mock_client.get_next_gameweek.return_value = {"id": 30}

        with patch.object(ChipPlan, "load", return_value=plan), \
             patch("fpl_cli.cli.chips.get_settings", return_value={"fpl": {"classic_entry_id": 123}}), \
             patch("fpl_cli.api.fpl.FPLClient") as mock_fpl_cls, \
             patch("fpl_cli.cli.chips._fetch_and_compute", _mock_fetch_and_compute(signals=None)):
            mock_fpl_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_fpl_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = runner.invoke(main, ["chips", "timing", "--format", "json"])

        assert result.exit_code == 1

    def test_table_agent_failure_exits_nonzero(self, runner: CliRunner):
        """Table-mode agent failure exits nonzero and reports on stderr.

        Nonzero rather than printing and succeeding (#47), and on stderr
        rather than stdout (#162). This is the only test that reaches that
        print: the contract walk in `test_cli_failure_streams.py` cannot,
        because with no `classic_entry_id` configured `chips timing` stops at
        the not-configured warning and exits 0. Asserting on `result.output`
        would not hold it either -- Click mixes both streams into that one.
        """
        plan = ChipPlan(current_gw=30)
        mock_client = AsyncMock()
        mock_client.get_next_gameweek.return_value = {"id": 30}

        with patch.object(ChipPlan, "load", return_value=plan), \
             patch("fpl_cli.cli.chips.get_settings", return_value={"fpl": {"classic_entry_id": 123}}), \
             patch("fpl_cli.api.fpl.FPLClient") as mock_fpl_cls, \
             patch("fpl_cli.cli.chips._fetch_and_compute", _mock_fetch_and_compute(signals=None)):
            mock_fpl_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_fpl_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = runner.invoke(main, ["chips", "timing"])

        assert result.exit_code == 1
        assert "Agent failed" in result.stderr
        assert result.stdout == ""


class TestNoSignals:
    def test_empty_exposure_produces_no_signals(self):
        signals = _compute_chip_signals([], {"freehit", "bboost", "3xc"}, {}, {}, make_fdr_by_team(), set())
        assert signals == []
