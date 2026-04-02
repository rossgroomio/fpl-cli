"""Tests for `fpl status` command."""

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.models.player import PlayerStatus
from tests.conftest import make_player, make_team


def _mock_client(
    current_gw=None, next_gw=None, manager_entry=None,
    history=None, picks=None, players=None, teams=None,
    classic_league_standings=None, gameweek_live=None,
):
    client = MagicMock()
    client.get_current_gameweek = AsyncMock(return_value=current_gw)
    client.get_next_gameweek = AsyncMock(return_value=next_gw)
    client.get_manager_entry = AsyncMock(return_value=manager_entry or {})
    client.get_manager_history = AsyncMock(return_value=history or {"current": [], "chips": []})
    client.get_manager_picks = AsyncMock(return_value=picks or {"picks": []})
    client.get_players = AsyncMock(return_value=players or [])
    client.get_teams = AsyncMock(return_value=teams or [])
    client.get_classic_league_standings = AsyncMock(return_value=classic_league_standings or {})
    client.get_gameweek_live = AsyncMock(return_value=gameweek_live or {"elements": []})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _mock_draft_client(league_details=None, game_state=None, entry_picks=None, bootstrap=None):
    client = MagicMock()
    client.get_league_details = AsyncMock(return_value=league_details or {})
    client.get_game_state = AsyncMock(return_value=game_state or {"current_event": 30})
    client.get_entry_picks = AsyncMock(return_value=entry_picks or {"picks": []})
    client.get_bootstrap_static = AsyncMock(return_value=bootstrap or {"elements": []})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _run(client, settings=None, draft_client=None, mock_draft_squad=None):
    settings = settings or {"fpl": {"classic_entry_id": 123}}
    runner = CliRunner()
    with ExitStack() as stack:
        stack.enter_context(patch("fpl_cli.cli.load_settings", return_value=settings))
        stack.enter_context(patch("fpl_cli.cli.status.load_settings", return_value=settings))
        stack.enter_context(patch("fpl_cli.api.fpl.FPLClient", return_value=client))
        if draft_client is not None:
            stack.enter_context(patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft_client))
        if mock_draft_squad is not None:
            stack.enter_context(patch(
                "fpl_cli.agents.common.get_draft_squad_players", new=mock_draft_squad,
            ))
        return runner.invoke(main, ["status"])


