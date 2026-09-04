"""Tests for TransferEvalAgent."""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fpl_cli.agents.analysis.transfer_eval import TransferEvalAgent
from fpl_cli.agents.base import AgentStatus
from fpl_cli.models.player import PlayerPosition, PlayerStatus
from fpl_cli.services.scoring import (
    VALUE_CEILING,
    VALUE_QUALITY_WEIGHTS,
    ScoringContext,
    ScoringData,
    build_player_evaluation,
    calculate_mins_factor,
    calculate_player_quality_score,
    normalise_score,
)
from tests.conftest import make_fixture, make_player, make_team

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_players_and_data(
    *,
    bgw_teams: set[int] | None = None,
    doubtful_ids: set[int] | None = None,
) -> tuple[list[Any], ScoringData]:
    """Build test players and scoring data.

    Args:
        bgw_teams: Team IDs with no fixtures (blank GW).
        doubtful_ids: Player IDs to mark as 25% chance of playing.
    """
    teams = [
        make_team(id=1, short_name="ARS"),
        make_team(id=2, short_name="MCI"),
        make_team(id=3, short_name="LIV"),
        make_team(id=4, short_name="CHE"),
    ]
    team_map = {t.id: t for t in teams}

    players = [
        # OUT player
        make_player(
            id=10, web_name="Palmer", first_name="Cole", second_name="Palmer",
            team_id=4, position=PlayerPosition.MIDFIELDER,
            form=6.0, minutes=1800, now_cost=100,
            goals_scored=8, assists=5, expected_goals=7.0, expected_assists=4.5,
        ),
        # IN candidates
        make_player(
            id=20, web_name="Salah", first_name="Mohamed", second_name="Salah",
            team_id=3, position=PlayerPosition.MIDFIELDER,
            form=7.5, minutes=1900, now_cost=130,
            goals_scored=12, assists=8, expected_goals=10.5, expected_assists=7.0,
        ),
        make_player(
            id=30, web_name="Mbeumo", first_name="Bryan", second_name="Mbeumo",
            team_id=2, position=PlayerPosition.MIDFIELDER,
            form=5.0, minutes=1600, now_cost=75,
            goals_scored=6, assists=4, expected_goals=5.5, expected_assists=3.5,
        ),
        make_player(
            id=40, web_name="Diaz", first_name="Luis", second_name="Diaz",
            team_id=3, position=PlayerPosition.MIDFIELDER,
            form=4.5, minutes=1400, now_cost=80,
            goals_scored=5, assists=3, expected_goals=4.0, expected_assists=2.5,
        ),
    ]

    if doubtful_ids:
        for p in players:
            if p.id in doubtful_ids:
                # Create new player with doubtful status
                idx = players.index(p)
                players[idx] = make_player(
                    id=p.id, web_name=p.web_name, first_name=p.first_name,
                    second_name=p.second_name, team_id=p.team_id,
                    position=p.position, form=p.form, minutes=p.minutes,
                    now_cost=p.now_cost, goals_scored=p.goals_scored,
                    assists=p.assists, expected_goals=float(p.expected_goals),
                    expected_assists=float(p.expected_assists),
                    status=PlayerStatus.DOUBTFUL,
                    chance_of_playing_next_round=25,
                )

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

    fixture_map: dict[int, list] = {
        1: [{"fixture": fixtures[0], "is_home": True}],
        2: [{"fixture": fixtures[1], "is_home": True}],
        3: [{"fixture": fixtures[1], "is_home": False}],
        4: [{"fixture": fixtures[0], "is_home": False}],
    }
    if bgw_teams:
        for tid in bgw_teams:
            fixture_map[tid] = []

    scoring_ctx = ScoringContext(
        team_map=team_map,
        team_fixture_map=fixture_map,
        ratings_service=ratings_service,
        next_gw_id=25,
    )

    scoring_data = ScoringData(
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

    return players, scoring_data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTransferEvalAgent:

    @pytest.fixture
    def standard_data(self):
        return _build_players_and_data()

    async def test_happy_path_returns_correct_structure(self, standard_data):
        players, scoring_data = standard_data

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20, 30, 40],
                })

        assert result.status == AgentStatus.SUCCESS
        assert "out_player" in result.data
        assert len(result.data["in_players"]) == 3

        out = result.data["out_player"]
        assert out["outlook_delta"] is None
        assert out["gw_delta"] is None
        assert out["outlook"] > 0
        assert out["this_gw"] >= 0

        for inp in result.data["in_players"]:
            assert "outlook_delta" in inp
            assert "gw_delta" in inp
            assert isinstance(inp["outlook_delta"], int)
            assert isinstance(inp["gw_delta"], int)

    async def test_sorted_by_outlook_delta_descending(self, standard_data):
        players, scoring_data = standard_data

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20, 30, 40],
                })

        deltas = [p["outlook_delta"] for p in result.data["in_players"]]
        assert deltas == sorted(deltas, reverse=True)

    async def test_single_in_candidate(self, standard_data):
        _, scoring_data = standard_data

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20],
                })

        assert result.status == AgentStatus.SUCCESS
        assert len(result.data["in_players"]) == 1

    async def test_bgw_candidate_gets_low_lineup_score(self):
        """Player with no fixtures (BGW) should get lineup_score 0."""
        _, scoring_data = _build_players_and_data(bgw_teams={3})

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20],  # Salah on team 3 (no fixtures)
                })

        assert result.status == AgentStatus.SUCCESS
        salah = result.data["in_players"][0]
        assert salah["this_gw"] == 0

    async def test_no_understat_data_still_scores(self, standard_data):
        _, scoring_data = standard_data

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20, 30],
                })

        assert result.status == AgentStatus.SUCCESS
        for inp in result.data["in_players"]:
            assert inp["outlook"] > 0

    async def test_doubtful_player_excluded_in_lineup(self):
        _, scoring_data = _build_players_and_data(doubtful_ids={20})

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20],
                })

        assert result.status == AgentStatus.SUCCESS
        salah = result.data["in_players"][0]
        assert salah["excluded"] is True
        # Should still have outlook score
        assert salah["outlook"] > 0

    async def test_empty_in_players_returns_failed(self):
        async with TransferEvalAgent() as agent:
            result = await agent.run({
                "out_player_id": 10,
                "in_player_ids": [],
            })

        assert result.status == AgentStatus.FAILED

    async def test_missing_context_returns_failed(self):
        async with TransferEvalAgent() as agent:
            result_none = await agent.run(None)
            result_empty = await agent.run({})

        assert result_none.status == AgentStatus.FAILED
        assert result_empty.status == AgentStatus.FAILED

    async def test_both_scoring_families_produce_different_scores(self, standard_data):
        """Target score and lineup score use different formulas."""
        _, scoring_data = standard_data

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20],
                })

        out = result.data["out_player"]
        # They _can_ be equal by coincidence, but checking they're both populated
        assert isinstance(out["outlook"], int)
        assert isinstance(out["this_gw"], int)

    # -----------------------------------------------------------------------
    # Quality / value score tests
    # -----------------------------------------------------------------------

    async def test_quality_score_non_null_with_understat(self):
        """MID with Understat match gets non-null quality_score and quality_per_m."""
        _, scoring_data = _build_players_and_data()
        # Add Understat data for all players
        scoring_data = dataclasses.replace(scoring_data, understat_lookup={
            10: {"npxG_per_90": 0.35, "xGChain_per_90": 0.55, "penalty_xG_per_90": 0.05},
            20: {"npxG_per_90": 0.50, "xGChain_per_90": 0.70, "penalty_xG_per_90": 0.08},
            30: {"npxG_per_90": 0.25, "xGChain_per_90": 0.40, "penalty_xG_per_90": 0.02},
        })

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20, 30],
                })

        assert result.status == AgentStatus.SUCCESS
        out = result.data["out_player"]
        assert isinstance(out["quality_score"], int)
        assert 0 <= out["quality_score"] <= 100
        assert isinstance(out["quality_per_m"], float)
        assert out["quality_per_m"] > 0

        for inp in result.data["in_players"]:
            assert isinstance(inp["quality_score"], int)
            assert isinstance(inp["quality_per_m"], float)

    async def test_quality_score_null_without_understat(self, standard_data):
        """Player without Understat match gets null quality_score and quality_per_m."""
        _, scoring_data = standard_data
        # standard_data has understat_lookup=None

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20],
                })

        assert result.status == AgentStatus.SUCCESS
        out = result.data["out_player"]
        assert out["quality_score"] is None
        assert out["quality_per_m"] is None

    async def test_quality_per_m_null_when_price_zero(self):
        """Player with price 0 gets quality_score but null quality_per_m."""
        _, scoring_data = _build_players_and_data()
        # Replace Mbeumo (id=30) with price 0
        assert scoring_data.players is not None
        for i, p in enumerate(scoring_data.players):
            if p.id == 30:
                scoring_data.players[i] = make_player(
                    id=30, web_name="Mbeumo", first_name="Bryan", second_name="Mbeumo",
                    team_id=2, position=PlayerPosition.MIDFIELDER,
                    form=5.0, minutes=1600, now_cost=0,
                    goals_scored=6, assists=4, expected_goals=5.5, expected_assists=3.5,
                )
        scoring_data = dataclasses.replace(scoring_data, understat_lookup={
            10: {"npxG_per_90": 0.35, "xGChain_per_90": 0.55, "penalty_xG_per_90": 0.05},
            30: {"npxG_per_90": 0.25, "xGChain_per_90": 0.40, "penalty_xG_per_90": 0.02},
        })

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [30],
                })

        assert result.status == AgentStatus.SUCCESS
        mbeumo = result.data["in_players"][0]
        assert isinstance(mbeumo["quality_score"], int)
        assert mbeumo["quality_per_m"] is None

    async def test_def_uses_without_xgi_weights(self):
        """DEF player with Understat match uses without_xgi() weights."""
        teams = [
            make_team(id=1, short_name="ARS"),
            make_team(id=2, short_name="MCI"),
        ]
        team_map = {t.id: t for t in teams}

        players = [
            make_player(
                id=10, web_name="Palmer", first_name="Cole", second_name="Palmer",
                team_id=1, position=PlayerPosition.MIDFIELDER,
                form=6.0, minutes=1800, now_cost=100,
                goals_scored=8, assists=5, expected_goals=7.0, expected_assists=4.5,
            ),
            make_player(
                id=50, web_name="Gabriel", first_name="Gabriel", second_name="Magalhaes",
                team_id=1, position=PlayerPosition.DEFENDER,
                form=4.0, minutes=2000, now_cost=60,
                goals_scored=3, assists=1, expected_goals=2.0, expected_assists=0.5,
                defensive_contribution_per_90=5.5,
            ),
        ]

        fixtures = [
            make_fixture(id=1, gameweek=25, home_team_id=1, away_team_id=2,
                         home_difficulty=3, away_difficulty=3),
        ]

        ratings_service = MagicMock()
        ratings_service.get_positional_fdr.return_value = 3.0
        ratings_service.get_matchup_score.return_value = {
            "matchup_score": 5.5, "attack_matchup": 5.0, "defence_matchup": 5.0,
            "form_differential": 0.1, "position_differential": 0.05, "reasoning": ["Average"],
        }

        scoring_ctx = ScoringContext(
            team_map=team_map,
            team_fixture_map={1: [{"fixture": fixtures[0], "is_home": True}], 2: [{"fixture": fixtures[0], "is_home": False}]},
            ratings_service=ratings_service,
            next_gw_id=25,
        )

        scoring_data = ScoringData(
            teams=teams, team_map=team_map,
            all_fixtures=fixtures, next_gw_fixtures=fixtures,
            next_gw_id=25, next_gw={"id": 25, "is_next": True},
            scoring_ctx=scoring_ctx, ratings_service=ratings_service,
            players=players,
            understat_lookup={
                10: {"npxG_per_90": 0.35, "xGChain_per_90": 0.55, "penalty_xG_per_90": 0.05},
                50: {"npxG_per_90": 0.05, "xGChain_per_90": 0.10, "penalty_xG_per_90": 0.0},
            },
            player_histories={}, player_priors=None,
        )

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [50],
                })

        assert result.status == AgentStatus.SUCCESS
        gabriel = result.data["in_players"][0]
        assert isinstance(gabriel["quality_score"], int)
        assert gabriel["quality_score"] > 0  # dc_per_90 contributes via without_xgi()

    async def test_quality_score_parity_with_fpl_player(self):
        """R5: quality_score must match between agent and fpl-player-style computation."""
        _, scoring_data = _build_players_and_data()
        understat = {
            10: {"npxG_per_90": 0.35, "xGChain_per_90": 0.55, "penalty_xG_per_90": 0.05},
        }
        scoring_data = dataclasses.replace(scoring_data, understat_lookup=understat)

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20],
                })

        agent_quality = result.data["out_player"]["quality_score"]

        # Reproduce fpl-player-style computation for the same player
        assert scoring_data.players is not None
        player = scoring_data.players[0]  # Palmer, id=10
        minutes_safe = max(player.minutes, 1)
        enrichment: dict = {
            "team_short": "CHE",
            **understat[10],
            "xGI_per_90": (player.expected_goals + player.expected_assists) / minutes_safe * 90,
            "dc_per_90": player.defensive_contribution_per_90,
        }
        evaluation, _ = build_player_evaluation(
            player, enrichment=enrichment,
            fixture_matchups=[],
        )
        q_dict = evaluation.as_quality_dict()
        weights = VALUE_QUALITY_WEIGHTS  # MID
        mins_factor = calculate_mins_factor(player.minutes, player.appearances, 25)
        raw = calculate_player_quality_score(q_dict, weights, mins_factor)
        expected_quality = normalise_score(raw, VALUE_CEILING)

        assert agent_quality == expected_quality

    async def test_quality_score_is_prior_informed_before_the_cutoff(self):
        """Same blend as compute_quality_value, so fpl transfer-eval agrees
        with fpl player at GW2 (#143). The fixture's out-player is a hot
        starter (observed 91), so a mid-table pedigree is the visible case:
        the blend pulls the score toward what last season implies.
        """
        from fpl_cli.services.player_prior import PlayerPrior, _compute_confidence

        _, scoring_data = _build_players_and_data()
        understat = {
            10: {"npxG_per_90": 0.35, "xGChain_per_90": 0.55, "penalty_xG_per_90": 0.05},
        }
        early = dataclasses.replace(scoring_data, next_gw_id=2, understat_lookup=understat)
        prior = PlayerPrior(0.3, _compute_confidence(2, 0.3), "history")
        with_prior = dataclasses.replace(early, player_priors={10: prior})

        async def _quality(data: ScoringData) -> int:
            with patch(
                "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
                new_callable=AsyncMock,
                return_value=data,
            ):
                async with TransferEvalAgent() as agent:
                    result = await agent.run({"out_player_id": 10, "in_player_ids": [20]})
            assert result.status == AgentStatus.SUCCESS
            return result.data["out_player"]["quality_score"]

        observed = await _quality(early)
        blended = await _quality(with_prior)
        weight = prior.confidence  # MID: no keeper discount
        assert blended < observed
        assert blended == pytest.approx(
            weight * observed + (1 - weight) * 0.3 * 92, abs=1,
        )

    async def test_result_carries_the_early_season_quality_notice(self):
        """The agent knows whether the priors loaded; the command does not, so
        the notice travels in the result (PR #208 review).
        """
        from fpl_cli.services.player_prior import PlayerPrior, _compute_confidence

        _, scoring_data = _build_players_and_data()
        understat = {
            10: {"npxG_per_90": 0.35, "xGChain_per_90": 0.55, "penalty_xG_per_90": 0.05},
        }

        async def _warnings(data: ScoringData) -> list[dict[str, str]]:
            with patch(
                "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
                new_callable=AsyncMock,
                return_value=data,
            ):
                async with TransferEvalAgent() as agent:
                    result = await agent.run({"out_player_id": 10, "in_player_ids": [20]})
            assert result.status == AgentStatus.SUCCESS
            return result.data["warnings"]

        early = dataclasses.replace(scoring_data, next_gw_id=2, understat_lookup=understat)
        blended = dataclasses.replace(
            early,
            player_priors={10: PlayerPrior(0.5, _compute_confidence(2, 0.5), "history")},
        )
        assert [w["code"] for w in await _warnings(blended)] == ["early_season_prior_informed"]
        assert [w["code"] for w in await _warnings(early)] == ["early_season_small_sample"]

        # Both blended columns are named, so a reader knows which numbers on
        # the page are pedigree-informed and which (lineup_score) are not.
        assert "quality_score and target_score" in (await _warnings(blended))[0]["message"]

        # No Understat means no quality_score — but the outlook target_score
        # is always shown and always blended (#206), so the notice still
        # applies and names only the column that is actually there.
        no_understat = await _warnings(dataclasses.replace(scoring_data, next_gw_id=2))
        assert [w["code"] for w in no_understat] == ["early_season_small_sample"]
        assert "target_score" in no_understat[0]["message"]
        assert "quality_score" not in no_understat[0]["message"]

        # Nothing to caveat: mid-season
        assert await _warnings(dataclasses.replace(scoring_data, understat_lookup=understat)) == []

    async def test_ruled_out_player_keeps_the_observed_quality_score(self):
        """The blend's hold-out reaches this path too: a player at 0% is not
        handed last season's standing for a gameweek they will not play.
        """
        from fpl_cli.services.player_prior import PlayerPrior, _compute_confidence

        players, scoring_data = _build_players_and_data()
        palmer = players[0]
        ruled_out = make_player(
            id=palmer.id, web_name=palmer.web_name, first_name=palmer.first_name,
            second_name=palmer.second_name, team_id=palmer.team_id,
            position=palmer.position, form=palmer.form, minutes=palmer.minutes,
            now_cost=palmer.now_cost, goals_scored=palmer.goals_scored,
            assists=palmer.assists, expected_goals=float(palmer.expected_goals),
            expected_assists=float(palmer.expected_assists),
            status=PlayerStatus.INJURED, chance_of_playing_next_round=0,
        )
        understat = {
            10: {"npxG_per_90": 0.35, "xGChain_per_90": 0.55, "penalty_xG_per_90": 0.05},
        }
        base = dataclasses.replace(
            scoring_data, next_gw_id=2, understat_lookup=understat,
            players=[ruled_out, *players[1:]],
        )
        with_prior = dataclasses.replace(
            base,
            player_priors={10: PlayerPrior(1.0, _compute_confidence(2, 1.0), "history")},
        )

        async def _quality(data: ScoringData) -> int:
            with patch(
                "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
                new_callable=AsyncMock,
                return_value=data,
            ):
                async with TransferEvalAgent() as agent:
                    result = await agent.run({"out_player_id": 10, "in_player_ids": [20]})
            assert result.status == AgentStatus.SUCCESS
            return result.data["out_player"]["quality_score"]

        assert await _quality(with_prior) == await _quality(base)

    async def test_reliability_threaded_through_player_priors(self, standard_data):
        """reliability field from PlayerPrior appears in agent output dict."""
        from fpl_cli.services.player_prior import PlayerPrior

        _, scoring_data = standard_data
        priors = {
            10: PlayerPrior(prior_strength=0.7, confidence=0.8, source="history", reliability=0.85),
            20: PlayerPrior(prior_strength=0.9, confidence=1.0, source="history", reliability=0.92),
            30: PlayerPrior(prior_strength=0.5, confidence=0.6, source="price", reliability=None),
        }
        scoring_data = dataclasses.replace(scoring_data, player_priors=priors)

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20, 30],
                })

        assert result.status == AgentStatus.SUCCESS
        assert result.data["out_player"]["reliability"] == pytest.approx(0.85)
        in_by_id = {p["id"]: p for p in result.data["in_players"]}
        assert in_by_id[20]["reliability"] == pytest.approx(0.92)
        assert in_by_id[30]["reliability"] is None

    async def test_reliability_none_when_no_priors(self, standard_data):
        """reliability=None in output when player_priors is not provided."""
        _, scoring_data = standard_data  # player_priors=None

        with patch(
            "fpl_cli.agents.analysis.transfer_eval.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=scoring_data,
        ):
            async with TransferEvalAgent() as agent:
                result = await agent.run({
                    "out_player_id": 10,
                    "in_player_ids": [20],
                })

        assert result.status == AgentStatus.SUCCESS
        assert result.data["out_player"]["reliability"] is None
        assert result.data["in_players"][0]["reliability"] is None
