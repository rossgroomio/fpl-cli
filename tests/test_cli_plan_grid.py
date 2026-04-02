"""Tests for `fpl squad grid` command."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.models.player import PlayerPosition
from tests.conftest import make_fixture, make_player, make_team


def _squad():
    return [
        make_player(id=1, web_name="Raya", team_id=1, position=PlayerPosition.GOALKEEPER),
        make_player(id=2, web_name="Gabriel", team_id=1, position=PlayerPosition.DEFENDER),
        make_player(id=3, web_name="Saka", team_id=1, position=PlayerPosition.MIDFIELDER),
        make_player(id=4, web_name="Jesus", team_id=1, position=PlayerPosition.FORWARD),
    ]


def _teams():
    return [
        make_team(id=1, name="Arsenal", short_name="ARS"),
        make_team(id=2, name="Chelsea", short_name="CHE"),
    ]


def _fixtures():
    return [
        make_fixture(id=1, gameweek=30, home_team_id=1, away_team_id=2),
        make_fixture(id=2, gameweek=32, home_team_id=2, away_team_id=1),
    ]


def _make_mocks(squad=None, teams=None, fixtures=None):
    squad = squad or _squad()
    teams = teams or _teams()
    fixtures = fixtures if fixtures is not None else _fixtures()

    client = MagicMock()
    client.get_players = AsyncMock(return_value=squad)
    client.get_teams = AsyncMock(return_value=teams)
    client.get_next_gameweek = AsyncMock(return_value={"id": 30})
    client.get_manager_picks = AsyncMock(return_value={
        "picks": [{"element": p.id} for p in squad],
        "active_chip": None,
    })
    client.get_fixtures = AsyncMock(return_value=fixtures)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    ratings = MagicMock()
    ratings.get_positional_fdr.return_value = 2.5
    ratings.ensure_fresh = AsyncMock()

    return client, ratings


def _run(args, client, ratings):
    runner = CliRunner()
    with (
        patch("fpl_cli.cli._plan_grid.load_settings", return_value={"fpl": {"classic_entry_id": 123}}),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings),
    ):
        return runner.invoke(main, ["squad", "grid"] + args)


class TestPlanGrid:
    def test_renders_without_error(self):
        client, ratings = _make_mocks()
        result = _run([], client, ratings)
        assert result.exit_code == 0, result.output
        assert "Fixture Grid" in result.output
        assert "GW30" in result.output

    def test_players_grouped_by_position_order(self):
        client, ratings = _make_mocks()
        result = _run([], client, ratings)
        output = result.output
        assert output.index("GK") < output.index("DEF") < output.index("MID") < output.index("FWD")

    def test_watch_flag_adds_extra_player(self):
        squad = _squad()
        watch_player = make_player(id=99, web_name="Mbeumo", team_id=2, position=PlayerPosition.FORWARD)
        client, ratings = _make_mocks(squad=squad + [watch_player])
        result = _run(["-w", "Mbeumo"], client, ratings)
        assert result.exit_code == 0, result.output
        assert "Mbeumo" in result.output

    def test_blank_gw_renders_as_dash(self):
        client, ratings = _make_mocks(fixtures=[])
        result = _run(["-n", "1"], client, ratings)
        assert result.exit_code == 0, result.output
        assert "-" in result.output

    def test_custom_gw_count(self):
        client, ratings = _make_mocks()
        result = _run(["-n", "3"], client, ratings)
        assert result.exit_code == 0, result.output
        assert "GW30" in result.output
        assert "GW32" in result.output
        assert "GW33" not in result.output

    def test_mode_opponent_threads_to_ratings_service(self):
        client, ratings = _make_mocks()
        result = _run(["--mode", "opponent", "-n", "1"], client, ratings)
        assert result.exit_code == 0, result.output
        calls = ratings.get_positional_fdr.call_args_list
        assert len(calls) > 0
        assert all(c.kwargs.get("mode") == "opponent" for c in calls)

    def test_mode_defaults_to_difference(self):
        client, ratings = _make_mocks()
        result = _run(["-n", "1"], client, ratings)
        assert result.exit_code == 0, result.output
        calls = ratings.get_positional_fdr.call_args_list
        assert all(c.kwargs.get("mode") == "difference" for c in calls)


def _draft_elements():
    return [
        {"id": 901, "web_name": "Raya", "team": 1, "element_type": 1},
        {"id": 902, "web_name": "Gabriel", "team": 1, "element_type": 2},
        {"id": 903, "web_name": "Saka", "team": 1, "element_type": 3},
        {"id": 904, "web_name": "Jesus", "team": 1, "element_type": 4},
    ]


def _make_draft_mocks(squad=None, teams=None, fixtures=None, draft_elements=None):
    squad = squad or _squad()
    teams = teams or _teams()
    fixtures = fixtures if fixtures is not None else _fixtures()
    draft_els = draft_elements or _draft_elements()

    client = MagicMock()
    client.get_players = AsyncMock(return_value=squad)
    client.get_teams = AsyncMock(return_value=teams)
    client.get_next_gameweek = AsyncMock(return_value={"id": 30})
    client.get_fixtures = AsyncMock(return_value=fixtures)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    draft_client = MagicMock()
    draft_client.get_bootstrap_static = AsyncMock(return_value={"elements": draft_els})
    draft_client.get_entry_picks = AsyncMock(return_value={
        "picks": [{"element": dp["id"]} for dp in draft_els],
    })
    draft_client.__aenter__ = AsyncMock(return_value=draft_client)
    draft_client.__aexit__ = AsyncMock(return_value=False)

    ratings = MagicMock()
    ratings.get_positional_fdr.return_value = 2.5
    ratings.ensure_fresh = AsyncMock()

    return client, draft_client, ratings


def _run_draft(args, client, draft_client, ratings):
    runner = CliRunner()
    with (
        patch("fpl_cli.cli._plan_grid.load_settings", return_value={"fpl": {"draft_entry_id": 456}}),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft_client),
        patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings),
    ):
        return runner.invoke(main, ["squad", "grid", "--draft"] + args)


class TestPlanGridDraft:
    def test_draft_renders_without_error(self):
        client, draft_client, ratings = _make_draft_mocks()
        result = _run_draft([], client, draft_client, ratings)
        assert result.exit_code == 0, result.output
        assert "Fixture Grid (Draft)" in result.output

    def test_draft_maps_ids_via_name_team(self):
        client, draft_client, ratings = _make_draft_mocks()
        result = _run_draft(["-n", "1"], client, draft_client, ratings)
        assert result.exit_code == 0, result.output
        assert "Raya" in result.output

    def test_draft_missing_config_errors(self):
        runner = CliRunner()
        with (
            patch("fpl_cli.cli._plan_grid.load_settings", return_value={"fpl": {}}),
        ):
            result = runner.invoke(main, ["squad", "grid", "--draft"])
            assert "draft_entry_id not configured" in result.output

    def test_draft_with_watch_list(self):
        squad = _squad()
        watch_player = make_player(id=99, web_name="Mbeumo", team_id=2, position=PlayerPosition.FORWARD)
        all_players = squad + [watch_player]
        client, draft_client, ratings = _make_draft_mocks(squad=all_players)
        result = _run_draft(["-w", "Mbeumo"], client, draft_client, ratings)
        assert result.exit_code == 0, result.output
        assert "Mbeumo" in result.output


class TestPlanGridJson:
    def test_json_output_is_valid(self):
        client, ratings = _make_mocks()
        result = _run(["--format", "json", "-n", "2"], client, ratings)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["command"] == "plan-grid"
        assert "metadata" in payload
        assert "data" in payload

    def test_json_metadata_fields(self):
        client, ratings = _make_mocks()
        result = _run(["--format", "json", "-n", "3", "--mode", "opponent"], client, ratings)
        payload = json.loads(result.output)
        meta = payload["metadata"]
        assert meta["gameweek"] == 30
        assert meta["format"] == "classic"
        assert meta["gws"] == 3
        assert meta["mode"] == "opponent"

    def test_json_data_structure(self):
        client, ratings = _make_mocks()
        result = _run(["--format", "json", "-n", "2"], client, ratings)
        payload = json.loads(result.output)
        data = payload["data"]
        assert len(data) == 4
        first = data[0]
        assert "player" in first
        assert "position" in first
        assert "team" in first
        assert "gameweeks" in first
        for gw_key in first["gameweeks"]:
            assert isinstance(gw_key, str)

    def test_json_fixture_entries(self):
        client, ratings = _make_mocks()
        result = _run(["--format", "json", "-n", "3"], client, ratings)
        payload = json.loads(result.output)
        player = payload["data"][0]
        gw30 = player["gameweeks"]["30"]
        assert len(gw30) == 1
        assert gw30[0]["opponent"] == "CHE"
        assert gw30[0]["venue"] == "home"
        assert gw30[0]["fdr"] == 2.5

    def test_json_blank_gameweek(self):
        client, ratings = _make_mocks()
        result = _run(["--format", "json", "-n", "3"], client, ratings)
        payload = json.loads(result.output)
        player = payload["data"][0]
        assert player["gameweeks"]["31"] == []

    def test_json_players_sorted_by_position(self):
        client, ratings = _make_mocks()
        result = _run(["--format", "json"], client, ratings)
        payload = json.loads(result.output)
        positions = [p["position"] for p in payload["data"]]
        order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
        assert positions == sorted(positions, key=lambda pos: order[pos])

    def test_json_with_watch_player(self):
        squad = _squad()
        watch_player = make_player(id=99, web_name="Mbeumo", team_id=2, position=PlayerPosition.FORWARD)
        client, ratings = _make_mocks(squad=squad + [watch_player])
        result = _run(["--format", "json", "-w", "Mbeumo", "-n", "1"], client, ratings)
        payload = json.loads(result.output)
        names = [p["player"] for p in payload["data"]]
        assert "Mbeumo" in names
        assert len(names) > len(squad)

    def test_json_no_table_markup(self):
        client, ratings = _make_mocks()
        result = _run(["--format", "json", "-n", "1"], client, ratings)
        assert "[bold]" not in result.output
        assert "Fixture Grid" not in result.output

    def test_json_draft_format_metadata(self):
        client, draft_client, ratings = _make_draft_mocks()
        runner = CliRunner()
        with (
            patch("fpl_cli.cli._plan_grid.load_settings", return_value={"fpl": {"draft_entry_id": 456}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft_client),
            patch("fpl_cli.services.team_ratings.TeamRatingsService", return_value=ratings),
        ):
            result = runner.invoke(main, ["squad", "grid", "--draft", "--format", "json", "-n", "1"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["metadata"]["format"] == "draft"