class TestStatusDashboard:
    def test_shows_gw_state_finished(self):
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2026-04-01T11:00:00Z"},
        )
        result = _run(client)
        assert result.exit_code == 0
        assert "Gameweek 30" in result.output
        assert "Finished" in result.output
        assert "GW31" in result.output

    def test_shows_gw_state_in_progress(self):
        client = _mock_client(
            current_gw={"id": 30, "finished": False},
            next_gw=None,
        )
        result = _run(client)
        assert "In Progress" in result.output

    def test_shows_deadline_countdown(self):
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
        )
        result = _run(client)
        assert "Next Deadline" in result.output

    def test_no_entry_id_shows_hint(self):
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
        )
        result = _run(client, settings={"fpl": {}})
        assert "Configure entry ID" in result.output

    def test_post_gw_shows_points_and_rank(self):
        """Post-GW state shows points and rank movement."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={
                "current": [
                    {"event": 29, "points": 55, "overall_rank": 150000},
                    {"event": 30, "points": 72, "overall_rank": 120000},
                ],
                "chips": [],
            },
        )
        result = _run(client)
        assert "GW30 Result" in result.output
        assert "72" in result.output


class TestChipTwoHalvesRule:
    """Chips are available twice per season, split at GW19.

    Half-season logic is tested exhaustively in test_cli_chips.py via
    ChipPlan.get_available_chips(). These tests verify the status.py
    integration path constructs a transient ChipPlan correctly from
    raw API dicts.
    """

    def _remaining_from_api(self, chips_used: list[dict], current_gw: int) -> list[str]:
        from fpl_cli.cli.chips import CHIP_NAMES
        from fpl_cli.models.chip_plan import ChipPlan, ChipType, UsedChip

        valid = {c.value for c in ChipType}
        used = [
            UsedChip(chip=ChipType(name), gameweek=event)
            for c in chips_used
            if (name := c.get("name", "").lower()) in valid
            and (event := c.get("event", 0))
        ]
        plan = ChipPlan(chips_used=used)
        return [CHIP_NAMES.get(c.value, c.value) for c in plan.get_available_chips(current_gw)]

    def test_first_half_wildcard_used_still_available_second_half(self):
        remaining = self._remaining_from_api([{"name": "wildcard", "event": 5}], current_gw=25)
        assert "Wildcard" in remaining

    def test_first_half_wildcard_used_unavailable_first_half(self):
        remaining = self._remaining_from_api([{"name": "wildcard", "event": 5}], current_gw=10)
        assert "Wildcard" not in remaining

    def test_both_halves_wildcard_used(self):
        chips_used = [
            {"name": "wildcard", "event": 5},
            {"name": "wildcard", "event": 25},
        ]
        remaining = self._remaining_from_api(chips_used, current_gw=30)
        assert "Wildcard" not in remaining

    def test_no_chips_used_all_available(self):
        remaining = self._remaining_from_api([], current_gw=10)
        assert len(remaining) == 4

    def test_all_first_half_chips_used(self):
        chips_used = [
            {"name": "wildcard", "event": 3},
            {"name": "freehit", "event": 5},
            {"name": "bboost", "event": 10},
            {"name": "3xc", "event": 15},
        ]
        assert len(self._remaining_from_api(chips_used, current_gw=18)) == 0
        assert len(self._remaining_from_api(chips_used, current_gw=25)) == 4


# --- Helper unit tests ---

class TestOrdinal:
    def test_ordinals(self):
        from fpl_cli.cli.status import _ordinal
        assert _ordinal(1) == "1st"
        assert _ordinal(2) == "2nd"
        assert _ordinal(3) == "3rd"
        assert _ordinal(4) == "4th"
        assert _ordinal(11) == "11th"
        assert _ordinal(12) == "12th"
        assert _ordinal(13) == "13th"
        assert _ordinal(21) == "21st"
        assert _ordinal("?") == "?"


class TestGwRank:
    def test_gw_rank_first(self):
        from fpl_cli.cli.status import _gw_rank
        standings = [
            {"event_total": 40},
            {"event_total": 60},
            {"event_total": 50},
        ]
        assert _gw_rank(standings, 60) == 1

    def test_gw_rank_tied(self):
        from fpl_cli.cli.status import _gw_rank
        standings = [
            {"event_total": 50},
            {"event_total": 50},
            {"event_total": 60},
        ]
        assert _gw_rank(standings, 50) == 2


# --- R6: Classic league standing ---

class TestClassicLeagueStanding:
    def test_shows_league_rank_post_gw(self):
        """Post-GW classic should show league standing with GW rank and overall rank."""
        injured_player = make_player(id=10, web_name="Injured", status=PlayerStatus.DOUBTFUL, team_id=1)
        team = make_team(id=1, short_name="ARS")
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={
                "current": [{"event": 30, "points": 65, "overall_rank": 50000}],
                "chips": [],
            },
            classic_league_standings={
                "standings": {"results": [
                    {"entry": 123, "rank": 4, "event_total": 65, "total": 1500},
                    {"entry": 456, "rank": 1, "event_total": 80, "total": 1800},
                    {"entry": 789, "rank": 2, "event_total": 70, "total": 1700},
                ]},
            },
            players=[injured_player],
            teams=[team],
        )
        result = _run(client, settings={"fpl": {"classic_entry_id": 123, "classic_league_id": 999}})
        assert result.exit_code == 0
        assert "League:" in result.output
        assert "3rd of 3 this week" in result.output
        assert "4th overall" in result.output

    def test_user_not_in_top_50(self):
        """When user is not found in page 1 standings, show hint."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={
                "current": [{"event": 30, "points": 40, "overall_rank": 200000}],
                "chips": [],
            },
            classic_league_standings={
                "standings": {"results": [
                    {"entry": 456, "rank": 1, "event_total": 80, "total": 1800},
                ]},
            },
        )
        result = _run(client, settings={"fpl": {"classic_entry_id": 123, "classic_league_id": 999}})
        assert "top 50" in result.output


# --- R3/R5: Flagged players bench dimmed ---

