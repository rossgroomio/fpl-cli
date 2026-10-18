"""Tests for FPL Draft API client."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from fpl_cli.api.fpl_draft import (
    FPLDraftClient,
    draft_player_team_id,
    is_accepted_transaction,
    is_denied_on_the_incoming_player,
    is_processed_trade,
    match_draft_teams_to_main,
    match_draft_to_main,
    resolve_lost_claims,
    untranslated_draft_teams,
)
from tests.conftest import (
    make_draft_league_entry,
    make_draft_player,
    make_draft_standing,
    make_draft_team,
    make_player,
    make_team,
)

# --- Fixtures ---

@pytest.fixture
def mock_draft_bootstrap():
    """Mock draft bootstrap-static response."""
    return {
        "elements": [
            make_draft_player(id=1, web_name="Salah", team=14, element_type=3, form=7.0, total_points=120),
            make_draft_player(id=2, web_name="Haaland", team=13, element_type=4, form=8.0, total_points=150),
            make_draft_player(id=3, web_name="Saka", team=1, element_type=3, form=6.0, total_points=90),
            make_draft_player(id=4, web_name="Gabriel", team=1, element_type=2, form=5.0, total_points=70),
            make_draft_player(id=5, web_name="Unavailable", team=1, element_type=3, status="u", total_points=0),
        ],
        "teams": [
            make_draft_team(id=1, name="Arsenal", short_name="ARS"),
            make_draft_team(id=13, name="Man City", short_name="MCI"),
            make_draft_team(id=14, name="Liverpool", short_name="LIV"),
        ],
    }


@pytest.fixture
def mock_league_details():
    """Mock draft league details response."""
    return {
        "league": {"id": 12345, "name": "Test Draft League"},
        "league_entries": [
            make_draft_league_entry(id=1, entry_id=100, entry_name="Team A"),
            make_draft_league_entry(id=2, entry_id=101, entry_name="Team B"),
            make_draft_league_entry(id=3, entry_id=102, entry_name="Team C"),
        ],
        "standings": [
            make_draft_standing(league_entry=1, rank=1, total=500, event_total=60),
            make_draft_standing(league_entry=2, rank=2, total=450, event_total=55),
            make_draft_standing(league_entry=3, rank=3, total=400, event_total=50),
        ],
    }


@pytest.fixture
def mock_element_status():
    """Mock element-status response."""
    return {
        "element_status": [
            {"element": 1, "owner": 100},  # Salah owned by team 100
            {"element": 2, "owner": 101},  # Haaland owned by team 101
            {"element": 3, "owner": None},  # Saka available
            {"element": 4, "owner": None},  # Gabriel available
            {"element": 5, "owner": None},  # Unavailable player
        ]
    }


@pytest.fixture
def mock_game_data():
    """Mock game data response."""
    return {
        "current_event": 25,
        "next_event": 26,
    }


@pytest.fixture
def mock_entry_picks():
    """Mock entry picks response."""
    return {
        "picks": [
            {"element": 1, "position": 1},
            {"element": 3, "position": 2},
        ]
    }


@pytest.fixture
def mock_transactions():
    """Mock transactions response."""
    return {
        "transactions": [
            {"element_in": 3, "element_out": 10, "entry": 100, "event": 25, "kind": "w", "result": "a"},
            {"element_in": 4, "element_out": 11, "entry": 101, "event": 24, "kind": "w", "result": "a"},
            {"element_in": 12, "element_out": 5, "entry": 102, "event": 23, "kind": "w", "result": "a"},
        ]
    }


# --- TestFPLDraftClient ---

class TestFPLDraftClientInit:
    """Tests for FPLDraftClient initialization."""

    def test_default_timeout(self):
        """Test default timeout is 30 seconds."""
        client = FPLDraftClient()
        assert client.timeout == 30.0

    def test_custom_timeout(self):
        """Test custom timeout is applied."""
        client = FPLDraftClient(timeout=60.0)
        assert client.timeout == 60.0

    def test_initial_cache_state(self):
        """Test initial cache is None."""
        client = FPLDraftClient()
        assert client._bootstrap_data is None


class TestFPLDraftClientBootstrap:
    """Tests for bootstrap-static endpoint."""

    @pytest.mark.asyncio
    async def test_get_bootstrap_static(self, mock_draft_bootstrap):
        """Test fetching bootstrap-static data."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_draft_bootstrap

            result = await client.get_bootstrap_static()

            mock_get.assert_called_once_with("bootstrap-static")
            assert "elements" in result
            assert "teams" in result
            assert len(result["elements"]) == 5

    @pytest.mark.asyncio
    async def test_get_bootstrap_static_caching(self, mock_draft_bootstrap):
        """Test bootstrap data is cached."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_draft_bootstrap

            # First call
            result1 = await client.get_bootstrap_static()
            # Second call should use cache
            result2 = await client.get_bootstrap_static()

            mock_get.assert_called_once()  # Only called once
            assert result1 == result2

    @pytest.mark.asyncio
    async def test_get_bootstrap_static_force_refresh(self, mock_draft_bootstrap):
        """Test force refresh bypasses cache."""
        client = FPLDraftClient()
        client._bootstrap_data = {"old": "data"}

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_draft_bootstrap

            result = await client.get_bootstrap_static(force_refresh=True)

            mock_get.assert_called_once()
            assert result == mock_draft_bootstrap


class TestFPLDraftClientLeague:
    """Tests for league-related endpoints."""

    @pytest.mark.asyncio
    async def test_get_league_details(self, mock_league_details):
        """Test fetching league details."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_league_details

            result = await client.get_league_details(12345)

            mock_get.assert_called_once_with("league/12345/details")
            assert result["league"]["name"] == "Test Draft League"
            assert len(result["league_entries"]) == 3

    @pytest.mark.asyncio
    async def test_get_league_ownership_status(self, mock_element_status):
        """Test fetching ownership status."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_element_status

            result = await client.get_league_ownership_status(12345)

            mock_get.assert_called_once_with("league/12345/element-status")
            assert len(result["element_status"]) == 5

    @pytest.mark.asyncio
    async def test_get_league_transactions(self, mock_transactions):
        """Test fetching league transactions."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_transactions

            result = await client.get_league_transactions(12345)

            mock_get.assert_called_once_with("draft/league/12345/transactions")
            assert len(result["transactions"]) == 3

    @pytest.mark.asyncio
    async def test_get_draft_choices_takes_the_league_id(self):
        """The path takes the league id: a draft id reads another league's draft."""
        client = FPLDraftClient()
        response = {
            "choices": [{"draft": 901, "index": 1, "round": 1, "pick": 1, "entry": 100, "element": 7}],
            "element_status": [],
            "idle": [],
        }

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = response

            result = await client.get_draft_choices(12345)

            mock_get.assert_called_once_with("draft/12345/choices")
            assert result == response

    @pytest.mark.asyncio
    async def test_get_league_trades(self):
        client = FPLDraftClient()
        payload = {"trades": [{"id": 1, "state": "p", "tradeitem_set": []}]}

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = payload

            result = await client.get_league_trades(12345)

            mock_get.assert_called_once_with("draft/league/12345/trades")
            assert result == payload


