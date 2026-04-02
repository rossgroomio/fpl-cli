"""Tests for StartingXIAgent."""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fpl_cli.agents.analysis.starting_xi import StartingXIAgent
from fpl_cli.agents.base import AgentStatus
from fpl_cli.models.player import Player, PlayerPosition
from fpl_cli.services.player_scoring import VALID_FORMATIONS
from fpl_cli.models.team import Team
from fpl_cli.services.player_scoring import ScoringContext, ScoringData
from tests.conftest import make_fixture, make_player, make_team


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_squad_players() -> tuple[list[Player], list[Team]]:
    """Build a 15-player squad: 2 GK, 5 DEF, 5 MID, 3 FWD."""
    teams = [
        make_team(id=1, short_name="ARS"),
        make_team(id=2, short_name="MCI"),
        make_team(id=3, short_name="LIV"),
        make_team(id=4, short_name="CHE"),
    ]
    players = [
        # GK x2
        make_player(id=1, web_name="GK1", team_id=1, position=PlayerPosition.GOALKEEPER, form=5.0, minutes=1800),
        make_player(id=2, web_name="GK2", team_id=2, position=PlayerPosition.GOALKEEPER, form=3.0, minutes=900),
        # DEF x5
        make_player(id=3, web_name="DEF1", team_id=1, position=PlayerPosition.DEFENDER, form=6.0, minutes=1700),
        make_player(id=4, web_name="DEF2", team_id=1, position=PlayerPosition.DEFENDER, form=5.5, minutes=1600),
        make_player(id=5, web_name="DEF3", team_id=2, position=PlayerPosition.DEFENDER, form=5.0, minutes=1500),
        make_player(id=6, web_name="DEF4", team_id=3, position=PlayerPosition.DEFENDER, form=4.5, minutes=1400),
        make_player(id=7, web_name="DEF5", team_id=4, position=PlayerPosition.DEFENDER, form=4.0, minutes=1300),
        # MID x5
        make_player(id=8, web_name="MID1", team_id=1, position=PlayerPosition.MIDFIELDER, form=7.0, minutes=1800),
        make_player(id=9, web_name="MID2", team_id=2, position=PlayerPosition.MIDFIELDER, form=6.5, minutes=1700),
        make_player(id=10, web_name="MID3", team_id=3, position=PlayerPosition.MIDFIELDER, form=6.0, minutes=1600),
        make_player(id=11, web_name="MID4", team_id=4, position=PlayerPosition.MIDFIELDER, form=5.5, minutes=1500),
        make_player(id=12, web_name="MID5", team_id=1, position=PlayerPosition.MIDFIELDER, form=5.0, minutes=1400),
        # FWD x3
        make_player(id=13, web_name="FWD1", team_id=2, position=PlayerPosition.FORWARD, form=8.0, minutes=1800),
        make_player(id=14, web_name="FWD2", team_id=3, position=PlayerPosition.FORWARD, form=7.0, minutes=1700),
        make_player(id=15, web_name="FWD3", team_id=4, position=PlayerPosition.FORWARD, form=6.0, minutes=1600),
    ]
    return players, teams