class TestFlaggedPlayersBenchDimmed:
    def test_bench_flagged_player_shown_dimmed(self):
        """Flagged bench players should appear with [bench] indicator."""
        starter = make_player(id=1, web_name="Starter", status=PlayerStatus.DOUBTFUL, team_id=1)
        bench_player = make_player(id=12, web_name="BenchGuy", status=PlayerStatus.INJURED, team_id=2)
        healthy = make_player(id=2, web_name="Healthy", status=PlayerStatus.AVAILABLE, team_id=1)
        team1 = make_team(id=1, short_name="ARS")
        team2 = make_team(id=2, short_name="CHE")

        # 11 starters + 4 bench; player 12 is first bench slot
        picks = [{"element": i} for i in range(1, 16)]
        client = _mock_client(
            current_gw={"id": 30, "finished": False},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={"current": [], "chips": []},
            picks={"picks": picks},
            players=[starter, bench_player, healthy],
            teams=[team1, team2],
        )
        result = _run(client)
        assert "Flagged Players" in result.output
        assert "Starter" in result.output
        assert "BenchGuy" in result.output
        assert "(bench)" in result.output


# --- R4: BOTH format layout ---

class TestBothFormatLayout:
    def test_both_shows_separator_and_headers(self):
        """BOTH format should show Classic and Draft headers with separator."""
        client = _mock_client(
            current_gw={"id": 30, "finished": False},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={"current": [], "chips": []},
        )
        draft_cl = _mock_draft_client(
            league_details={"standings": [], "league_entries": []},
            game_state={"current_event": 30, "waivers_processed": False},
        )

        mock_squad = AsyncMock(return_value=[])
        settings = {
            "fpl": {
                "classic_entry_id": 123,
                "draft_entry_id": 456,
                "draft_league_id": 789,
            }
        }
        result = _run(client, settings=settings, draft_client=draft_cl, mock_draft_squad=mock_squad)
        assert result.exit_code == 0
        assert "# Classic" in result.output
        assert "# Draft" in result.output
        assert "-" * 50 in result.output

    def test_both_partial_failure_draft_error(self):
        """If draft section fails, classic should still render."""
        client = _mock_client(
            current_gw={"id": 30, "finished": False},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={"current": [], "chips": []},
        )
        draft_cl = MagicMock()
        draft_cl.__aenter__ = AsyncMock(side_effect=httpx.HTTPError("connection failed"))
        draft_cl.__aexit__ = AsyncMock(return_value=False)

        settings = {
            "fpl": {
                "classic_entry_id": 123,
                "draft_entry_id": 456,
                "draft_league_id": 789,
            }
        }
        result = _run(client, settings=settings, draft_client=draft_cl)
        assert result.exit_code == 0
        assert "# Classic" in result.output
        assert "Could not load draft data" in result.output


# --- R1: Draft post-GW standings ---