class TestIsProcessedTrade:
    """Only a processed trade moved players: `p` did, `v` (vetoed) did not, and
    a code nobody has seen move players is not read as a move (#458)."""

    @pytest.mark.parametrize(("state", "moved"), [("p", True), ("v", False), ("o", False), ("", False), (None, False)])
    def test_only_a_processed_trade_moved_players(self, state, moved):
        assert is_processed_trade({"state": state}) is moved

    def test_a_row_without_a_state_is_not_processed(self):
        assert is_processed_trade({}) is False


class TestFPLDraftClientEntry:
    """Tests for entry-related endpoints."""

    @pytest.mark.asyncio
    async def test_get_entry_profile(self):
        """Test fetching entry profile."""
        client = FPLDraftClient()
        mock_response = {"entry": {"id": 100, "name": "Test Team"}}

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            result = await client.get_entry_profile(100)

            mock_get.assert_called_once_with("entry/100/public")
            assert result["entry"]["id"] == 100

    @pytest.mark.asyncio
    async def test_get_entry_picks(self, mock_entry_picks):
        """Test fetching entry picks for gameweek."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_entry_picks

            result = await client.get_entry_picks(100, 25)

            mock_get.assert_called_once_with("entry/100/event/25")
            assert len(result["picks"]) == 2


class TestFPLDraftClientGameData:
    """Tests for game data endpoint."""

    @pytest.mark.asyncio
    async def test_get_game_state(self, mock_game_data):
        """Test fetching game state."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_game_data

            result = await client.get_game_state()

            mock_get.assert_called_once_with("game")
            assert result["current_event"] == 25