def _mock_scoring_data(players: list[Any], teams: list[Any]) -> ScoringData:
    """Build a ScoringData with the given players and teams."""
    team_map = {t.id: t for t in teams}
    fixtures = [
        make_fixture(id=1, gameweek=25, home_team_id=1, away_team_id=4, home_difficulty=2, away_difficulty=4),
        make_fixture(id=2, gameweek=25, home_team_id=2, away_team_id=3, home_difficulty=3, away_difficulty=3),
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
            2: [{"fixture": fixtures[1], "is_home": True}],
            3: [{"fixture": fixtures[1], "is_home": False}],
            4: [{"fixture": fixtures[0], "is_home": False}],
        },
        ratings_service=ratings_service,
        next_gw_id=25,
    )

    return ScoringData(
        teams=teams,
        team_map=team_map,
        all_fixtures=fixtures,
        next_gw_fixtures=fixtures,
        next_gw_id=25,
        next_gw={"id": 25, "is_next": True},
        scoring_ctx=scoring_ctx,
        ratings_service=ratings_service,
        players=players,
        understat_lookup=None,
        player_histories={},
        player_priors=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStartingXIAgent:
    """Tests for StartingXIAgent."""

    @pytest.fixture
    def squad_data(self):
        players, teams = _build_squad_players()
        return players, teams, _mock_scoring_data(players, teams)

    async def test_happy_path_returns_success_with_11_starters_4_bench(self, squad_data):
        players, _, scoring_data = squad_data
        squad_ids = [p.id for p in players]

        with patch(
            "fpl_cli.agents.analysis.starting_xi.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with StartingXIAgent() as agent:
                result = await agent.run({"squad": squad_ids})

        assert result.status == AgentStatus.SUCCESS
        assert len(result.data["starting_xi"]) == 11
        assert len(result.data["bench"]) == 4
        formation = tuple(int(x) for x in result.data["formation"].split("-"))
        assert formation in VALID_FORMATIONS
        assert result.data["total_score"] > 0

    async def test_player_without_understat_still_scores(self, squad_data):
        """Players missing Understat enrichment should still produce a score."""
        players, _, scoring_data = squad_data
        # Explicitly set understat_lookup to empty (no player matched)
        scoring_data = ScoringData(
            teams=scoring_data.teams,
            team_map=scoring_data.team_map,
            all_fixtures=scoring_data.all_fixtures,
            next_gw_fixtures=scoring_data.next_gw_fixtures,
            next_gw_id=scoring_data.next_gw_id,
            next_gw=scoring_data.next_gw,
            scoring_ctx=scoring_data.scoring_ctx,
            ratings_service=scoring_data.ratings_service,
            players=scoring_data.players,
            understat_lookup={},  # No Understat data for any player
            player_histories=scoring_data.player_histories,
            player_priors=scoring_data.player_priors,
        )
        squad_ids = [p.id for p in players]

        with patch(
            "fpl_cli.agents.analysis.starting_xi.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with StartingXIAgent() as agent:
                result = await agent.run({"squad": squad_ids})

        assert result.status == AgentStatus.SUCCESS
        assert len(result.data["starting_xi"]) == 11

    async def test_bgw_squad_with_missing_fixtures(self, squad_data):
        """Players on teams without next-GW fixtures (BGW) should still be scored."""
        players, _, scoring_data = squad_data

        # Build a new ScoringContext with empty fixture maps for teams 3 and 4
        bgw_fixture_map = dict(scoring_data.scoring_ctx.team_fixture_map)
        bgw_fixture_map[3] = []
        bgw_fixture_map[4] = []
        bgw_ctx = dataclasses.replace(scoring_data.scoring_ctx, team_fixture_map=bgw_fixture_map)
        bgw_data = ScoringData(
            teams=scoring_data.teams,
            team_map=scoring_data.team_map,
            all_fixtures=scoring_data.all_fixtures,
            next_gw_fixtures=scoring_data.next_gw_fixtures,
            next_gw_id=scoring_data.next_gw_id,
            next_gw=scoring_data.next_gw,
            scoring_ctx=bgw_ctx,
            ratings_service=scoring_data.ratings_service,
            players=scoring_data.players,
            understat_lookup=scoring_data.understat_lookup,
            player_histories=scoring_data.player_histories,
            player_priors=scoring_data.player_priors,
        )

        squad_ids = [p.id for p in players]

        with patch(
            "fpl_cli.agents.analysis.starting_xi.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=bgw_data,
        ):
            async with StartingXIAgent() as agent:
                result = await agent.run({"squad": squad_ids})

        assert result.status == AgentStatus.SUCCESS
        assert len(result.data["starting_xi"]) == 11
        assert len(result.data["bench"]) == 4

    async def test_no_squad_returns_failed(self):
        """Agent should return FAILED when no squad is provided."""
        async with StartingXIAgent() as agent:
            result_none = await agent.run(None)
            result_empty = await agent.run({})

        assert result_none.status == AgentStatus.FAILED
        assert "squad" in result_none.errors[0].lower()
        assert result_empty.status == AgentStatus.FAILED
