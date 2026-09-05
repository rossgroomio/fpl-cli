"""Tests for shared agent utilities in agents/common.py."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from fpl_cli.agents.common import (
    SquadPicksUnavailableError,
    enrich_player,
    fetch_understat_lookup,
    get_actual_squad_picks,
    get_draft_ownership_mapping,
    get_draft_squad_players,
    get_own_squad_picks,
    rekey_for_draft,
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


class TestGetDraftOwnershipMapping:
    def _make_client(self):
        client = AsyncMock()
        client.get_league_details = AsyncMock(return_value={
            "league_entries": [
                {"entry_id": 7, "player_first_name": "Ross", "player_last_name": "Groom"},
            ],
        })
        client.get_bootstrap_static = AsyncMock(return_value={
            "elements": [{"id": 99, "web_name": "Salah", "team": 1}],
        })
        client.get_league_ownership = AsyncMock(return_value={99: 7})
        return client

    async def test_reuses_league_details_it_already_fetched(self):
        """get_league_details is not memoised, so it must not be fetched twice."""
        client = self._make_client()

        await get_draft_ownership_mapping(client, [make_player(id=1, web_name="Salah", team_id=1)], 12345)

        client.get_league_details.assert_awaited_once()
        args = client.get_league_ownership.await_args.args
        assert args[2] == client.get_league_details.return_value

    async def test_returns_ownership_entries_and_id_mapping(self):
        client = self._make_client()

        owned, entries, main_to_draft = await get_draft_ownership_mapping(
            client, [make_player(id=1, web_name="Salah", team_id=1)], 12345,
        )

        assert owned == {99: 7}
        assert entries == {7: "Ross Groom"}
        assert main_to_draft == {1: 99}


class TestRekeyForDraft:
    """#209: main-keyed scoring lookups translated into the draft id space."""

    def _map(self):
        """Draft 5 is main 500 — the two spaces disagree for this player."""
        return {5: make_player(id=500, web_name="Semenyo", team_id=1)}

    def test_value_moves_from_the_main_id_to_the_draft_id(self):
        assert rekey_for_draft({500: "own"}, self._map()) == {5: "own"}

    def test_a_row_under_the_draft_id_is_never_borrowed(self):
        """The whole bug: reading raw, draft id 5 found main player 5's row."""
        assert rekey_for_draft({5: "stranger"}, self._map()) == {}

    def test_unmatched_draft_element_is_absent_rather_than_guessed(self):
        assert rekey_for_draft({500: "own"}, {}) == {}

    def test_none_stays_none(self):
        """Callers distinguish 'not requested' from 'requested and empty'."""
        assert rekey_for_draft(None, self._map()) is None

    def test_empty_lookup_stays_empty(self):
        assert rekey_for_draft({}, self._map()) == {}


class TestGetOwnSquadPicks:
    """One diagnosis for the two causes behind a picks 404, so `fpl squad` and
    `fpl captain` cannot word the same broken config differently (#228)."""

    @staticmethod
    def _not_found(path: str) -> httpx.HTTPStatusError:
        request = httpx.Request("GET", f"https://fantasy.premierleague.com/api{path}")
        return httpx.HTTPStatusError(
            "Not Found", request=request, response=httpx.Response(404, request=request)
        )

    async def test_passes_picks_through_when_they_exist(self):
        client = AsyncMock()
        client.get_manager_picks = AsyncMock(return_value={"active_chip": None, "picks": [{"element": 1}]})

        picks, gw = await get_own_squad_picks(client, 123, 10)

        assert (picks["picks"], gw) == ([{"element": 1}], 10)
        client.get_manager_entry.assert_not_awaited()

    async def test_live_entry_reads_as_no_squad_yet(self):
        client = AsyncMock()
        client.get_manager_picks = AsyncMock(side_effect=self._not_found("/entry/123/event/2/picks/"))
        client.get_manager_entry = AsyncMock(return_value={"id": 123, "name": "Team"})

        with pytest.raises(SquadPicksUnavailableError) as exc_info:
            await get_own_squad_picks(client, 123, 2)

        assert "No squad submitted for GW2 yet" in str(exc_info.value)

    async def test_missing_entry_names_the_reissued_id(self):
        client = AsyncMock()
        client.get_manager_picks = AsyncMock(side_effect=self._not_found("/entry/999/event/2/picks/"))
        client.get_manager_entry = AsyncMock(side_effect=self._not_found("/entry/999/"))

        with pytest.raises(SquadPicksUnavailableError) as exc_info:
            await get_own_squad_picks(client, 999, 2)

        message = str(exc_info.value)
        assert "No FPL entry 999 exists" in message
        assert "reissued" in message

    async def test_unreachable_entry_endpoint_does_not_condemn_the_id(self):
        """A 503 proves nothing about the ID, so the wording must not change."""
        client = AsyncMock()
        client.get_manager_picks = AsyncMock(side_effect=self._not_found("/entry/123/event/2/picks/"))
        request = httpx.Request("GET", "https://fantasy.premierleague.com/api/entry/123/")
        client.get_manager_entry = AsyncMock(side_effect=httpx.HTTPStatusError(
            "Service Unavailable", request=request,
            response=httpx.Response(503, request=request),
        ))

        with pytest.raises(SquadPicksUnavailableError) as exc_info:
            await get_own_squad_picks(client, 123, 2)

        assert "No squad submitted for GW2 yet" in str(exc_info.value)

    async def test_a_broken_probe_never_costs_the_report(self):
        """The probe only refines the message; whatever it raises, the 404 the
        caller already established still gets reported."""
        client = AsyncMock()
        client.get_manager_picks = AsyncMock(side_effect=self._not_found("/entry/123/event/2/picks/"))
        client.get_manager_entry = AsyncMock(side_effect=ValueError("malformed JSON"))

        with pytest.raises(SquadPicksUnavailableError) as exc_info:
            await get_own_squad_picks(client, 123, 2)

        assert "No squad submitted for GW2 yet" in str(exc_info.value)

    async def test_a_non_404_still_raises_the_http_error(self):
        client = AsyncMock()
        request = httpx.Request("GET", "https://fantasy.premierleague.com/api/entry/123/event/2/picks/")
        client.get_manager_picks = AsyncMock(side_effect=httpx.HTTPStatusError(
            "Server Error", request=request, response=httpx.Response(500, request=request),
        ))

        with pytest.raises(httpx.HTTPStatusError):
            await get_own_squad_picks(client, 123, 2)

        client.get_manager_entry.assert_not_awaited()