class TestFPLDraftClientSquad:
    """Tests for get_squad."""

    @pytest.mark.asyncio
    async def test_get_squad(self, mock_draft_bootstrap, mock_game_data, mock_entry_picks):
        """Test fetching enriched team squad."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint == "entry/100/event/25":
                    return mock_entry_picks
                return {}

            mock_get.side_effect = side_effect

            result = await client.get_squad(100)

            assert len(result) == 2
            assert result[0]["id"] == 1  # Salah
            assert result[1]["id"] == 3  # Saka


class TestFPLDraftClientOwnership:
    """Tests for ownership-related methods."""

    @pytest.mark.asyncio
    async def test_get_league_ownership(
        self, mock_draft_bootstrap, mock_league_details, mock_game_data, mock_entry_picks
    ):
        """Test building ownership from actual squads."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "league/12345/details":
                    return mock_league_details
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint.startswith("entry/") and "/event/" in endpoint:
                    return mock_entry_picks
                return {}

            mock_get.side_effect = side_effect

            result = await client.get_league_ownership(12345, mock_draft_bootstrap)

            # Each entry has Salah (1) and Saka (3), so they should be owned
            assert 1 in result or 3 in result

    @pytest.mark.asyncio
    async def test_get_league_ownership_reuses_supplied_league_details(
        self, mock_draft_bootstrap, mock_league_details, mock_game_data, mock_entry_picks
    ):
        """A caller holding league details should not pay for a second fetch.

        get_league_details is not memoised, unlike get_bootstrap_static and
        get_game_state, so callers that already fetched it pass it through.
        """
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "league/12345/details":
                    return mock_league_details
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint.startswith("entry/") and "/event/" in endpoint:
                    return mock_entry_picks
                return {}

            mock_get.side_effect = side_effect

            result = await client.get_league_ownership(
                12345, mock_draft_bootstrap, mock_league_details,
            )

            requested = [c.args[0] for c in mock_get.call_args_list]
            assert "league/12345/details" not in requested
            assert 1 in result or 3 in result

    @pytest.mark.asyncio
    async def test_get_available_players(
        self, mock_draft_bootstrap, mock_league_details, mock_game_data, mock_entry_picks
    ):
        """Test getting available players."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "league/12345/details":
                    return mock_league_details
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint.startswith("entry/") and "/event/" in endpoint:
                    return mock_entry_picks
                return {}

            mock_get.side_effect = side_effect

            result = await client.get_available_players(12345, mock_draft_bootstrap)

            # Players not owned and not unavailable with 0 points should be available
            available_ids = [p["id"] for p in result]
            # Unavailable player (id=5) with status='u' and 0 points should be excluded
            assert 5 not in available_ids

    @pytest.mark.asyncio
    async def test_get_available_players_excludes_unavailable_zero_points(self, mock_draft_bootstrap):
        """Test that unavailable players with 0 points are excluded."""
        client = FPLDraftClient()

        # Mock empty ownership
        with patch.object(client, "get_league_ownership", new_callable=AsyncMock) as mock_ownership:
            mock_ownership.return_value = {}

            result = await client.get_available_players(12345, mock_draft_bootstrap)

            # Player 5 has status='u' and total_points=0, should be excluded
            available_ids = [p["id"] for p in result]
            assert 5 not in available_ids
            # Other players should be available
            assert 1 in available_ids
            assert 2 in available_ids


class TestFPLDraftClientWaiverOrder:
    """Tests for waiver order."""

    @pytest.mark.asyncio
    async def test_get_waiver_order(self, mock_league_details):
        """Test getting waiver order (reverse of standings)."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_league_details

            result = await client.get_waiver_order(12345)

            # Should be sorted by rank descending (worst first)
            assert result[0]["rank"] == 3
            assert result[-1]["rank"] == 1


