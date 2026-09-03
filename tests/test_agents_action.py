"""Tests for action agents (WaiverAgent)."""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fpl_cli.agents.action.waiver import WaiverAgent
from fpl_cli.agents.base import AgentStatus
from fpl_cli.models.player import PlayerPosition
from fpl_cli.services.player_prior import PlayerPrior
from fpl_cli.services.scoring import ScoringContext, ScoringData
from tests.conftest import (
    make_draft_league_entry,
    make_draft_player,
    make_draft_standing,
    make_draft_team,
    make_fixture,
    make_player,
    make_team,
)

# --- Fixtures ---

@pytest.fixture
def mock_draft_bootstrap():
    """Mock draft bootstrap-static response."""
    return {
        "elements": [
            make_draft_player(id=1, web_name="Salah", team=14, element_type=3, form=7.0, minutes=1800),
            make_draft_player(id=2, web_name="Haaland", team=13, element_type=4, form=8.0, minutes=1900),
            make_draft_player(id=3, web_name="Saka", team=1, element_type=3, form=6.0, minutes=1700),
            make_draft_player(id=4, web_name="Gabriel", team=1, element_type=2, form=5.0, minutes=1800),
            make_draft_player(id=5, web_name="Raya", team=1, element_type=1, form=4.0, minutes=1900),
            # Low form players for squad
            make_draft_player(id=10, web_name="Bench1", team=5, element_type=3, form=2.0, minutes=500),
            make_draft_player(id=11, web_name="Bench2", team=6, element_type=4, form=1.5, minutes=400),
            make_draft_player(id=12, web_name="Bench3", team=7, element_type=2, form=2.5, minutes=600),
        ],
        "teams": [
            make_draft_team(id=1, name="Arsenal", short_name="ARS"),
            make_draft_team(id=5, name="Tottenham", short_name="TOT"),
            make_draft_team(id=6, name="Brighton", short_name="BHA"),
            make_draft_team(id=7, name="West Ham", short_name="WHU"),
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
            make_draft_league_entry(id=1, entry_id=100, entry_name="My Team"),
            make_draft_league_entry(id=2, entry_id=101, entry_name="Rival Team"),
        ],
        "standings": [
            make_draft_standing(league_entry=1, rank=1, total=500, event_total=60),
            make_draft_standing(league_entry=2, rank=2, total=450, event_total=55),
        ],
    }


@pytest.fixture
def mock_game_data():
    """Mock game data response."""
    return {"current_event": 25}


@pytest.fixture
def mock_entry_picks():
    """Mock entry picks with low-form players."""
    return {
        "picks": [
            {"element": 10, "position": 1},  # Bench1 (MID, form 2.0)
            {"element": 11, "position": 2},  # Bench2 (FWD, form 1.5)
            {"element": 12, "position": 3},  # Bench3 (DEF, form 2.5)
        ]
    }


def _mock_scoring_data(players: list[Any] | None = None) -> ScoringData:
    """Fixed ScoringData standing in for prepare_scoring_data's upstream fetches.

    Teams mirror the draft bootstrap fixture so matchup enrichment resolves.
    *players* are the main-game ``Player`` models the draft elements join to;
    the default empty list leaves every join unmatched, which is what the
    tests that do not care about the join want.
    """
    teams = [
        make_team(id=1, short_name="ARS"),
        make_team(id=5, short_name="TOT"),
        make_team(id=6, short_name="BHA"),
        make_team(id=7, short_name="WHU"),
        make_team(id=13, short_name="MCI"),
        make_team(id=14, short_name="LIV"),
    ]
    team_map = {t.id: t for t in teams}
    fixtures = [
        make_fixture(id=1, gameweek=26, home_team_id=1, away_team_id=14, home_difficulty=3, away_difficulty=3),
        make_fixture(id=2, gameweek=26, home_team_id=13, away_team_id=5, home_difficulty=2, away_difficulty=4),
        make_fixture(id=3, gameweek=26, home_team_id=6, away_team_id=7, home_difficulty=3, away_difficulty=3),
    ]

    ratings_service = MagicMock()
    ratings_service.get_positional_fdr.return_value = 3.0
    ratings_service.get_matchup_score.return_value = {
        "matchup_score": 5.5,
        "attack_matchup": 5.0,
        "defence_matchup": 5.0,
        "form_differential": 0.1,
        "position_differential": 0.05,
        "reasoning": ["Average"],
    }

    scoring_ctx = ScoringContext(
        team_map=team_map,
        team_fixture_map={
            1: [{"fixture": fixtures[0], "is_home": True}],
            14: [{"fixture": fixtures[0], "is_home": False}],
            13: [{"fixture": fixtures[1], "is_home": True}],
            5: [{"fixture": fixtures[1], "is_home": False}],
            6: [{"fixture": fixtures[2], "is_home": True}],
            7: [{"fixture": fixtures[2], "is_home": False}],
        },
        ratings_service=ratings_service,
        next_gw_id=26,
    )

    return ScoringData(
        teams=teams,
        team_map=team_map,
        all_fixtures=fixtures,
        next_gw_fixtures=fixtures,
        next_gw_id=26,
        next_gw={"id": 26, "is_next": True},
        scoring_ctx=scoring_ctx,
        ratings_service=ratings_service,
        players=players or [],
        player_histories={},
        player_priors=None,
    )