class TestDraftPostGwStandings:
    def test_shows_draft_standings(self):
        """Draft post-GW should show GW rank and overall rank."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
        )
        draft_cl = _mock_draft_client(
            league_details={
                "standings": [
                    {"league_entry": 1, "rank": 3, "event_total": 52, "total": 1200},
                    {"league_entry": 2, "rank": 1, "event_total": 70, "total": 1500},
                    {"league_entry": 3, "rank": 2, "event_total": 60, "total": 1400},
                ],
                "league_entries": [
                    {"id": 1, "entry_id": 456, "player_first_name": "Alice", "player_last_name": "A"},
                    {"id": 2, "entry_id": 111, "player_first_name": "Alice", "player_last_name": "B"},
                    {"id": 3, "entry_id": 222, "player_first_name": "Bob", "player_last_name": "C"},
                ],
            },
            game_state={"current_event": 30, "waivers_processed": False},
        )

        mock_squad = AsyncMock(return_value=[])

        settings = {"fpl": {"draft_entry_id": 456, "draft_league_id": 789}}
        result = _run(client, settings=settings, draft_client=draft_cl, mock_draft_squad=mock_squad)
        assert result.exit_code == 0
        assert "GW30 Result" in result.output
        assert "52 pts" in result.output
        assert "3rd of 3 this week" in result.output
        assert "3rd overall" in result.output


# --- R2: Waiver deadline ---

class TestWaiverDeadline:
    def test_waivers_not_processed_shows_countdown(self):
        """When waivers not yet processed, show countdown to waiver deadline."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
        )
        draft_cl = _mock_draft_client(
            league_details={"standings": [], "league_entries": []},
            game_state={"current_event": 30, "waivers_processed": False},
        )

        mock_squad = AsyncMock(return_value=[])

        settings = {"fpl": {"draft_entry_id": 456, "draft_league_id": 789}}
        result = _run(client, settings=settings, draft_client=draft_cl, mock_draft_squad=mock_squad)
        assert "Waiver Deadline" in result.output

    def test_waivers_processed_shows_free_agency(self):
        """When waivers already processed, show free agency message."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
        )
        draft_cl = _mock_draft_client(
            league_details={"standings": [], "league_entries": []},
            game_state={"current_event": 30, "waivers_processed": True},
        )

        mock_squad = AsyncMock(return_value=[])

        settings = {"fpl": {"draft_entry_id": 456, "draft_league_id": 789}}
        result = _run(client, settings=settings, draft_client=draft_cl, mock_draft_squad=mock_squad)
        assert "free agency" in result.output


# --- Draft missing config ---

class TestDraftMissingConfig:
    def test_draft_no_entry_id_shows_hint(self):
        """Draft format with no entry ID should show config hint."""
        client = _mock_client(
            current_gw={"id": 30, "finished": False},
        )
        result = _run(client, settings={"fpl": {"draft_league_id": 42}})
        assert "draft_entry_id" in result.output


# --- _countdown unit tests ---

class TestCountdown:
    def test_future_days_and_hours(self):
        from datetime import datetime, timedelta, timezone

        from fpl_cli.cli.status import _countdown

        future = datetime.now(timezone.utc) + timedelta(days=5, hours=3)
        result = _countdown(future.isoformat())
        assert "d" in result
        assert "h" in result

    def test_past_deadline(self):
        from fpl_cli.cli.status import _countdown
        assert _countdown("2020-01-01T00:00:00Z") == "passed"

    def test_invalid_string_returned_as_is(self):
        from fpl_cli.cli.status import _countdown
        assert _countdown("not-a-date") == "not-a-date"

    def test_minutes_shown_when_no_days(self):
        """When less than a day away, minutes should appear."""
        from datetime import datetime, timedelta, timezone

        from fpl_cli.cli.status import _countdown

        soon = datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)
        result = _countdown(soon.isoformat())
        assert "m" in result
        assert "d" not in result


# --- Classic league standings failure ---

class TestClassicLeagueStandingsFailure:
    def test_league_api_failure_still_shows_points(self):
        """If league standings API fails, post-GW points and rank still render."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={
                "current": [
                    {"event": 29, "points": 55, "overall_rank": 150000},
                    {"event": 30, "points": 72, "overall_rank": 120000},
                ],
                "chips": [],
            },
        )
        client.get_classic_league_standings = AsyncMock(
            side_effect=httpx.HTTPError("API down"),
        )
        result = _run(client, settings={"fpl": {"classic_entry_id": 123, "classic_league_id": 999}})
        assert result.exit_code == 0
        assert "72" in result.output
        assert "GW30 Result" in result.output
        assert "League:" not in result.output


# --- Fines in status ---