class TestFPLDraftClientReleases:
    """Tests for recent releases."""

    @pytest.mark.asyncio
    async def test_get_recent_releases(
        self, mock_draft_bootstrap, mock_game_data, mock_element_status, mock_transactions
    ):
        """Test getting recently released players."""
        client = FPLDraftClient()

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint == "league/12345/element-status":
                    return mock_element_status
                elif endpoint == "draft/league/12345/transactions":
                    return mock_transactions
                return {}

            mock_get.side_effect = side_effect

            result = await client.get_recent_releases(12345, mock_draft_bootstrap)

            # Transactions that dropped players should create releases
            # element_out values in transactions: 10, 11, 5
            # Player 5 exists in bootstrap (Unavailable), but 10 and 11 don't
            assert isinstance(result, list)

    @staticmethod
    async def _releases_for_gameweek(
        event, mock_draft_bootstrap, mock_game_data, mock_element_status,
    ):
        """Run get_recent_releases over one release in gameweek `event`.

        Element 4 (Gabriel) is both in the bootstrap and unowned, so nothing
        but the gameweek window can exclude it -- with a player the bootstrap
        does not carry, the row is dropped before the window is ever
        evaluated and an "it was filtered out" assertion holds vacuously.
        """
        client = FPLDraftClient()
        txns = {
            "transactions": [
                {"element_in": 3, "element_out": 4, "entry": 100,
                 "event": event, "kind": "w", "result": "a"},
            ]
        }

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "bootstrap-static":
                    return mock_draft_bootstrap
                elif endpoint == "game":
                    return mock_game_data
                elif endpoint == "league/12345/element-status":
                    return mock_element_status
                elif endpoint == "draft/league/12345/transactions":
                    return txns
                return {}

            mock_get.side_effect = side_effect
            return await client.get_recent_releases(
                12345, mock_draft_bootstrap, max_gameweeks_back=4,
            )

    @pytest.mark.asyncio
    async def test_get_recent_releases_filters_by_gameweek(
        self, mock_draft_bootstrap, mock_game_data, mock_element_status
    ):
        """Test that old releases are filtered out."""
        # Current GW is 25, max_gameweeks_back=4 means only GW 22+ included
        result = await self._releases_for_gameweek(
            20, mock_draft_bootstrap, mock_game_data, mock_element_status,
        )
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_get_recent_releases_keeps_a_release_inside_the_window(
        self, mock_draft_bootstrap, mock_game_data, mock_element_status
    ):
        """The positive control for the gameweek filter: the same release one
        gameweek inside the window is reported."""
        result = await self._releases_for_gameweek(
            22, mock_draft_bootstrap, mock_game_data, mock_element_status,
        )
        assert [r["player"]["id"] for r in result] == [4]


class TestFPLDraftClientParsePlayer:
    """Tests for parse_player method."""

    def test_parse_player(self):
        """Test parsing raw player data."""
        client = FPLDraftClient()
        raw_data = make_draft_player(
            id=1,
            web_name="Salah",
            first_name="Mohamed",
            second_name="Salah",
            team=14,
            element_type=3,
            form=7.5,
            points_per_game=6.2,
        )

        result = client.parse_player(raw_data, {14: 12})

        assert result["id"] == 1
        assert result["player_name"] == "Salah"
        assert result["team_id"] == 12
        assert result["position"] == "MID"
        assert result["form"] == 7.5
        assert result["ppg"] == 6.2

    def test_parse_player_handles_missing_fields(self):
        """Test parse_player with minimal data."""
        client = FPLDraftClient()
        raw_data = {"id": 1}

        result = client.parse_player(raw_data, {1: 1})

        assert result["id"] == 1
        assert result["player_name"] == ""
        assert result["total_points"] == 0
        assert result["appearances"] == 0
        assert result["form"] == 0.0
        assert result["code"] is None
        assert result["team_id"] is None

    @pytest.mark.parametrize(
        ("total_points", "points_per_game", "minutes", "expected"),
        [
            (50, 5.0, 900, 10),
            (37, 4.6, 700, 8),
            (40, 0.1, 3000, 38),   # near-zero ppg: capped at a season
            (0, 0.0, 0, 0),        # never played
            (0, 0.0, 35, 1),       # featured, but nothing to divide
            (-2, -0.7, 90, 3),     # negative ppg divides too
            (-4, 1.0, 0, 0),       # inconsistent row: floored, not negative
        ],
    )
    def test_parse_player_carries_appearances_as_the_main_game_derives_them(
        self, total_points, points_per_game, minutes, expected,
    ):
        """#471: the row must say how often the player featured.

        The waiver score's minutes factor reads 0 as "never played" and, from
        GW6, zeroes every per-90 term and the matchup bonus, so a player with
        minutes must never come out at 0. The count is derived as
        `Player.appearances` derives it, so the two games agree.
        """
        client = FPLDraftClient()
        raw = make_draft_player(
            total_points=total_points, points_per_game=points_per_game, minutes=minutes,
        )

        result = client.parse_player(raw, {1: 1})

        main = make_player(
            total_points=total_points, points_per_game=points_per_game, minutes=minutes,
        )
        assert result["appearances"] == main.appearances == expected

    def test_parse_player_tolerates_a_null_total(self):
        """A null total reads as 0 rather than failing the whole pool."""
        client = FPLDraftClient()

        result = client.parse_player(make_draft_player(total_points=None, minutes=90), {1: 1})

        assert (result["total_points"], result["appearances"]) == (0, 1)

    def test_parse_player_emits_the_main_games_team_id(self):
        """#423: the draft game numbers its clubs on its own, and everything a
        parsed player is scored against is keyed by the main game's ids.

        Draft club 14 is main-game club 12 here, so a row that passed the raw
        draft id through would score the player on another club's fixtures.
        """
        client = FPLDraftClient()

        result = client.parse_player(make_draft_player(team=14), {14: 12})

        assert result["team_id"] == 12

    def test_parse_player_leaves_an_untranslated_club_empty(self):
        """A club the main game does not list is no club, not the raw id."""
        client = FPLDraftClient()

        result = client.parse_player(make_draft_player(team=14), {13: 13})

        assert result["team_id"] is None

    def test_parse_player_keeps_the_cross_game_code(self):
        """#418: ``id`` is the draft game's own; ``code`` is the key both games share.

        A mid-season addition holds a different id in each game, so a consumer
        joining a draft row to a main-game row needs the code on the row.
        """
        client = FPLDraftClient()

        result = client.parse_player(make_draft_player(id=575, code=9575), {})

        assert result["id"] == 575
        assert result["code"] == 9575

    def test_parse_player_converts_string_fields(self):
        """Test that string fields are converted to appropriate types."""
        client = FPLDraftClient()
        raw_data = {
            "id": 1,
            "form": "7.5",
            "points_per_game": "6.2",
            "expected_goals": "10.5",
            "expected_assists": "5.3",
        }

        result = client.parse_player(raw_data, {})

        assert result["form"] == 7.5
        assert result["ppg"] == 6.2
        assert result["expected_goals"] == 10.5
        assert result["expected_assists"] == 5.3


