"""Tests for shared agent utilities in agents/common.py."""

from unittest.mock import AsyncMock, patch

from fpl_cli.agents.common import (
    enrich_player,
    fetch_understat_lookup,
    get_actual_squad_picks,
    get_draft_squad_players,
)
from fpl_cli.services.matchup import build_team_fixture_map
from tests.conftest import make_draft_player, make_draft_team, make_fixture, make_player


class TestBuildTeamFixtureMap:
    def test_single_fixture(self):
        f = make_fixture(home_team_id=1, away_team_id=2)
        result = build_team_fixture_map([f])

        assert len(result) == 2
        assert len(result[1]) == 1
        assert result[1][0]["fixture"] is f
        assert result[1][0]["is_home"] is True
        assert result[2][0]["is_home"] is False

    def test_empty_fixtures(self):
        assert build_team_fixture_map([]) == {}

    def test_dgw_team_gets_two_entries(self):
        f1 = make_fixture(id=1, home_team_id=1, away_team_id=2)
        f2 = make_fixture(id=2, home_team_id=3, away_team_id=1)
        result = build_team_fixture_map([f1, f2])

        assert len(result[1]) == 2
        assert result[1][0]["is_home"] is True
        assert result[1][1]["is_home"] is False

    def test_all_teams_included(self):
        f = make_fixture(home_team_id=5, away_team_id=8)
        result = build_team_fixture_map([f])
        assert set(result.keys()) == {5, 8}


class TestEnrichPlayer:
    def test_adds_team_name_and_position(self):
        player = {"team_id": 1, "position": "MID", "minutes": 100}
        team_map = {1: {"name": "Arsenal", "short_name": "ARS"}}
        result = enrich_player(player, team_map)

        assert result["team_name"] == "Arsenal"
        assert result["team_short"] == "ARS"
        assert result["position"] == "MID"

    def test_xgi_per_90_with_sufficient_minutes(self):
        player = {
            "team_id": 1, "position": "FWD", "minutes": 900,
            "expected_goals": 4.5, "expected_assists": 2.5,
        }
        team_map = {1: {"name": "Test", "short_name": "TST"}}
        result = enrich_player(player, team_map)

        expected = round(((4.5 + 2.5) / 900) * 90, 2)
        assert result["xGI_per_90"] == expected

    def test_xgi_per_90_below_min_minutes(self):
        player = {"team_id": 1, "position": "GK", "minutes": 10}
        team_map = {1: {"name": "Test", "short_name": "TST"}}
        result = enrich_player(player, team_map)
        assert result["xGI_per_90"] == 0

    def test_availability_included_by_default(self):
        player = {"team_id": 1, "position": "GK", "minutes": 0, "chance_of_playing": 75, "news": "Knee injury"}
        team_map = {1: {"name": "Test", "short_name": "TST"}}
        result = enrich_player(player, team_map)

        assert result["availability"] == "75%"
        assert result["injury_news"] == "Knee injury"

    def test_availability_excluded(self):
        player = {"team_id": 1, "position": "GK", "minutes": 0, "chance_of_playing": 0}
        team_map = {1: {"name": "Test", "short_name": "TST"}}
        result = enrich_player(player, team_map, include_availability=False)

        assert "availability" not in result

    def test_missing_team_uses_defaults(self):
        player = {"team_id": 999, "position": "GK", "minutes": 0}
        result = enrich_player(player, {})

        assert result["team_name"] == "Unknown"
        assert result["team_short"] == "???"

    def test_low_minutes_zeroes_xgi(self):
        """xGI/90 zeroed when minutes below threshold (Nyoni scenario)."""
        team_map = {14: {"name": "Liverpool", "short_name": "LIV"}}
        player = {"team_id": 14, "minutes": 6, "expected_goals": 0.3, "expected_assists": 0.07, "position": "MID"}
        result = enrich_player(player, team_map, include_availability=False)
        assert result["xGI_per_90"] == 0

    def test_availability_checkmark_for_full(self):
        player = {"team_id": 1, "position": "GK", "minutes": 0, "chance_of_playing": 100}
        result = enrich_player(player, {1: {"name": "T", "short_name": "T"}})
        assert result["availability"] == "\u2713"

    def test_availability_cross_for_zero(self):
        player = {"team_id": 1, "position": "GK", "minutes": 0, "chance_of_playing": 0}
        result = enrich_player(player, {1: {"name": "T", "short_name": "T"}})
        assert result["availability"] == "\u2717"

    def test_injury_news_truncation(self):
        long_news = "This is a very long injury news message that should be truncated"
        player = {"team_id": 1, "position": "GK", "minutes": 0, "news": long_news}
        result = enrich_player(player, {1: {"name": "T", "short_name": "T"}})
        assert len(result["injury_news"]) <= 30

    def test_with_draft_player_integration(self):
        """Integration test using real draft API parse flow."""
        from fpl_cli.api.fpl_draft import FPLDraftClient
        client = FPLDraftClient()
        team_map = {14: make_draft_team(id=14, name="Liverpool", short_name="LIV")}
        raw = make_draft_player(id=1, web_name="Salah", team=14, element_type=3, minutes=1800)
        player = client.parse_player(raw)
        result = enrich_player(player, team_map)

        assert result["team_name"] == "Liverpool"
        assert result["position"] == "MID"
        assert result["xGI_per_90"] > 0