class TestStatusFinesClassic:
    """Fines display in classic section of status."""

    def _settings(self, rules, league_id=999):
        return {
            "fpl": {"classic_entry_id": 123, "classic_league_id": league_id},
            "fines": {"classic": rules},
        }

    def _base_client(self, standings_results, user_pts=42, **kwargs):
        return _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={
                "current": [{"event": 30, "points": user_pts, "overall_rank": 100000}],
                "chips": [],
            },
            classic_league_standings={
                "standings": {"results": standings_results},
            },
            picks={"picks": [{"element": i} for i in range(1, 16)]},
            players=[make_player(id=i, web_name=f"Player{i}", team_id=1) for i in range(1, 16)],
            teams=[make_team(id=1, short_name="ARS")],
            **kwargs,
        )

    def test_below_threshold_triggered(self):
        """Below-threshold fine shows when user scores below threshold."""
        standings = [
            {"entry": 123, "rank": 3, "event_total": 25, "total": 1500, "player_name": "You"},
            {"entry": 456, "rank": 1, "event_total": 80, "total": 1800, "player_name": "Alice"},
        ]
        client = self._base_client(standings, user_pts=25)
        rules = [{"type": "below-threshold", "threshold": 30, "penalty": "£1 to the pot"}]
        result = _run(client, settings=self._settings(rules))
        assert result.exit_code == 0
        assert "Fine:" in result.output
        assert "25 pts" in result.output

    def test_below_threshold_not_triggered_silent(self):
        """No fines output when score is above threshold."""
        standings = [
            {"entry": 123, "rank": 1, "event_total": 70, "total": 1500, "player_name": "You"},
            {"entry": 456, "rank": 2, "event_total": 50, "total": 1200, "player_name": "Alice"},
        ]
        client = self._base_client(standings, user_pts=70)
        rules = [{"type": "below-threshold", "threshold": 30, "penalty": "£1"}]
        result = _run(client, settings=self._settings(rules))
        assert result.exit_code == 0
        assert "Fine:" not in result.output

    def test_no_fines_config_no_output(self):
        """No fines configured means no fines output."""
        standings = [
            {"entry": 123, "rank": 1, "event_total": 20, "total": 1500, "player_name": "You"},
        ]
        client = self._base_client(standings, user_pts=20)
        settings = {"fpl": {"classic_entry_id": 123, "classic_league_id": 999}}
        result = _run(client, settings=settings)
        assert result.exit_code == 0
        assert "Fine:" not in result.output

    def test_last_place_triggered(self):
        """Last-place fine shows when user is bottom of standings."""
        standings = [
            {"entry": 456, "rank": 1, "event_total": 80, "total": 1800, "player_name": "Alice"},
            {"entry": 123, "rank": 2, "event_total": 20, "total": 1500, "player_name": "You"},
        ]
        client = self._base_client(standings, user_pts=20)
        rules = [{"type": "last-place", "penalty": "Wear the shirt"}]
        result = _run(client, settings=self._settings(rules))
        assert result.exit_code == 0
        assert "Fine:" in result.output
        assert "last" in result.output.lower()

    def test_red_card_triggers_live_fetch(self):
        """Red-card rule fetches live GW data."""
        standings = [
            {"entry": 123, "rank": 1, "event_total": 70, "total": 1500, "player_name": "You"},
        ]
        live_data = {"elements": [
            {"id": 1, "stats": {"red_cards": 1, "total_points": 2}},
            *[{"id": i, "stats": {"red_cards": 0, "total_points": 5}} for i in range(2, 16)],
        ]}
        client = self._base_client(standings, user_pts=70, gameweek_live=live_data)
        rules = [{"type": "red-card", "penalty": "£5 fine"}]
        result = _run(client, settings=self._settings(rules))
        assert result.exit_code == 0
        assert "Fine:" in result.output
        assert "Red card" in result.output
        client.get_gameweek_live.assert_called_once_with(30)

    def test_no_red_card_rule_skips_live_fetch(self):
        """When no red-card rule configured, live GW data is not fetched."""
        standings = [
            {"entry": 123, "rank": 1, "event_total": 70, "total": 1500, "player_name": "You"},
        ]
        client = self._base_client(standings, user_pts=70)
        rules = [{"type": "below-threshold", "threshold": 30, "penalty": "£1"}]
        result = _run(client, settings=self._settings(rules))
        assert result.exit_code == 0
        client.get_gameweek_live.assert_not_called()

    def test_fines_error_doesnt_break_status(self):
        """Fines evaluation failure still renders rest of status."""
        standings = [
            {"entry": 123, "rank": 1, "event_total": 70, "total": 1500, "player_name": "You"},
        ]
        client = self._base_client(standings, user_pts=70)
        # Invalid rule type will cause parse error - but we need a valid config
        # that causes evaluate_fines to fail internally. Use below-threshold
        # without a threshold in the raw config (parse_fines_config will reject).
        # Instead, let's test with a live GW fetch failure for red-card.
        client.get_gameweek_live = AsyncMock(side_effect=httpx.HTTPError("API down"))
        rules = [{"type": "red-card", "penalty": "£5"}]
        result = _run(client, settings=self._settings(rules))
        assert result.exit_code == 0
        assert "GW30 Result" in result.output

    def test_close_margin_asterisk(self):
        """Close margin with top-level use_net_points shows asterisk footnote."""
        standings = [
            {"entry": 123, "rank": 2, "event_total": 20, "total": 1500, "player_name": "You"},
            {"entry": 456, "rank": 1, "event_total": 23, "total": 1800, "player_name": "Alice"},
        ]
        client = self._base_client(standings, user_pts=20)
        rules = [{"type": "last-place", "penalty": "Wear the shirt"}]
        settings = self._settings(rules)
        settings["use_net_points"] = True
        result = _run(client, settings=settings)
        assert result.exit_code == 0
        assert "*" in result.output
        assert "gross pts" in result.output