class TestMatchDraftToMain:
    """#168: the draft->main join is `code`, not `web_name`."""

    def test_matches_a_player_the_main_game_renamed_mid_season(self):
        """The main game renamed Savinho to Savio and the draft game kept the
        old spelling. Same code, same team, so the join must still land."""
        draft = make_draft_player(id=403, code=510281, web_name="Savinho", team=19)
        main = make_player(id=403, code=510281, web_name="Sávio", team_id=19)

        assert match_draft_to_main([draft], [main], team_id_map={}) == {403: main}

    def test_matches_a_player_the_draft_game_moved_to_another_club(self):
        """A January transfer lands in the two bootstraps at different times,
        so the team half of the old key is no more stable than the name."""
        draft = make_draft_player(id=403, code=510281, web_name="Savinho", team=19)
        main = make_player(id=403, code=510281, web_name="Savinho", team_id=13)

        assert match_draft_to_main([draft], [main], team_id_map={}) == {403: main}

    def test_falls_back_to_name_and_team_for_a_codeless_element(self):
        draft = make_draft_player(id=900, web_name="Star", team=1)
        main = make_player(id=5, web_name="Star", team_id=7)

        assert match_draft_to_main([draft], [main], team_id_map={1: 7}) == {900: main}

    def test_name_fallback_folds_diacritics(self):
        draft = make_draft_player(id=900, web_name="Gyokeres", team=1)
        main = make_player(id=5, web_name="Gyökeres", team_id=7)

        assert match_draft_to_main([draft], [main], team_id_map={1: 7}) == {900: main}

    def test_name_fallback_keeps_two_players_of_the_same_name_apart(self):
        draft = make_draft_player(id=900, web_name="Martinez", team=1)
        wrong_club = make_player(id=5, web_name="Martinez", team_id=2)

        assert match_draft_to_main([draft], [wrong_club], team_id_map={1: 7}) == {}

    def test_name_fallback_reads_the_draft_team_in_the_main_games_numbering(self):
        """#423: draft club 1 is main-game club 7 here, so the namesake at
        main-game club 1 plays for a different club and must not match."""
        draft = make_draft_player(id=900, web_name="Martinez", team=1)
        namesake = make_player(id=5, web_name="Martinez", team_id=1)

        assert match_draft_to_main([draft], [namesake], team_id_map={1: 7}) == {}

    def test_name_fallback_needs_the_club_to_translate(self):
        """A club the map has no main-game id for has nothing to be compared
        with, so a codeless element goes unmatched rather than paired on a
        guess."""
        draft = make_draft_player(id=900, web_name="Star", team=1)
        main = make_player(id=5, web_name="Star", team_id=1)

        assert match_draft_to_main([draft], [main], team_id_map={}) == {}

    def test_a_genuine_miss_stays_missing(self):
        draft = make_draft_player(id=900, code=111, web_name="Mystery", team=1)
        main = make_player(id=5, code=222, web_name="Star", team_id=1)

        assert match_draft_to_main([draft], [main], team_id_map={}) == {}

    def test_a_zero_code_is_not_a_join_key(self):
        """`Player.code` defaults to 0 for a main player built without one, so
        a codeless draft element must not collide with it on a falsy key."""
        draft = make_draft_player(id=900, code=0, web_name="Mystery", team=1)
        main = make_player(id=5, code=0, web_name="Star", team_id=1)

        assert match_draft_to_main([draft], [main], team_id_map={}) == {}