class TestFetchUnderstatLookup:
    async def test_returns_matched_players(self):
        mock_us_player = {"name": "Salah", "team": "Liverpool", "npxG_per_90": 0.5}
        players = [{"player_name": "Salah", "position": "MID", "minutes": 1800}]

        with (
            patch("fpl_cli.agents.common.UnderstatClient") as mock_client,
            patch("fpl_cli.agents.common.match_fpl_to_understat", return_value=mock_us_player),
        ):
            mock_client.return_value.get_league_players = AsyncMock(return_value=[mock_us_player])
            mock_client.return_value.close = AsyncMock()
            result = await fetch_understat_lookup(players, lambda p: "Liverpool")

        assert 0 in result
        assert result[0]["npxG_per_90"] == 0.5

    async def test_skips_players_without_team(self):
        players = [{"player_name": "Unknown", "position": "MID", "minutes": 100}]

        with (
            patch("fpl_cli.agents.common.UnderstatClient") as mock_client,
            patch("fpl_cli.agents.common.match_fpl_to_understat") as mock_match,
        ):
            mock_client.return_value.get_league_players = AsyncMock(return_value=[])
            mock_client.return_value.close = AsyncMock()
            result = await fetch_understat_lookup(players, lambda p: None)

        mock_match.assert_not_called()
        assert result == {}

    async def test_network_error_returns_empty(self):
        import httpx

        players = [{"player_name": "Salah", "position": "MID", "minutes": 1800}]
        logged = []

        with patch("fpl_cli.agents.common.UnderstatClient") as mock_client:
            mock_client.return_value.get_league_players = AsyncMock(
                side_effect=httpx.ConnectError("connection failed")
            )
            mock_client.return_value.close = AsyncMock()
            result = await fetch_understat_lookup(
                players, lambda p: "Liverpool", log=logged.append
            )

        assert result == {}
        assert len(logged) == 1
        assert "unavailable" in logged[0]

    async def test_accepts_shared_client(self):
        mock_us_player = {"name": "Salah", "team": "Liverpool"}
        players = [{"player_name": "Salah", "position": "MID", "minutes": 1800}]

        from fpl_cli.api.understat import UnderstatClient
        shared_client = UnderstatClient()
        shared_client.get_league_players = AsyncMock(return_value=[mock_us_player])

        with patch("fpl_cli.agents.common.match_fpl_to_understat", return_value=mock_us_player):
            result = await fetch_understat_lookup(
                players, lambda p: "Liverpool", client=shared_client
            )

        assert 0 in result
        shared_client.get_league_players.assert_awaited_once()

    async def test_empty_players_list(self):
        with patch("fpl_cli.agents.common.UnderstatClient") as mock_client:
            mock_client.return_value.get_league_players = AsyncMock(return_value=[])
            mock_client.return_value.close = AsyncMock()
            result = await fetch_understat_lookup([], lambda p: "")

        assert result == {}