class TestStatusFinesDraft:
    """Fines display in draft section of status."""

    def test_draft_below_threshold_triggered(self):
        """Draft fines evaluate using draft standings."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
        )
        draft_cl = _mock_draft_client(
            league_details={
                "standings": [
                    {"league_entry": 1, "rank": 3, "event_total": 25, "total": 1200},
                    {"league_entry": 2, "rank": 1, "event_total": 70, "total": 1500},
                ],
                "league_entries": [
                    {"id": 1, "entry_id": 456, "player_first_name": "Alice", "player_last_name": "A"},
                    {"id": 2, "entry_id": 111, "player_first_name": "Alice", "player_last_name": "B"},
                ],
            },
            game_state={"current_event": 30, "waivers_processed": False},
        )

        mock_squad = AsyncMock(return_value=[])

        settings = {
            "fpl": {"draft_entry_id": 456, "draft_league_id": 789},
            "fines": {"draft": [{"type": "below-threshold", "threshold": 30, "penalty": "£1"}]},
        }
        result = _run(client, settings=settings, draft_client=draft_cl, mock_draft_squad=mock_squad)
        assert result.exit_code == 0
        assert "Fine:" in result.output
        assert "25 pts" in result.output


# --- JSON output ---

def _run_json(client, settings=None, draft_client=None, mock_draft_squad=None):
    settings = settings or {"fpl": {"classic_entry_id": 123}}
    runner = CliRunner()
    with ExitStack() as stack:
        stack.enter_context(patch("fpl_cli.cli.load_settings", return_value=settings))
        stack.enter_context(patch("fpl_cli.cli.status.load_settings", return_value=settings))
        stack.enter_context(patch("fpl_cli.api.fpl.FPLClient", return_value=client))
        if draft_client is not None:
            stack.enter_context(patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft_client))
        if mock_draft_squad is not None:
            stack.enter_context(patch(
                "fpl_cli.agents.common.get_draft_squad_players", new=mock_draft_squad,
            ))
        return runner.invoke(main, ["status", "--format", "json"])


class TestStatusJsonOutput:
    """JSON output for status command."""

    def test_classic_only_happy_path(self):
        """Classic-only: valid JSON with gameweek_info and classic keys, no draft."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={
                "current": [
                    {"event": 29, "points": 55, "overall_rank": 150000},
                    {"event": 30, "points": 72, "overall_rank": 120000},
                ],
                "chips": [],
            },
        )
        result = _run_json(client)
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["command"] == "status"
        assert "gameweek_info" in payload["data"]
        assert "classic" in payload["data"]
        assert "draft" not in payload["data"]

    def test_draft_only_happy_path(self):
        """Draft-only: valid JSON with gameweek_info and draft keys."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
        )
        draft_cl = _mock_draft_client(
            league_details={
                "standings": [
                    {"league_entry": 1, "rank": 3, "event_total": 52, "total": 1200},
                    {"league_entry": 2, "rank": 1, "event_total": 70, "total": 1500},
                ],
                "league_entries": [
                    {"id": 1, "entry_id": 456, "player_first_name": "Alice", "player_last_name": "A"},
                    {"id": 2, "entry_id": 111, "player_first_name": "Alice", "player_last_name": "B"},
                ],
            },
            game_state={"current_event": 30, "waivers_processed": False},
        )
        mock_squad = AsyncMock(return_value=[])
        settings = {"fpl": {"draft_entry_id": 456, "draft_league_id": 789}}
        result = _run_json(client, settings=settings, draft_client=draft_cl, mock_draft_squad=mock_squad)
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "gameweek_info" in payload["data"]
        assert "draft" in payload["data"]
        assert "classic" not in payload["data"]

    def test_both_formats(self):
        """Both formats: JSON has gameweek_info, classic, and draft keys."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={
                "current": [{"event": 30, "points": 65, "overall_rank": 50000}],
                "chips": [],
            },
        )
        draft_cl = _mock_draft_client(
            league_details={"standings": [], "league_entries": []},
            game_state={"current_event": 30, "waivers_processed": True},
        )
        mock_squad = AsyncMock(return_value=[])
        settings = {
            "fpl": {
                "classic_entry_id": 123,
                "draft_entry_id": 456,
                "draft_league_id": 789,
            }
        }
        result = _run_json(client, settings=settings, draft_client=draft_cl, mock_draft_squad=mock_squad)
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "gameweek_info" in payload["data"]
        assert "classic" in payload["data"]
        assert "draft" in payload["data"]

    def test_metadata_format_matches_mode(self):
        """Metadata format field reflects configured mode."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={"current": [{"event": 30, "points": 65, "overall_rank": 50000}], "chips": []},
        )
        result = _run_json(client)
        payload = json.loads(result.output)
        assert payload["metadata"]["format"] == "classic"
        assert payload["metadata"]["gameweek"] == 30

    def test_classic_gw_result_contains_points_and_rank(self):
        """Classic section has gw_result with points and rank."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={
                "current": [
                    {"event": 29, "points": 55, "overall_rank": 150000},
                    {"event": 30, "points": 72, "overall_rank": 120000},
                ],
                "chips": [],
            },
        )
        result = _run_json(client)
        payload = json.loads(result.output)
        gw_result = payload["data"]["classic"]["gw_result"]
        assert gw_result["points"] == 72
        assert gw_result["overall_rank"] == 120000
        assert gw_result["rank_change"] == 30000

    def test_no_league_configured_no_league_standing(self):
        """When no classic_league_id, league_standing key is absent."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={
                "current": [{"event": 30, "points": 65, "overall_rank": 50000}],
                "chips": [],
            },
        )
        settings = {"fpl": {"classic_entry_id": 123}}  # no classic_league_id
        result = _run_json(client, settings=settings)
        payload = json.loads(result.output)
        assert "league_standing" not in payload["data"]["classic"]

    def test_gw_not_finished_no_gw_result_has_pre_deadline(self):
        """When GW not finished, no gw_result but pre_deadline is present."""
        client = _mock_client(
            current_gw={"id": 30, "finished": False},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={"current": [], "chips": []},
        )
        result = _run_json(client)
        payload = json.loads(result.output)
        classic = payload["data"]["classic"]
        assert "gw_result" not in classic
        assert "pre_deadline" in classic
        assert "bank" in classic["pre_deadline"]

    def test_no_entry_ids_format_none(self):
        """No entry IDs (format=None): JSON has gameweek_info only."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
        )
        result = _run_json(client, settings={"fpl": {}})
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "gameweek_info" in payload["data"]
        assert "classic" not in payload["data"]
        assert "draft" not in payload["data"]
        assert payload["metadata"]["format"] is None

    def test_api_failure_classic_omitted(self):
        """API failure in classic section: classic key omitted, no crash."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
        )
        client.get_manager_history = AsyncMock(side_effect=httpx.HTTPError("API down"))
        result = _run_json(client)
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "gameweek_info" in payload["data"]
        assert "classic" not in payload["data"]

    def test_table_output_unchanged(self):
        """Existing table output still works with default format."""
        client = _mock_client(
            current_gw={"id": 30, "finished": True},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={
                "current": [{"event": 30, "points": 72, "overall_rank": 120000}],
                "chips": [],
            },
        )
        result = _run(client)
        assert result.exit_code == 0
        assert "FPL Status" in result.output
        assert "Gameweek 30" in result.output


class TestStatusDiscoveryNote:
    """Discovery note for custom analysis (R10)."""

    def test_discovery_note_when_toggle_off(self):
        client = _mock_client(
            current_gw={"id": 30, "finished": False},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
        )
        result = _run(client, settings={"fpl": {"classic_entry_id": 123}})
        assert result.exit_code == 0
        assert "Custom analysis features" in result.output
        assert "fpl init" in result.output

    def test_no_discovery_note_when_toggle_on(self):
        client = _mock_client(
            current_gw={"id": 30, "finished": False},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
        )
        settings = {"fpl": {"classic_entry_id": 123}, "custom_analysis": True}
        result = _run(client, settings=settings)
        assert result.exit_code == 0
        assert "Custom analysis features" not in result.output

    def test_discovery_note_not_in_json_output(self):
        client = _mock_client(
            current_gw={"id": 30, "finished": False},
            next_gw={"id": 31, "deadline_time": "2099-01-01T11:00:00Z"},
            history={"current": [], "chips": []},
        )
        result = _run_json(client, settings={"fpl": {"classic_entry_id": 123}})
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "Custom analysis" not in str(payload)