class TestMatchDraftTeamsToMain:
    """#423: the two games number their clubs independently; `code` is shared."""

    def test_joins_on_code_where_the_numberings_diverge(self):
        draft_teams = [
            make_draft_team(id=1, code=3, short_name="ARS"),
            make_draft_team(id=2, code=43, short_name="MCI"),
        ]
        main_teams = [
            make_team(id=1, code=43, short_name="MCI"),
            make_team(id=2, code=3, short_name="ARS"),
        ]

        assert match_draft_teams_to_main(draft_teams, main_teams) == {1: 2, 2: 1}

    def test_matches_identical_numberings_one_to_one(self):
        """The live state today: every club shares its id across the games."""
        draft_teams = [make_draft_team(id=i) for i in range(1, 21)]
        main_teams = [make_team(id=i) for i in range(1, 21)]

        assert match_draft_teams_to_main(draft_teams, main_teams) == {i: i for i in range(1, 21)}

    def test_a_club_missing_from_the_main_game_is_absent(self):
        draft_teams = [make_draft_team(id=1, code=3), make_draft_team(id=2, code=999)]
        main_teams = [make_team(id=1, code=3)]

        assert match_draft_teams_to_main(draft_teams, main_teams) == {1: 1}

    def test_a_draft_club_without_a_code_is_absent(self):
        """Absent, never matched on its id: the id is what cannot be trusted."""
        draft_teams = [{"id": 1, "name": "Arsenal", "short_name": "ARS"}]
        main_teams = [make_team(id=1, code=3)]

        assert match_draft_teams_to_main(draft_teams, main_teams) == {}

    def test_a_zero_code_is_not_a_join_key(self):
        draft_teams = [make_draft_team(id=1, code=0)]
        main_teams = [make_team(id=1, code=0)]

        assert match_draft_teams_to_main(draft_teams, main_teams) == {}


class TestDraftPlayerTeamId:
    def test_translates_the_draft_team(self):
        assert draft_player_team_id(make_draft_player(team=14), {14: 12}) == 12

    def test_an_untranslated_club_is_none_not_the_raw_id(self):
        assert draft_player_team_id(make_draft_player(team=14), {}) is None

    def test_a_player_without_a_team_is_none(self):
        assert draft_player_team_id({"id": 1}, {1: 1}) is None

    def test_a_matched_main_player_answers_first(self):
        """His club is already in the main game's numbering, so it holds even
        when the draft list carries a club the main game has dropped."""
        main = make_player(id=5, team_id=7)

        assert draft_player_team_id(make_draft_player(team=14), {}, main_player=main) == 7
        assert draft_player_team_id(make_draft_player(team=14), {14: 12}, main_player=main) == 7


class TestUntranslatedDraftTeams:
    def test_names_the_clubs_the_map_lacks(self):
        teams = [
            make_draft_team(id=1, short_name="ARS"),
            make_draft_team(id=2, short_name="NOW"),
            make_draft_team(id=3, short_name="HUL"),
        ]

        assert untranslated_draft_teams(teams, {1: 1}) == ["HUL", "NOW"]

    def test_empty_when_every_club_translates(self):
        assert untranslated_draft_teams([make_draft_team(id=1)], {1: 4}) == []


class TestDraftTransactionResultCodes:
    """Issue #329: the feed's two denial codes do not mean the same thing, and
    collapsing them either loses a manager's attempt or invents one."""

    def test_an_accepted_row_is_the_only_completed_move(self):
        assert is_accepted_transaction({"result": "a"}) is True
        assert is_accepted_transaction({"result": "di"}) is False
        assert is_accepted_transaction({"result": "do"}) is False

    def test_a_row_with_no_result_at_all_is_not_treated_as_accepted(self):
        assert is_accepted_transaction({}) is False

    def test_a_di_row_is_denied_on_the_incoming_player(self):
        assert is_denied_on_the_incoming_player({"result": "di"}) is True

    def test_a_do_row_is_not_an_attempt(self):
        """`do` means the manager's own earlier accepted claim had already
        used that drop. Counting it as a failed attempt would report the
        manager who *won* the claim as having lost one."""
        assert is_denied_on_the_incoming_player({"result": "do"}) is False

    def test_an_accepted_row_is_not_denied(self):
        assert is_denied_on_the_incoming_player({"result": "a"}) is False