@contextmanager
def _stub_upstream_fetches(players: list[Any] | None = None):
    """Patch the two seams that reach past the draft client.

    Shared by every test that drives ``WaiverAgent.run`` end to end.
    """
    with (
        patch(
            "fpl_cli.agents.action.waiver.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=_mock_scoring_data(players),
        ),
        patch(
            "fpl_cli.agents.action.waiver.fetch_understat_lookup",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        yield


@pytest.fixture
def mock_waiver_order():
    """Mock waiver order (reverse standings)."""
    return [
        {"league_entry": 2, "rank": 2, "entry_id": 101},
        {"league_entry": 1, "rank": 1, "entry_id": 100},
    ]


# --- TestWaiverAgent ---

class TestWaiverAgentInit:
    """Tests for WaiverAgent initialization."""

    def test_agent_initialization(self):
        """Test default initialization."""
        agent = WaiverAgent()
        assert agent.name == "WaiverAgent"
        assert agent.league_id is None
        assert agent.entry_id is None

    def test_agent_initialization_with_config(self):
        """Test initialization with config."""
        config = {"draft_league_id": 12345, "draft_entry_id": 100}
        agent = WaiverAgent(config=config)
        assert agent.league_id == 12345
        assert agent.entry_id == 100


class TestWaiverAgentRun:
    """Tests for WaiverAgent run method."""

    @pytest.fixture(autouse=True)
    def _stub_fpl_client_fetches(self):
        """Patch the two seams that reach past the draft client.

        WaiverAgent holds a second client (self.fpl_client) that the tests
        below never patch: prepare_scoring_data pulls bootstrap-static,
        fixtures and both GitHub datasets through it, and
        fetch_understat_lookup scrapes understat.com. Patching these two
        calls removes every downstream fetch at once, so the tests neither
        depend on the network nor report unrelated upstream breakage as a
        WaiverAgent failure.
        """
        with _stub_upstream_fetches():
            yield

    @pytest.mark.asyncio
    async def test_run_missing_league_id(self):
        """Test run fails without league_id."""
        agent = WaiverAgent()
        result = await agent.run()

        assert result.status == AgentStatus.FAILED
        assert "No draft league ID" in result.message

    @pytest.mark.asyncio
    async def test_run_success(self, mock_draft_bootstrap, mock_league_details, mock_game_data):
        """Test successful waiver analysis."""
        agent = WaiverAgent(config={"draft_league_id": 12345})

        with patch.object(agent.client, "get_bootstrap_static", new_callable=AsyncMock) as mock_bootstrap, \
             patch.object(agent.client, "get_available_players", new_callable=AsyncMock) as mock_available, \
             patch.object(agent.client, "get_recent_releases", new_callable=AsyncMock) as mock_releases, \
             patch.object(agent.client, "get_league_details", new_callable=AsyncMock) as mock_details, \
             patch.object(agent.client, "get_waiver_order", new_callable=AsyncMock) as mock_order:

            mock_bootstrap.return_value = mock_draft_bootstrap
            # Return Salah and Saka as available
            mock_available.return_value = [
                mock_draft_bootstrap["elements"][0],  # Salah
                mock_draft_bootstrap["elements"][2],  # Saka
            ]
            mock_releases.return_value = []
            mock_details.return_value = mock_league_details
            mock_order.return_value = [{"league_entry": 1, "rank": 1}]

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            assert "top_targets" in result.data
            assert "targets_by_position" in result.data
            assert "recent_releases" in result.data
            assert result.data["league_id"] == 12345

    @pytest.mark.asyncio
    async def test_run_with_entry_id(
        self, mock_draft_bootstrap, mock_league_details, mock_game_data, mock_entry_picks
    ):
        """Test run with entry_id fetches squad."""
        agent = WaiverAgent(config={"draft_league_id": 12345, "draft_entry_id": 100})

        with patch.object(agent.client, "get_bootstrap_static", new_callable=AsyncMock) as mock_bootstrap, \
             patch.object(agent.client, "get_available_players", new_callable=AsyncMock) as mock_available, \
             patch.object(agent.client, "get_recent_releases", new_callable=AsyncMock) as mock_releases, \
             patch.object(agent.client, "get_league_details", new_callable=AsyncMock) as mock_details, \
             patch.object(agent.client, "get_game_state", new_callable=AsyncMock) as mock_game, \
             patch.object(agent.client, "get_entry_picks", new_callable=AsyncMock) as mock_picks, \
             patch.object(agent.client, "get_waiver_order", new_callable=AsyncMock) as mock_order:

            mock_bootstrap.return_value = mock_draft_bootstrap
            mock_available.return_value = [mock_draft_bootstrap["elements"][0]]  # Salah
            mock_releases.return_value = []
            mock_details.return_value = mock_league_details
            mock_game.return_value = mock_game_data
            mock_picks.return_value = mock_entry_picks
            mock_order.return_value = [{"league_entry": 1, "rank": 1, "entry_id": 100}]

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            assert "current_squad" in result.data
            assert "squad_weaknesses" in result.data

    @pytest.mark.asyncio
    async def test_run_handles_api_error(self):
        """Test run handles API errors gracefully."""
        agent = WaiverAgent(config={"draft_league_id": 12345})

        with patch.object(agent.client, "get_bootstrap_static", new_callable=AsyncMock) as mock_bootstrap:
            mock_bootstrap.side_effect = Exception("API Error")

            result = await agent.run()

            assert result.status == AgentStatus.FAILED
            assert "API Error" in result.errors[0]

    @pytest.mark.asyncio
    async def test_run_finds_matching_entry_by_entry_id(
        self, mock_draft_bootstrap, mock_league_details, mock_game_data, mock_entry_picks
    ):
        """Test that entry_id matches against league entry_id."""
        agent = WaiverAgent(config={"draft_league_id": 12345, "draft_entry_id": 100})

        with patch.object(agent.client, "get_bootstrap_static", new_callable=AsyncMock) as mock_bootstrap, \
             patch.object(agent.client, "get_available_players", new_callable=AsyncMock) as mock_available, \
             patch.object(agent.client, "get_recent_releases", new_callable=AsyncMock) as mock_releases, \
             patch.object(agent.client, "get_league_details", new_callable=AsyncMock) as mock_details, \
             patch.object(agent.client, "get_game_state", new_callable=AsyncMock) as mock_game, \
             patch.object(agent.client, "get_entry_picks", new_callable=AsyncMock) as mock_picks, \
             patch.object(agent.client, "get_waiver_order", new_callable=AsyncMock) as mock_order:

            mock_bootstrap.return_value = mock_draft_bootstrap
            mock_available.return_value = []
            mock_releases.return_value = []
            mock_details.return_value = mock_league_details
            mock_game.return_value = mock_game_data
            mock_picks.return_value = mock_entry_picks
            mock_order.return_value = []

            result = await agent.run()

            # Should have found the team by entry_id=100
            assert result.status == AgentStatus.SUCCESS
            mock_picks.assert_called()

    @pytest.mark.asyncio
    async def test_run_includes_recent_releases(self, mock_draft_bootstrap, mock_league_details):
        """Test that recent releases are included with resolved manager names."""
        agent = WaiverAgent(config={"draft_league_id": 12345})

        mock_release = {
            "player": mock_draft_bootstrap["elements"][0],  # Salah
            "dropped_by": 101,  # entry_id from mock_league_details -> "Rival Team"
            "gameweek": 30,
        }

        with patch.object(agent.client, "get_bootstrap_static", new_callable=AsyncMock) as mock_bootstrap, \
             patch.object(agent.client, "get_available_players", new_callable=AsyncMock) as mock_available, \
             patch.object(agent.client, "get_recent_releases", new_callable=AsyncMock) as mock_releases, \
             patch.object(agent.client, "get_league_details", new_callable=AsyncMock) as mock_details, \
             patch.object(agent.client, "get_waiver_order", new_callable=AsyncMock) as mock_order:

            mock_bootstrap.return_value = mock_draft_bootstrap
            mock_available.return_value = []
            mock_releases.return_value = [mock_release]
            mock_details.return_value = mock_league_details
            mock_order.return_value = []

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            assert len(result.data["recent_releases"]) == 1
            assert result.data["recent_releases"][0]["dropped_by"] == "John Doe"
            assert result.data["recent_releases"][0]["gameweek"] == 30

    @pytest.mark.asyncio
    async def test_run_recent_releases_failure_degrades_gracefully(self, mock_draft_bootstrap):
        """Test that get_recent_releases failure doesn't block waivers."""
        agent = WaiverAgent(config={"draft_league_id": 12345})

        with patch.object(agent.client, "get_bootstrap_static", new_callable=AsyncMock) as mock_bootstrap, \
             patch.object(agent.client, "get_available_players", new_callable=AsyncMock) as mock_available, \
             patch.object(agent.client, "get_recent_releases", new_callable=AsyncMock) as mock_releases, \
             patch.object(agent.client, "get_waiver_order", new_callable=AsyncMock) as mock_order:

            mock_bootstrap.return_value = mock_draft_bootstrap
            mock_available.return_value = []
            mock_releases.side_effect = Exception("Transaction API unavailable")
            mock_order.return_value = []

            result = await agent.run()

            assert result.status == AgentStatus.SUCCESS
            assert result.data["recent_releases"] == []


# --- Full unowned waiver pool ---

GOLDEN_RANKED = Path(__file__).parent / "fixtures" / "waiver_ranked_golden.json"

_POOL_TEAM_IDS = (1, 5, 6, 7, 13, 14)


def _wide_bootstrap() -> dict[str, Any]:
    """Draft bootstrap holding more players than ``top_targets`` can carry.

    18 fit players plus one flagged returnee whose zero form and 90 minutes pin
    it below every one of them, so the returnee is reachable only through the
    full ``pool`` roster. Player 50 stands in for a rival-owned player: it is
    never offered as available.
    """
    elements = [
        make_draft_player(
            id=i,
            web_name=f"Player{i:02d}",
            team=_POOL_TEAM_IDS[i % len(_POOL_TEAM_IDS)],
            element_type=(i % 4) + 1,
            form=8.0 - i * 0.2,
            points_per_game=7.0 - i * 0.2,
            minutes=1800,
        )
        for i in range(1, 19)
    ]
    elements.append(
        make_draft_player(
            id=99,
            web_name="Returnee",
            team=1,
            element_type=3,
            form=0.0,
            points_per_game=0.0,
            minutes=90,
            status="i",
            news="Hamstring injury - expected back 12 Apr",
            chance_of_playing_next_round=25,
        )
    )
    elements.append(make_draft_player(id=50, web_name="Owned", team=5, element_type=2))
    return {
        "elements": elements,
        "teams": [
            make_draft_team(id=1, name="Arsenal", short_name="ARS"),
            make_draft_team(id=5, name="Tottenham", short_name="TOT"),
            make_draft_team(id=6, name="Brighton", short_name="BHA"),
            make_draft_team(id=7, name="West Ham", short_name="WHU"),
            make_draft_team(id=13, name="Man City", short_name="MCI"),
            make_draft_team(id=14, name="Liverpool", short_name="LIV"),
        ],
    }


async def _run_waivers(
    available_ids: list[int],
    entry_id: int | None = None,
    picks: list[int] | None = None,
):
    """Drive ``WaiverAgent.run`` over ``_wide_bootstrap`` with every seam stubbed."""
    bootstrap = _wide_bootstrap()
    by_id = {p["id"]: p for p in bootstrap["elements"]}
    league_details = {
        "league": {"id": 12345, "name": "Test Draft League"},
        "league_entries": [make_draft_league_entry(id=1, entry_id=100, entry_name="My Team")],
        "standings": [make_draft_standing(league_entry=1, rank=1, total=500, event_total=60)],
    }
    config: dict[str, Any] = {"draft_league_id": 12345}
    if entry_id is not None:
        config["draft_entry_id"] = entry_id

    agent = WaiverAgent(config=config)
    with (
        _stub_upstream_fetches(),
        patch.object(agent.client, "get_bootstrap_static", new_callable=AsyncMock, return_value=bootstrap),
        patch.object(
            agent.client, "get_available_players", new_callable=AsyncMock,
            return_value=[by_id[i] for i in available_ids],
        ),
        patch.object(agent.client, "get_recent_releases", new_callable=AsyncMock, return_value=[]),
        patch.object(agent.client, "get_league_details", new_callable=AsyncMock, return_value=league_details),
        patch.object(agent.client, "get_game_state", new_callable=AsyncMock, return_value={"current_event": 25}),
        patch.object(
            agent.client, "get_entry_picks", new_callable=AsyncMock,
            return_value={"picks": [{"element": pid, "position": n} for n, pid in enumerate(picks or [], 1)]},
        ),
        patch.object(agent.client, "get_waiver_order", new_callable=AsyncMock, return_value=[]),
    ):
        return await agent.run()


_ALL_AVAILABLE = [*range(1, 19), 99]


class TestWaiverAgentPool:
    """The result carries the full unowned roster, not just the ranked subset."""

    @pytest.mark.asyncio
    async def test_flagged_player_absent_from_top_targets_is_in_pool(self):
        """A flagged returnee can never reach the top 15 — the pool is its only route."""
        result = await _run_waivers(_ALL_AVAILABLE)

        assert result.status == AgentStatus.SUCCESS
        assert "Returnee" not in {t["player_name"] for t in result.data["top_targets"]}
        assert "Returnee" in {p["player_name"] for p in result.data["pool"]}

    @pytest.mark.asyncio
    async def test_pool_covers_every_unowned_player(self):
        result = await _run_waivers(_ALL_AVAILABLE)

        assert {p["id"] for p in result.data["pool"]} == set(_ALL_AVAILABLE)

    @pytest.mark.asyncio
    async def test_pool_entry_is_lean_id_name_position_team(self):
        """The roster is an identity record, not a second copy of the scored target."""
        result = await _run_waivers([99])

        assert result.data["pool"] == [
            {"id": 99, "player_name": "Returnee", "position": "MID", "team_short": "ARS"},
        ]

    @pytest.mark.asyncio
    async def test_owned_players_never_appear_in_pool(self):
        """Player 50 is rostered, so it is in the squad and out of the pool."""
        result = await _run_waivers([1, 2, 3], entry_id=100, picks=[50])

        assert {p["id"] for p in result.data["pool"]} == {1, 2, 3}
        assert 50 in {p["id"] for p in result.data["current_squad"]}

    @pytest.mark.asyncio
    async def test_ranked_output_is_untouched_by_the_pool_field(self):
        """Golden guard: the two long-standing keys must not shift.

        The fixture was generated from the agent as it stood before ``pool``
        existed, so any drift in ``top_targets`` or ``targets_by_position``
        fails here rather than in the skill scripts that read them.
        """
        result = await _run_waivers(_ALL_AVAILABLE)
        golden = json.loads(GOLDEN_RANKED.read_text(encoding="utf-8"))

        assert result.data["top_targets"] == golden["top_targets"]
        assert result.data["targets_by_position"] == golden["targets_by_position"]

    @pytest.mark.asyncio
    async def test_empty_available_list_yields_empty_pool(self):
        result = await _run_waivers([])

        assert result.data["pool"] == []


class TestWaiverAgentShrinkage:
    """Regression tests for #122: shrinkage must not hoist unavailable players."""

    @staticmethod
    def _pool() -> list[dict]:
        """Three playing MIDs plus one ruled out of the next gameweek."""
        base = {
            "position": "MID",
            "status": "a",
            "team_short": "ARS",
            "xGI_per_90": 0.3,
            "appearances": 6,
            "minutes": 540,
        }
        return [
            {**base, "id": 1, "player_name": "Elite", "form": 8.0, "ppg": 7.0},
            {**base, "id": 2, "player_name": "Good", "form": 6.0, "ppg": 5.5},
            {**base, "id": 3, "player_name": "Fringe", "form": 1.0, "ppg": 1.5},
            {
                **base, "id": 4, "player_name": "Injured", "form": 0.0, "ppg": 0.0,
                "status": "i", "chance_of_playing": 0, "minutes": 0, "appearances": 0,
            },
        ]

    @staticmethod
    def _priors() -> dict[int, PlayerPrior]:
        """Low confidence for the injured player — the shrinkage that caused #122."""
        return {
            1: PlayerPrior(0.9, 1.0, "history"),
            2: PlayerPrior(0.8, 1.0, "history"),
            3: PlayerPrior(0.6, 0.9, "history"),
            4: PlayerPrior(0.1, 0.3, "price"),
        }

    def _rank(self, next_gw_id: int) -> list[dict]:
        agent = WaiverAgent()
        agent._player_priors = self._priors()
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}
        return agent._rank_waiver_targets(
            self._pool(), [], squad_by_position, next_gw_id=next_gw_id,
        )

    def test_injured_player_ranks_below_weak_available_one(self):
        """Early season, where shrinkage is live, is where #122 bit."""
        ranked = self._rank(next_gw_id=3)
        order = [p["id"] for p in ranked]
        assert order.index(4) > order.index(3)

    def test_injured_player_score_is_untouched_by_shrinkage(self):
        """Held out means the ranked score is exactly the score that was computed."""
        agent = WaiverAgent()
        agent._player_priors = self._priors()
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}
        pool = self._pool()
        injured_in = next(p for p in pool if p["id"] == 4)

        raw = agent._calculate_waiver_score(injured_in, squad_by_position, next_gw_id=3)
        ranked = agent._rank_waiver_targets(pool, [], squad_by_position, next_gw_id=3)
        injured_out = next(p for p in ranked if p["id"] == 4)

        assert injured_out["waiver_score"] == raw

    def test_ordering_unchanged_after_the_shrinkage_cutoff(self):
        """From GW10 shrinkage is skipped entirely, so the hold-out is a no-op."""
        ranked = self._rank(next_gw_id=12)
        order = [p["id"] for p in ranked]
        assert order.index(4) > order.index(3)

    def test_available_players_still_shrink(self):
        """The fix removes the unavailable player, not shrinkage itself."""
        agent = WaiverAgent()
        agent._player_priors = self._priors()
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}
        pool = [p for p in self._pool() if p["id"] != 4]

        early = agent._rank_waiver_targets(pool, [], squad_by_position, next_gw_id=3)
        agent._player_priors = {pid: PlayerPrior(0.0, 1.0, "history") for pid in (1, 2, 3)}
        unshrunk = agent._rank_waiver_targets(pool, [], squad_by_position, next_gw_id=3)

        fringe_shrunk = next(p["waiver_score"] for p in early if p["id"] == 3)
        fringe_raw = next(p["waiver_score"] for p in unshrunk if p["id"] == 3)
        assert fringe_shrunk > fringe_raw