class TestGetActualSquadPicks:
    async def test_normal_gameweek_passes_through(self):
        client = AsyncMock()
        client.get_manager_picks = AsyncMock(return_value={"active_chip": None, "picks": [{"element": 1}]})

        picks, gw = await get_actual_squad_picks(client, entry_id=123, gameweek=10)

        assert gw == 10
        assert picks["picks"] == [{"element": 1}]
        client.get_manager_picks.assert_awaited_once_with(123, 10)

    async def test_freehit_falls_back_one_gw(self):
        client = AsyncMock()
        client.get_manager_picks = AsyncMock(
            side_effect=[
                {"active_chip": "freehit", "picks": []},
                {"active_chip": None, "picks": [{"element": 2}]},
            ]
        )
        logged = []

        picks, gw = await get_actual_squad_picks(client, 123, 5, log=logged.append)

        assert gw == 4
        assert picks["picks"] == [{"element": 2}]
        assert len(logged) == 1
        assert "Free Hit" in logged[0]

    async def test_freehit_gw1_no_fallback(self):
        client = AsyncMock()
        client.get_manager_picks = AsyncMock(return_value={"active_chip": "freehit", "picks": []})

        picks, gw = await get_actual_squad_picks(client, 123, 1)

        assert gw == 1
        client.get_manager_picks.assert_awaited_once_with(123, 1)

    async def test_other_chip_no_fallback(self):
        client = AsyncMock()
        client.get_manager_picks = AsyncMock(return_value={"active_chip": "bboost", "picks": [{"element": 3}]})

        picks, gw = await get_actual_squad_picks(client, 123, 10)

        assert gw == 10
        assert picks["active_chip"] == "bboost"


class TestGetDraftSquadPlayersDiacritics:
    """Verify draft-to-main mapping handles accented name mismatches."""

    @staticmethod
    def _mock_draft_client(draft_elements: list[dict], picks: list[dict]):
        client = AsyncMock()
        client.get_bootstrap_static = AsyncMock(
            return_value={"elements": draft_elements}
        )
        client.get_entry_picks = AsyncMock(
            return_value={"picks": picks}
        )
        return client

    async def test_accented_main_ascii_draft(self):
        """Main FPL has 'Gyökeres', Draft API has 'Gyokeres' - should match."""
        main_players = [make_player(id=10, web_name="Gyökeres", team_id=1)]
        draft_elements = [{"id": 100, "web_name": "Gyokeres", "team": 1}]
        picks = [{"element": 100}]

        client = self._mock_draft_client(draft_elements, picks)
        squad = await get_draft_squad_players(client, main_players, 1, 1)
        assert len(squad) == 1
        assert squad[0].id == 10

    async def test_ascii_main_accented_draft(self):
        """Main FPL has 'Raul', Draft API has 'Raúl' - should match."""
        main_players = [make_player(id=20, web_name="Raul", team_id=3)]
        draft_elements = [{"id": 200, "web_name": "Raúl", "team": 3}]
        picks = [{"element": 200}]

        client = self._mock_draft_client(draft_elements, picks)
        squad = await get_draft_squad_players(client, main_players, 1, 1)
        assert len(squad) == 1
        assert squad[0].id == 20

    async def test_non_accented_still_matches(self):
        """Plain ASCII names still match as before."""
        main_players = [make_player(id=30, web_name="Haaland", team_id=2)]
        draft_elements = [{"id": 300, "web_name": "Haaland", "team": 2}]
        picks = [{"element": 300}]

        client = self._mock_draft_client(draft_elements, picks)
        squad = await get_draft_squad_players(client, main_players, 1, 1)
        assert len(squad) == 1
        assert squad[0].id == 30