def _claim(element_in, result, priority=None, element_out=1, index=0, kind="w"):
    """One waiver row. `index` is the league-wide processing order the engine
    works the batch through; a free-agent row carries none, since free agency
    opens only once the whole batch is done."""
    return {
        "element_in": element_in, "element_out": element_out,
        "result": result, "priority": priority, "kind": kind,
        "index": None if kind == "f" else index,
    }


def _free_agent(element_in, result="a", element_out=1):
    return _claim(element_in, result, element_out=element_out, kind="f")


class TestResolveLostClaims:
    """Issue #342: `di` says the incoming player was gone when the claim
    processed, not who took him -- and a manager's own winning claim removes
    him exactly as a rival's does."""

    def test_a_di_for_a_player_the_manager_won_is_not_a_loss(self):
        """The reported shape: won at priority 1, listed again at priority 4
        against a different drop, denied because he had just taken the player
        himself. Reporting it inverts the outcome for the manager who won."""
        claims = resolve_lost_claims([
            _claim(167, "a", priority=1, element_out=55, index=2),
            _claim(167, "di", priority=4, element_out=222, index=9),
        ])
        assert claims == []

    def test_a_di_for_a_player_a_rival_won_is_still_a_loss(self):
        claims = resolve_lost_claims([
            _claim(167, "a", priority=1, index=2),
            _claim(569, "di", priority=2, index=7),
        ])
        assert [c["element_in"] for c in claims] == [569]

    def test_signing_the_player_as_a_free_agent_later_does_not_erase_the_loss(self):
        """A rival won the waiver, dropped him, and the manager picked him up
        in free agency. Free agency runs after the whole waiver batch, so that
        pickup cannot be what denied the claim -- he lost the waiver and got
        him another way, which is the case `contested_draft_claims`
        documents."""
        claims = resolve_lost_claims([
            _claim(900, "di", priority=1, index=3),
            _free_agent(900),
        ])
        assert [c["element_in"] for c in claims] == [900]

    def test_a_free_agent_pickup_of_an_untouched_player_changes_nothing(self):
        claims = resolve_lost_claims([
            _claim(900, "di", priority=1, index=3),
            _free_agent(115),
        ])
        assert [c["element_in"] for c in claims] == [900]

    def test_only_a_claim_that_landed_first_clears_the_denial(self):
        """Ordering, not end-of-gameweek ownership: an accepted row later in
        the batch cannot have caused an earlier denial."""
        claims = resolve_lost_claims([
            _claim(900, "di", priority=1, index=3),
            _claim(900, "a", priority=2, index=8),
        ])
        assert [c["element_in"] for c in claims] == [900]

    def test_one_player_claimed_several_times_is_one_loss(self):
        """A conditional chain offering different drops for one target: he
        wanted the player once, so three denials are not three defeats."""
        claims = resolve_lost_claims([
            _claim(569, "di", priority=2, element_out=55),
            _claim(569, "di", priority=5, element_out=222),
            _claim(569, "di", priority=8, element_out=333),
        ])
        assert len(claims) == 1
        assert claims[0]["priority"] == 2

    def test_the_best_priority_survives_whatever_order_the_feed_sends(self):
        claims = resolve_lost_claims([
            _claim(569, "di", priority=6),
            _claim(569, "di", priority=3),
        ])
        assert claims[0]["priority"] == 3

    def test_a_claim_with_no_priority_loses_to_a_numbered_one(self):
        """A free-agent denial carries no priority; a numbered claim is the
        more informative row to keep."""
        claims = resolve_lost_claims([
            _claim(569, "di", priority=None),
            _claim(569, "di", priority=4),
        ])
        assert claims[0]["priority"] == 4

    def test_a_do_row_is_still_never_an_attempt(self):
        claims = resolve_lost_claims([
            _claim(167, "a", priority=1, element_out=55, index=2),
            _claim(569, "do", priority=2, element_out=55, index=7),
        ])
        assert claims == []

    def test_the_full_reported_gameweek_resolves_to_the_distinct_losses(self):
        """One manager's real gameweek: six denied rows, two players claimed
        twice each, and one denial for the player he won."""
        claims = resolve_lost_claims([
            _claim(167, "a", priority=1, element_out=55, index=2),
            _claim(569, "di", priority=2, element_out=55, index=7),
            _claim(316, "di", priority=3, element_out=55, index=8),
            _claim(167, "di", priority=4, element_out=222, index=9),
            _claim(569, "di", priority=5, element_out=222, index=10),
            _claim(316, "di", priority=6, element_out=222, index=11),
            _claim(115, "di", priority=7, element_out=503, index=12),
        ])
        assert [(c["element_in"], c["priority"]) for c in claims] == [
            (569, 2), (316, 3), (115, 7),
        ]

    def test_a_gameweek_with_nothing_denied_resolves_to_nothing(self):
        assert resolve_lost_claims([_claim(167, "a", priority=1)]) == []

    def test_a_row_with_no_incoming_player_is_skipped(self):
        assert resolve_lost_claims([_claim(None, "di", priority=1)]) == []