class TestWaiverAgentScoring:
    """Tests for waiver scoring methods."""

    def test_calculate_waiver_score_form_bonus(self):
        """Test high form adds to waiver score."""
        agent = WaiverAgent()
        player = {"form": 8.0, "ppg": 5.0, "xGI_per_90": 0.3, "minutes": 900, "appearances": 10, "position": "MID", "status": "a"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        score = agent._calculate_waiver_score(player, squad_by_position)

        assert 0 < score <= 100

    def test_calculate_waiver_score_ppg_bonus(self):
        """Test PPG adds to waiver score."""
        agent = WaiverAgent()
        player = {"form": 0, "ppg": 6.0, "xGI_per_90": 0, "minutes": 900, "appearances": 10, "position": "MID", "status": "a"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        score = agent._calculate_waiver_score(player, squad_by_position)

        # PPG 6.0 * 0.6 = 3.6, combined_mins_factor ~1.0 for nailed player
        assert score >= 3.0

    def test_calculate_waiver_score_position_need_bonus(self):
        """Test empty position adds bonus."""
        agent = WaiverAgent()
        player = {"form": 5.0, "ppg": 5.0, "xGI_per_90": 0.3, "minutes": 900, "appearances": 10, "position": "FWD", "status": "a"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [{"form": 5.0}], "FWD": []}  # FWD is empty

        score = agent._calculate_waiver_score(player, squad_by_position)

        # Should get +5 bonus for empty position
        assert score >= 15

    def test_calculate_waiver_score_availability_penalty(self):
        """Test unavailable players get penalty."""
        agent = WaiverAgent()
        player = {"form": 8.0, "ppg": 6.0, "xGI_per_90": 0.5, "minutes": 1800, "appearances": 20, "position": "MID", "status": "d", "chance_of_playing": 50}
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        score = agent._calculate_waiver_score(player, squad_by_position)

        assert score > 0

    def test_calculate_waiver_score_low_minutes_penalty(self):
        """Test low minutes gives lower score via combined_mins_factor."""
        agent = WaiverAgent()
        player = {"form": 8.0, "ppg": 6.0, "xGI_per_90": 0.5, "minutes": 100, "appearances": 2, "position": "MID", "status": "a"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        score = agent._calculate_waiver_score(player, squad_by_position)

        player_high_min = dict(player)
        player_high_min["minutes"] = 1000
        player_high_min["appearances"] = 12
        score_high_min = agent._calculate_waiver_score(player_high_min, squad_by_position)

        assert score < score_high_min

    def test_calculate_waiver_score_early_season_bypasses_mins_penalty(self):
        """Before GW5, combined_mins_factor is 1.0 regardless of minutes."""
        agent = WaiverAgent()
        player = {"form": 6.0, "ppg": 4.0, "xGI_per_90": 0.3, "minutes": 90, "appearances": 1, "position": "MID", "status": "a"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        score_early = agent._calculate_waiver_score(player, squad_by_position, next_gw_id=3)
        score_late = agent._calculate_waiver_score(player, squad_by_position, next_gw_id=25)

        # Early season should score higher (no mins penalty)
        assert score_early > score_late
        assert 0 < score_early <= 100

    def test_calculate_waiver_score_zero_appearances(self):
        """Player with 0 appearances: per-90 zeroed, form/ppg preserved."""
        agent = WaiverAgent()
        player = {"form": 6.0, "ppg": 4.0, "xGI_per_90": 0.3, "minutes": 0, "appearances": 0, "position": "MID", "status": "a"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        score = agent._calculate_waiver_score(player, squad_by_position, next_gw_id=25)

        # per90=0 (mins_factor=0); form=7.0 (6.0*1.3 capped at 7),
        # ppg=2.4 (4.0*0.6) + position need(5) = 14.4 raw
        # Normalised: 14.4 / MID_WAIVER_CEILING (26.82) * 100 ≈ 54
        assert score == 54  # Form/ppg + position need, but no per-90

        # With appearances, per-90 stats contribute → higher score
        player_with_apps = {**player, "minutes": 900, "appearances": 10}
        score_with_apps = agent._calculate_waiver_score(player_with_apps, squad_by_position, next_gw_id=25)
        assert score_with_apps > score


class TestWaiverAgentGkSignals:
    """#207: draft keepers are scored with the GK signal block.

    The calibrated waiver GK anchor budgets for saves/90, xGC quality and
    clean-sheet rate. The draft bootstrap publishes none of them, so the
    agent joins each draft element to its main-game ``Player`` on ``code``
    and reads them from there. Without that join a keeper is scored on form
    and ppg alone against an anchor that assumes all three, and the best
    keeper in the pool cannot reach the top of the scale at any point in
    the season.
    """

    def _draft_keeper(self, **overrides):
        """The keeper as ``_calculate_waiver_score`` receives him: no GK stats."""
        return {
            "id": 5, "player_name": "Raya", "position": "GK", "team_short": "ARS",
            "form": 6.0, "ppg": 5.0, "xGI_per_90": 0.0, "minutes": 1800,
            "appearances": 20, "status": "a", **overrides,
        }

    def _main_keeper(self, **overrides):
        fields = {
            "id": 500, "code": 9001, "web_name": "Raya", "team_id": 1,
            "position": PlayerPosition.GOALKEEPER,
            "form": 6.0, "points_per_game": 5.0, "minutes": 1800,
            "total_points": 100, "saves_per_90": 4.5,
            "expected_goals_conceded": 15.0, "clean_sheets": 10,
        }
        return make_player(**{**fields, **overrides})

    def test_joined_keeper_outscores_the_same_keeper_unjoined(self):
        """The signals reach the score, not just the enrichment dict."""
        agent = WaiverAgent()
        squad = {"GK": [{"form": 5.0}], "DEF": [], "MID": [], "FWD": []}

        unjoined = agent._calculate_waiver_score(
            self._draft_keeper(), squad, next_gw_id=26,
        )
        agent._main_player_map = {5: self._main_keeper()}
        joined = agent._calculate_waiver_score(
            self._draft_keeper(), squad, next_gw_id=26,
        )

        assert joined > unjoined

    def test_only_a_joined_keeper_claims_the_calendar_scaled_ceiling(self):
        """The flag is per player: a missed join must keep the full anchor.

        Scaling the denominator over a numerator with no signals inflates
        that keeper ~30% at GW2 (PR #156 review), so the outfielder and the
        keeper the join missed both stay on the unscaled ceiling.
        """
        from fpl_cli.services.scoring import calculate_waiver_score

        agent = WaiverAgent()
        agent._main_player_map = {5: self._main_keeper()}
        squad = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        with patch(
            "fpl_cli.agents.action.waiver.calculate_waiver_score",
            side_effect=calculate_waiver_score,
        ) as spy:
            agent._calculate_waiver_score(self._draft_keeper(), squad, next_gw_id=2)
            agent._calculate_waiver_score(
                self._draft_keeper(id=6, player_name="Backup"), squad, next_gw_id=2,
            )
            # Same id, so the map still resolves: position is what gates it.
            agent._calculate_waiver_score(
                self._draft_keeper(position="MID"), squad, next_gw_id=2,
            )

        assert [c.kwargs["gk_signals_supplied"] for c in spy.call_args_list] == [
            True, False, False,
        ]

    @pytest.mark.asyncio
    async def test_run_joins_available_keepers_on_code_not_name(
        self, mock_draft_bootstrap, mock_league_details,
    ):
        """The map is built in run() from the main players scoring data carries.

        The two names deliberately disagree: a mid-season rename must not
        drop the keeper the two APIs agree on in every other field (#168).
        """
        agent = WaiverAgent(config={"draft_league_id": 12345})
        draft_keeper = make_draft_player(
            id=5, web_name="Raya", team=1, element_type=1, code=9001,
            form=6.0, minutes=1800,
        )
        # Only `code` can match these two: diacritic folding would rescue a
        # near-miss spelling, so the names are made to disagree outright.
        main_keeper = self._main_keeper(web_name="Martinez")

        with _stub_upstream_fetches(players=[main_keeper]), \
             patch.object(agent.client, "get_bootstrap_static", new_callable=AsyncMock) as mock_bootstrap, \
             patch.object(agent.client, "get_available_players", new_callable=AsyncMock) as mock_available, \
             patch.object(agent.client, "get_recent_releases", new_callable=AsyncMock) as mock_releases, \
             patch.object(agent.client, "get_league_details", new_callable=AsyncMock) as mock_details, \
             patch.object(agent.client, "get_waiver_order", new_callable=AsyncMock) as mock_order:

            mock_bootstrap.return_value = mock_draft_bootstrap
            mock_available.return_value = [draft_keeper]
            mock_releases.return_value = []
            mock_details.return_value = mock_league_details
            mock_order.return_value = []

            result = await agent.run()

        assert result.status == AgentStatus.SUCCESS
        assert agent._main_player_map == {5: main_keeper}


class TestWaiverAgentReasons:
    """Tests for _generate_target_reasons method."""

    def test_generate_target_reasons_excellent_form(self):
        """Test excellent form generates reason."""
        agent = WaiverAgent()
        player = {"form": 7.0, "ppg": 4.0, "xGI_per_90": 0.2, "minutes": 500, "position": "MID"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        reasons = agent._generate_target_reasons(player, squad_by_position)

        assert any("Excellent form" in r for r in reasons)

    def test_generate_target_reasons_good_ppg(self):
        """Test good PPG generates reason."""
        agent = WaiverAgent()
        player = {"form": 3.0, "ppg": 5.5, "xGI_per_90": 0.2, "minutes": 500, "position": "MID"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        reasons = agent._generate_target_reasons(player, squad_by_position)

        assert any("Strong PPG" in r for r in reasons)

    def test_generate_target_reasons_high_xgi(self):
        """Test high xGI generates reason."""
        agent = WaiverAgent()
        player = {"form": 3.0, "ppg": 4.0, "xGI_per_90": 0.5, "minutes": 500, "position": "MID"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        reasons = agent._generate_target_reasons(player, squad_by_position)

        assert any("High xGI" in r for r in reasons)

    def test_generate_target_reasons_regular_starter(self):
        """Test high minutes generates starter reason."""
        agent = WaiverAgent()
        player = {"form": 3.0, "ppg": 4.0, "xGI_per_90": 0.2, "minutes": 1800, "position": "MID"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        reasons = agent._generate_target_reasons(player, squad_by_position)

        assert any("Regular starter" in r for r in reasons)

    def test_generate_target_reasons_better_than_current(self):
        """Test reason when better than current options."""
        agent = WaiverAgent()
        player = {"form": 6.0, "ppg": 4.0, "xGI_per_90": 0.2, "minutes": 500, "position": "MID"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [{"form": 3.0}], "FWD": []}

        reasons = agent._generate_target_reasons(player, squad_by_position)

        assert any("Better than current MID" in r for r in reasons)

    def test_generate_target_reasons_fallback(self):
        """Test fallback reason when no criteria met."""
        agent = WaiverAgent()
        player = {"form": 2.0, "ppg": 3.0, "xGI_per_90": 0.1, "minutes": 400, "position": "MID"}
        squad_by_position = {"GK": [], "DEF": [], "MID": [{"form": 5.0}], "FWD": []}

        reasons = agent._generate_target_reasons(player, squad_by_position)

        assert "Depth option" in reasons


class TestWaiverAgentHelpers:
    """Tests for helper methods."""

    def test_group_by_position(self):
        """Test _group_by_position groups players correctly."""
        agent = WaiverAgent()
        players = [
            {"position": "GK", "name": "GK1"},
            {"position": "DEF", "name": "DEF1"},
            {"position": "DEF", "name": "DEF2"},
            {"position": "MID", "name": "MID1"},
            {"position": "FWD", "name": "FWD1"},
        ]

        result = agent._group_by_position(players)

        assert len(result["GK"]) == 1
        assert len(result["DEF"]) == 2
        assert len(result["MID"]) == 1
        assert len(result["FWD"]) == 1


class TestWaiverAgentWeaknesses:
    """Tests for _identify_weaknesses method."""

    def test_identify_weaknesses_empty_position(self):
        """Test empty position is identified as weakness."""
        agent = WaiverAgent()
        squad_by_position = {"GK": [], "DEF": [{"form": 5.0}], "MID": [], "FWD": [{"form": 6.0}]}

        weaknesses = agent._identify_weaknesses(squad_by_position)

        positions_with_weakness = [w["position"] for w in weaknesses]
        assert "GK" in positions_with_weakness
        assert "MID" in positions_with_weakness

    def test_identify_weaknesses_low_form(self):
        """Test low average form is identified as weakness."""
        agent = WaiverAgent()
        squad_by_position = {
            "GK": [{"form": 5.0}],
            "DEF": [{"form": 2.0}, {"form": 2.5}],  # Avg 2.25 < 3
            "MID": [{"form": 5.0}],
            "FWD": [{"form": 6.0}],
        }

        weaknesses = agent._identify_weaknesses(squad_by_position)

        def_weakness = next((w for w in weaknesses if w["position"] == "DEF"), None)
        assert def_weakness is not None
        assert def_weakness["severity"] == "medium"
        assert "Low average form" in def_weakness["reason"]


class TestWaiverAgentRecommendations:
    """Tests for _generate_recommendations method."""

    def test_generate_recommendations_finds_drop_candidate(self):
        """Test recommendations include drop candidate."""
        agent = WaiverAgent()
        waiver_targets = [
            {"position": "MID", "player_name": "NewMID", "team_short": "ARS", "form": 7.0, "waiver_score": 20, "reasons": ["Good form"]},
        ]
        squad_by_position = {
            "GK": [],
            "DEF": [],
            "MID": [{"player_name": "OldMID", "form": 2.0}],
            "FWD": [],
        }

        recommendations = agent._generate_recommendations(waiver_targets, squad_by_position)

        assert len(recommendations) > 0
        rec = recommendations[0]
        assert rec["target"]["name"] == "NewMID"
        assert rec["drop"]["name"] == "OldMID"

    def test_generate_recommendations_limits_to_5(self):
        """Test recommendations are limited to 5."""
        agent = WaiverAgent()
        waiver_targets = [
            {"position": pos, "player_name": f"Player{i}", "team_short": "TST", "form": 7.0, "waiver_score": 20 - i, "reasons": []}
            for i, pos in enumerate(["GK", "DEF", "DEF", "MID", "MID", "MID", "FWD", "FWD"])
        ]
        squad_by_position = {
            "GK": [{"player_name": "OldGK", "form": 2.0}],
            "DEF": [{"player_name": "OldDEF", "form": 2.0}],
            "MID": [{"player_name": "OldMID", "form": 2.0}],
            "FWD": [{"player_name": "OldFWD", "form": 2.0}],
        }

        recommendations = agent._generate_recommendations(waiver_targets, squad_by_position)

        assert len(recommendations) <= 5

    def test_generate_recommendations_one_per_position(self):
        """Test only one recommendation per position."""
        agent = WaiverAgent()
        waiver_targets = [
            {"position": "MID", "player_name": "MID1", "team_short": "ARS", "form": 8.0, "waiver_score": 25, "reasons": []},
            {"position": "MID", "player_name": "MID2", "team_short": "LIV", "form": 7.0, "waiver_score": 20, "reasons": []},
            {"position": "FWD", "player_name": "FWD1", "team_short": "MCI", "form": 9.0, "waiver_score": 30, "reasons": []},
        ]
        squad_by_position = {
            "GK": [],
            "DEF": [],
            "MID": [{"player_name": "OldMID", "form": 2.0}],
            "FWD": [{"player_name": "OldFWD", "form": 2.0}],
        }

        recommendations = agent._generate_recommendations(waiver_targets, squad_by_position)

        positions = [r["target"]["position"] for r in recommendations]
        # Should only have one MID recommendation
        assert positions.count("MID") == 1

    def test_reliability_threaded_from_priors(self):
        """Target dict includes reliability from _player_priors when available."""
        from fpl_cli.services.player_prior import PlayerPrior

        agent = WaiverAgent()
        agent._player_priors = {
            99: PlayerPrior(prior_strength=0.5, confidence=0.6, source="history", reliability=0.85),
        }
        waiver_targets = [
            {"id": 99, "position": "MID", "player_name": "NewMID", "team_short": "ARS", "form": 7.0, "waiver_score": 20, "reasons": []},
        ]
        squad_by_position = {"GK": [], "DEF": [], "MID": [{"player_name": "OldMID", "form": 2.0}], "FWD": []}

        recommendations = agent._generate_recommendations(waiver_targets, squad_by_position)

        assert recommendations[0]["target"]["reliability"] == pytest.approx(0.85)

    def test_reliability_none_when_no_priors(self):
        """Target dict has reliability=None when _player_priors not set."""
        agent = WaiverAgent()
        waiver_targets = [
            {"id": 99, "position": "MID", "player_name": "NewMID", "team_short": "ARS", "form": 7.0, "waiver_score": 20, "reasons": []},
        ]
        squad_by_position = {"GK": [], "DEF": [], "MID": [{"player_name": "OldMID", "form": 2.0}], "FWD": []}

        recommendations = agent._generate_recommendations(waiver_targets, squad_by_position)

        assert recommendations[0]["target"]["reliability"] is None


class TestWaiverAgentTeamExposure:
    """Tests for team exposure awareness."""

    def test_get_team_exposure_counts_correctly(self):
        """Test _get_team_exposure counts players per team."""
        agent = WaiverAgent()
        squad_by_position = {
            "GK": [{"team_short": "ARS"}],
            "DEF": [{"team_short": "ARS"}, {"team_short": "LIV"}],
            "MID": [{"team_short": "ARS"}, {"team_short": "MCI"}],
            "FWD": [{"team_short": "LIV"}],
        }

        counts = agent._get_team_exposure(squad_by_position)

        assert counts["ARS"] == 3
        assert counts["LIV"] == 2
        assert counts["MCI"] == 1

    def test_check_team_exposure_no_warning(self):
        """Test no warning for low exposure."""
        agent = WaiverAgent()
        target = {"team_short": "TOT"}
        drop_candidate = {"team_short": "WHU"}
        team_counts = {"ARS": 2, "LIV": 1, "TOT": 0}

        new_count, warning = agent._check_team_exposure(target, drop_candidate, team_counts)

        assert new_count == 1
        assert warning is None

    def test_check_team_exposure_triple_up_warning(self):
        """Test warning for triple-up."""
        agent = WaiverAgent()
        target = {"team_short": "ARS"}
        drop_candidate = {"team_short": "WHU"}
        team_counts = {"ARS": 2, "LIV": 1}

        new_count, warning = agent._check_team_exposure(target, drop_candidate, team_counts)

        assert new_count == 3
        assert "Triple-up" in warning
        assert "ARS" in warning

    def test_check_team_exposure_heavy_exposure_warning(self):
        """Test warning for heavy exposure (4+ players)."""
        agent = WaiverAgent()
        target = {"team_short": "AVL"}
        drop_candidate = {"team_short": "WHU"}
        team_counts = {"AVL": 3}

        new_count, warning = agent._check_team_exposure(target, drop_candidate, team_counts)

        assert new_count == 4
        assert "Heavy exposure" in warning
        assert "4 AVL" in warning

    def test_check_team_exposure_same_team_swap(self):
        """Test no net change when swapping players from same team."""
        agent = WaiverAgent()
        target = {"team_short": "ARS"}
        drop_candidate = {"team_short": "ARS"}
        team_counts = {"ARS": 3}

        new_count, warning = agent._check_team_exposure(target, drop_candidate, team_counts)

        assert new_count == 3  # No change
        assert warning is None  # No warning for same-team swap

    def test_calculate_waiver_score_stacking_penalty(self):
        """Test stacking penalty reduces waiver score."""
        agent = WaiverAgent()
        player = {
            "form": 7.0, "ppg": 5.0, "xGI_per_90": 0.4,
            "minutes": 1000, "appearances": 12, "position": "MID",
            "status": "a", "team_short": "ARS"
        }
        squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        # Score with no existing ARS players
        team_counts_none = {"LIV": 2}
        score_no_stack = agent._calculate_waiver_score(player, squad_by_position, team_counts_none)

        # Score with 2 existing ARS players (would create triple-up)
        team_counts_double = {"ARS": 2}
        score_triple = agent._calculate_waiver_score(player, squad_by_position, team_counts_double)

        # Score with 3 existing ARS players (heavy stacking)
        team_counts_triple = {"ARS": 3}
        score_heavy = agent._calculate_waiver_score(player, squad_by_position, team_counts_triple)

        assert score_no_stack > score_triple  # -2 penalty for triple-up
        assert score_triple > score_heavy  # -5 penalty for 4+

    def test_generate_recommendations_includes_exposure_warning(self):
        """Test recommendations include exposure warnings."""
        agent = WaiverAgent()
        waiver_targets = [
            {
                "position": "DEF", "player_name": "Cash", "team_short": "AVL",
                "form": 6.0, "waiver_score": 18, "reasons": ["Good form"]
            },
        ]
        # Have 3 AVL players, dropping a non-AVL player, Cash would make 4 AVL
        squad_by_position = {
            "GK": [{"player_name": "Martinez", "team_short": "AVL", "form": 5.0}],
            "DEF": [
                {"player_name": "Konsa", "team_short": "AVL", "form": 4.0},
                {"player_name": "White", "team_short": "ARS", "form": 3.0},  # Non-AVL drop candidate
            ],
            "MID": [{"player_name": "Rogers", "team_short": "AVL", "form": 4.5}],
            "FWD": [],
        }

        recommendations = agent._generate_recommendations(waiver_targets, squad_by_position)

        assert len(recommendations) > 0
        rec = recommendations[0]
        assert "exposure" in rec
        assert rec["exposure"]["team"] == "AVL"
        assert rec["exposure"]["count_after"] == 4  # 3 existing + 1 new
        assert "Heavy exposure" in rec["exposure"]["warning"]

    def test_generate_recommendations_no_exposure_for_same_team_swap(self):
        """Test no exposure warning when dropping player from same team."""
        agent = WaiverAgent()
        waiver_targets = [
            {
                "position": "DEF", "player_name": "Cash", "team_short": "AVL",
                "form": 6.0, "waiver_score": 18, "reasons": ["Good form"]
            },
        ]
        # Have 2 AVL players in DEF - dropping one AVL for Cash is net 0
        squad_by_position = {
            "GK": [],
            "DEF": [
                {"player_name": "Konsa", "team_short": "AVL", "form": 4.0},
                {"player_name": "Digne", "team_short": "AVL", "form": 3.0},
            ],
            "MID": [],
            "FWD": [],
        }

        recommendations = agent._generate_recommendations(waiver_targets, squad_by_position)

        assert len(recommendations) > 0
        rec = recommendations[0]
        # Should not have exposure warning since dropping Digne (AVL) for Cash (AVL)
        assert "exposure" not in rec or rec.get("exposure") is None