class TestRecentReleasesIgnoreDeniedClaims:
    """A denied claim names the player it would have dropped, but nobody was
    released -- so it must not be reported as a release (issue #329)."""

    @pytest.mark.asyncio
    async def test_a_denied_claims_drop_is_not_a_release(self, mock_draft_bootstrap):
        client = FPLDraftClient()
        txns = {
            "transactions": [
                {"element_in": 1, "element_out": 4, "entry": 100, "event": 25, "kind": "w", "result": "di"},
                {"element_in": 2, "element_out": 5, "entry": 100, "event": 25, "kind": "w", "result": "do"},
            ]
        }
        with (
            patch.object(client, "get_game_state", new_callable=AsyncMock) as mock_state,
            patch.object(client, "get_league_ownership_status", new_callable=AsyncMock) as mock_status,
            patch.object(client, "get_league_transactions", new_callable=AsyncMock) as mock_txns,
        ):
            mock_state.return_value = {"current_event": 25}
            mock_status.return_value = {"element_status": []}
            mock_txns.return_value = txns

            assert await client.get_recent_releases(12345, mock_draft_bootstrap) == []

    @pytest.mark.asyncio
    async def test_an_accepted_claims_drop_is_still_a_release(self, mock_draft_bootstrap):
        client = FPLDraftClient()
        txns = {
            "transactions": [
                {"element_in": 1, "element_out": 4, "entry": 100, "event": 25, "kind": "w", "result": "a"},
            ]
        }
        with (
            patch.object(client, "get_game_state", new_callable=AsyncMock) as mock_state,
            patch.object(client, "get_league_ownership_status", new_callable=AsyncMock) as mock_status,
            patch.object(client, "get_league_transactions", new_callable=AsyncMock) as mock_txns,
        ):
            mock_state.return_value = {"current_event": 25}
            mock_status.return_value = {"element_status": []}
            mock_txns.return_value = txns

            releases = await client.get_recent_releases(12345, mock_draft_bootstrap)
            assert [r["player"]["id"] for r in releases] == [4]


class TestLeagueOwnershipUnreadSquads:
    """A rival squad the ownership read could not fetch is recorded, not dropped (#438)."""

    @pytest.mark.asyncio
    async def test_a_failed_picks_fetch_is_recorded_by_entry_name(
        self, mock_draft_bootstrap, mock_league_details, mock_game_data, mock_entry_picks, caplog
    ):
        import logging

        client = FPLDraftClient()
        assert client.unread_squads == {}

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "game":
                    return mock_game_data
                if endpoint.startswith("entry/101/"):
                    raise httpx.ConnectError("down")
                if endpoint.startswith("entry/") and "/event/" in endpoint:
                    return mock_entry_picks
                return {}

            mock_get.side_effect = side_effect
            with caplog.at_level(logging.WARNING):
                result = await client.get_league_ownership(
                    12345, mock_draft_bootstrap, mock_league_details,
                )

        assert client.unread_squads == {101: "Team B"}
        # The readable squads still own their players; the unread one owns none.
        assert result
        assert 101 not in set(result.values())
        assert "Could not read the squad of Team B (entry 101)" in caplog.text

    @pytest.mark.asyncio
    async def test_a_clean_read_resets_the_record(
        self, mock_draft_bootstrap, mock_league_details, mock_game_data, mock_entry_picks
    ):
        client = FPLDraftClient()
        client.unread_squads = {101: "Team B"}

        with patch.object(client, "_get", new_callable=AsyncMock) as mock_get:
            async def side_effect(endpoint):
                if endpoint == "game":
                    return mock_game_data
                if endpoint.startswith("entry/") and "/event/" in endpoint:
                    return mock_entry_picks
                return {}

            mock_get.side_effect = side_effect
            await client.get_league_ownership(12345, mock_draft_bootstrap, mock_league_details)

        assert client.unread_squads == {}
