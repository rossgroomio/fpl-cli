"""Tests for StatsAgent Understat enrichment."""


import pytest

from fpl_cli.agents.analysis.stats import StatsAgent
from fpl_cli.models.player import PlayerPosition, PlayerStatus
from tests.conftest import make_player, make_team


@pytest.fixture
def mock_understat_match():
    """Mock Understat match result for a single player."""
    return {
        "id": 12345,
        "name": "Mohamed Salah",
        "team": "Liverpool",
        "position": "M F",
        "minutes": 1800,
        "npxG": 10.2,
        "npxG_per_90": 0.51,
        "xGChain": 18.5,
        "xGChain_per_90": 0.93,
        "xGBuildup": 5.2,
        "xGBuildup_per_90": 0.26,
        "penalty_xG": 2.3,
        "penalty_xG_per_90": 0.12,
    }


class TestStatsAgentUnderstatEnrichment:
    """Tests for Understat data merging in StatsAgent."""

    def test_merge_understat_data_adds_fields(self, mock_understat_match):
        """Test that Understat metrics are merged into player stats."""
        agent = StatsAgent(config={"gameweeks": 0})

        player_stats = {
            "id": 1,
            "name": "Salah",
            "team": "LIV",
            "position": "MID",
            "minutes": 1800,
        }

        enriched = agent._merge_understat_data(player_stats, mock_understat_match)

        assert enriched["npxG_per_90"] == 0.51
        assert enriched["xGChain_per_90"] == 0.93
        assert enriched["xGBuildup_per_90"] == 0.26
        assert enriched["penalty_xG"] == 2.3
        assert enriched["penalty_xG_per_90"] == 0.12

    def test_merge_understat_data_missing_returns_nones(self):
        """Test graceful fallback when no Understat match."""
        agent = StatsAgent(config={"gameweeks": 0})

        player_stats = {
            "id": 1,
            "name": "Unknown",
            "team": "???",
            "position": "MID",
            "minutes": 900,
        }

        enriched = agent._merge_understat_data(player_stats, None)

        assert enriched["npxG_per_90"] is None
        assert enriched["xGChain_per_90"] is None
        assert enriched["xGBuildup_per_90"] is None
        assert enriched["penalty_xG"] is None
        assert enriched["penalty_xG_per_90"] is None


class TestStatsAgentNpxGScoring:
    """Tests for npxG-aware scoring in StatsAgent."""

    def test_differential_score_differs_with_npxg(self):
        """Score should differ when npxG is available vs when it's None."""
        agent = StatsAgent(config={"gameweeks": 0})

        base = {
            "position": "MID",
            "xGI_per_90": 0.8,
            "form": 5,
            "points_per_game": 5,
            "ownership": 5,
            "GI_minus_xGI": 0,
            "positional_fdr": 3.0,
            "matchup_score": 5.0,
            "minutes": 2400,
            "appearances": 28,
        }

        player_with_npxg = {
            **base,
            "npxG_per_90": 0.2,  # Lower than xGI because penalties stripped
            "xGChain_per_90": 0.3,
        }

        player_without_npxg = {
            **base,
            "npxG_per_90": None,
            "xGChain_per_90": None,
        }

        score_with = agent._calculate_differential_score(player_with_npxg)
        score_without = agent._calculate_differential_score(player_without_npxg)

        # Scores must differ - proves npxG path is active
        assert score_with != score_without
        # Both must be positive
        assert score_with > 0
        assert score_without > 0


class TestStatsAgentAvailabilityIsNotScored:
    """Target and differential scores must not filter on availability.

    These two lists are discovery surfaces: they answer "who is worth buying
    over the next few gameweeks", a 3-6 GW question, while
    ``chance_of_playing`` is a next-round flag. Users who want the list
    narrowed to players they can field this week have an explicit lever in
    ``fpl stats --available-only`` (see ``tests/test_cli_stats.py``); a silent
    penalty inside the score would duplicate that lever, remove the choice,
    and move a player down the table with nothing in the row explaining why.

    The ownership family's shared flow does carry a -3 availability penalty,
    but it is gated on ``status != "a"`` and ``PlayerStats`` carries no
    ``status`` field, so it cannot fire here. That is load-bearing rather than
    incidental: plumbing ``status`` through to ``PlayerStats`` — for display,
    say — would silently switch the penalty on for both commands. These tests
    fail if that happens, so the change has to be a deliberate one.
    """

    @staticmethod
    def _stats_for(agent: StatsAgent, **player_kwargs) -> dict:
        """Build a PlayerStats record through the real builder, not by hand."""
        player = make_player(
            position=PlayerPosition.MIDFIELDER,
            form=7.0, points_per_game=6.5, minutes=600,
            goals_scored=4, assists=3,
            expected_goals=3.5, expected_assists=2.0,
            **player_kwargs,
        )
        return agent._calculate_player_stats(player, {1: make_team()})

    def test_target_score_ignores_a_zero_chance_of_playing(self):
        agent = StatsAgent(config={"gameweeks": 0})
        fit = self._stats_for(agent)
        ruled_out = self._stats_for(
            agent, status=PlayerStatus.INJURED, chance_of_playing_next_round=0,
        )
        assert agent._calculate_target_score(ruled_out) == agent._calculate_target_score(fit)

    def test_differential_score_ignores_a_zero_chance_of_playing(self):
        agent = StatsAgent(config={"gameweeks": 0})
        fit = self._stats_for(agent)
        ruled_out = self._stats_for(
            agent, status=PlayerStatus.INJURED, chance_of_playing_next_round=0,
        )
        assert agent._calculate_differential_score(ruled_out) == agent._calculate_differential_score(fit)

    def test_scores_ignore_a_doubt_percentage_too(self):
        """Not just the 0% case — no availability bucket moves these scores."""
        agent = StatsAgent(config={"gameweeks": 0})
        fit = self._stats_for(agent)
        for cop in (25, 50, 75):
            doubtful = self._stats_for(
                agent, status=PlayerStatus.DOUBTFUL, chance_of_playing_next_round=cop,
            )
            assert agent._calculate_target_score(doubtful) == agent._calculate_target_score(fit)


class TestStatsAgentReliability:
    """Tests for reliability propagation in _find_targets and _compute_differentials."""

    def _make_player(self, player_id: int, ownership: float, position: str = "MID") -> dict:
        return {
            "id": player_id,
            "player_name": f"Player{player_id}",
            "team_short": "TST",
            "position": position,
            "price": 80,
            "ownership": ownership,
            "minutes": 2000,
            "goals": 5,
            "assists": 3,
            "GI": 8,
            "xG": 4.0,
            "xA": 3.0,
            "xGI": 7.0,
            "xG_per_90": 0.2,
            "xA_per_90": 0.15,
            "xGI_per_90": 0.35,
            "goals_minus_xG": 1.0,
            "assists_minus_xA": 0.0,
            "GI_minus_xGI": 1.0,
            "form": 5.0,
            "total_points": 80,
            "ppg": 5.0,
            "dc_per_90": 0.0,
            "npxG_per_90": None,
            "xGChain_per_90": None,
            "xGBuildup_per_90": None,
            "penalty_xG": None,
            "penalty_xG_per_90": None,
            "matchup_score": 5.0,
            "next_opponent": "CHE",
        }

    def test_find_targets_includes_reliability_from_priors(self):
        from fpl_cli.services.player_prior import PlayerPrior

        agent = StatsAgent(config={"gameweeks": 0})
        agent._player_priors = {
            1: PlayerPrior(prior_strength=0.5, confidence=0.6, source="history", reliability=0.85),
        }
        agent._next_gw_id = 30

        players = [self._make_player(1, ownership=10.0)]
        result = agent._find_targets(players)

        assert result["all"][0]["reliability"] == pytest.approx(0.85)

    def test_find_targets_reliability_none_when_no_prior(self):
        agent = StatsAgent(config={"gameweeks": 0})
        agent._next_gw_id = 30

        players = [self._make_player(1, ownership=10.0)]
        result = agent._find_targets(players)

        assert result["all"][0]["reliability"] is None

    def test_compute_differentials_includes_reliability_from_priors(self):
        from fpl_cli.services.player_prior import PlayerPrior

        agent = StatsAgent(config={"gameweeks": 0, "differential_threshold": 15.0, "semi_differential_threshold": 15.0})
        agent._player_priors = {
            2: PlayerPrior(prior_strength=0.5, confidence=0.6, source="history", reliability=0.70),
        }
        agent._next_gw_id = 30

        players = [self._make_player(2, ownership=5.0)]
        result = agent._find_differentials(players)

        assert result["all"][0]["reliability"] == pytest.approx(0.70)


class TestStatsAgentEarlySeasonPriorBlend:
    """Target and differential ranking carries the prior blend (#206).

    Going into GW2 form and ppg are one observation of one match and both
    caps saturate on a single good game, so the ranked lists put a one-game
    wonder above a quiet-starting elite. The position-mean shrinkage these
    views used to run afterwards could not fix that — it compresses gaps
    rather than reordering them — so the prior now enters the quality
    baseline inside the score, and the shrinkage pass is gone.
    """

    @staticmethod
    def _fwd(player_id: int, *, form: float, ppg: float, xgi: float, minutes: int) -> dict:
        return {
            "id": player_id, "player_name": f"Player{player_id}", "team_short": "TST",
            "position": "FWD", "price": 80, "ownership": 8.0,
            "minutes": minutes, "goals": 0, "assists": 0, "GI": 0,
            "xG": 0.0, "xA": 0.0, "xGI": 0.0,
            "xG_per_90": 0.0, "xA_per_90": 0.0, "xGI_per_90": xgi,
            "goals_minus_xG": 0.0, "assists_minus_xA": 0.0, "GI_minus_xGI": 0.0,
            "form": form, "total_points": 0, "ppg": ppg, "dc_per_90": 0.0,
            "npxG_per_90": None, "xGChain_per_90": None, "xGBuildup_per_90": None,
            "penalty_xG": None, "penalty_xG_per_90": None,
            "matchup_score": 5.0, "next_opponent": "CHE",
        }

    def _pool(self) -> list[dict]:
        return [
            self._fwd(1, form=9.0, ppg=9.0, xgi=1.4, minutes=65),   # one-game wonder
            self._fwd(2, form=2.0, ppg=2.0, xgi=0.9, minutes=90),   # quiet elite
        ]

    @staticmethod
    def _priors() -> dict:
        from fpl_cli.services.player_prior import PlayerPrior, _compute_confidence
        return {
            1: PlayerPrior(0.2, _compute_confidence(2, 0.2), "price"),
            2: PlayerPrior(1.0, _compute_confidence(2, 1.0), "history"),
        }

    def _agent(self, priors: dict | None) -> StatsAgent:
        agent = StatsAgent(config={
            "gameweeks": 0,
            "differential_threshold": 5.0,
            "semi_differential_threshold": 15.0,
        })
        agent._player_priors = priors
        agent._next_gw_id = 2
        return agent

    def test_targets_invert_without_priors(self):
        """The defect: pure observation puts the wonder on top."""
        result = self._agent(None)._find_targets(self._pool())
        assert [p["id"] for p in result["all"]] == [1, 2]

    def test_targets_rank_the_quiet_elite_first_with_priors(self):
        result = self._agent(self._priors())._find_targets(self._pool())
        assert [p["id"] for p in result["all"]] == [2, 1]

    def test_differentials_rank_the_quiet_elite_first_with_priors(self):
        result = self._agent(self._priors())._find_differentials(self._pool())
        assert [p["id"] for p in result["all"]] == [2, 1]

    def test_scores_are_the_blend_alone_not_blend_plus_shrinkage(self):
        """The two devices are never stacked: the ranked score is exactly the
        score the formula returned, with nothing pulled toward a mean after.
        """
        agent = self._agent(self._priors())
        priors = self._priors()
        pool = self._pool()
        ranked = agent._find_targets(pool)
        for entry in ranked["all"]:
            player = next(p for p in pool if p["id"] == entry["id"])
            assert entry["target_score"] == agent._calculate_target_score(
                player, priors[entry["id"]],
            )

    def test_ruled_out_player_is_not_handed_last_seasons_standing(self):
        """The #122 hold-out survives the move inside the blend."""
        from fpl_cli.services.player_prior import PlayerPrior
        pool = self._pool()
        ruled_out = self._fwd(3, form=0.0, ppg=0.0, xgi=0.0, minutes=0)
        ruled_out["chance_of_playing"] = 0
        ruled_out["status"] = "i"
        pool.append(ruled_out)

        agent = self._agent({**self._priors(), 3: PlayerPrior(1.0, 0.0, "history")})
        result = agent._find_targets(pool)

        assert [p["id"] for p in result["all"]][-1] == 3


def _scoring_data(*, next_gw_id: int, player_priors, players):
    """A minimal real ScoringData for driving StatsAgent.run().

    ``team_fixture_map`` is empty on purpose: matchup enrichment then
    short-circuits to a neutral score, which keeps the harness to the seams
    these tests are actually about.
    """
    from unittest.mock import MagicMock

    from fpl_cli.services.scoring import ScoringContext, ScoringData

    teams = [make_team(id=1, short_name="ARS")]
    team_map = {t.id: t for t in teams}
    ratings_service = MagicMock()
    return ScoringData(
        teams=teams,
        team_map=team_map,
        all_fixtures=[],
        next_gw_fixtures=[],
        next_gw_id=next_gw_id,
        next_gw={"id": next_gw_id, "is_next": True},
        scoring_ctx=ScoringContext(
            team_map=team_map,
            team_fixture_map={},
            ratings_service=ratings_service,
            next_gw_id=next_gw_id,
        ),
        ratings_service=ratings_service,
        players=players,
        player_histories={},
        player_priors=player_priors,
        adjusted_npxg_lookup=None,
        consistency_lookup=None,
    )


async def _run_stats(
    views: set[str],
    *,
    next_gw_id: int = 2,
    player_priors=None,
    players=None,
    config=None,
    histories=None,
):
    """Drive ``StatsAgent.run`` over one qualifying player with every seam stubbed."""
    from unittest.mock import AsyncMock, patch

    if players is None:
        players = [
            make_player(
                id=1, web_name="Salah", team_id=1, position=PlayerPosition.MIDFIELDER,
                form=6.0, points_per_game=5.0, minutes=900, total_points=90,
                expected_goals=5.0, expected_assists=3.0,
            ),
        ]
    current_gw_id = max(next_gw_id - 1, 1)
    finished = [gw for gw in range(1, 39) if gw < current_gw_id]
    agent_config = dict(config) if config is not None else {"gameweeks": 0, "min_minutes": 0}
    agent_config["views"] = views
    agent = StatsAgent(config=agent_config)

    async def _player_detail(player_id: int):
        """Spread each player's season minutes evenly over the finished gameweeks.

        The windowed branch calls this per player; leaving it live lets
        pytest-socket's block be swallowed by the gather's return_exceptions
        and hides the dependency behind an empty window.
        """
        if histories is not None:
            return {"history": histories.get(player_id, [])}
        player = next((p for p in players if p.id == player_id), None)
        per_gw = (player.minutes // len(finished)) if player and finished else 0
        return {
            "history": [
                {
                    "round": gw, "minutes": per_gw, "goals_scored": 0, "assists": 0,
                    "expected_goals": "0.1", "expected_assists": "0.1", "total_points": 2,
                }
                for gw in finished
            ],
        }

    with (
        patch.object(agent.client, "get_players", new_callable=AsyncMock, return_value=players),
        patch.object(
            agent.client, "get_teams", new_callable=AsyncMock,
            return_value=[make_team(id=1, short_name="ARS")],
        ),
        patch.object(
            agent.client, "get_current_gameweek", new_callable=AsyncMock,
            return_value={"id": current_gw_id},
        ),
        patch.object(
            agent.client, "get_gameweeks", new_callable=AsyncMock,
            return_value=[
                {"id": gw, "finished": gw in finished} for gw in range(1, 39)
            ],
        ),
        patch.object(agent.client, "get_player_detail", side_effect=_player_detail),
        patch(
            "fpl_cli.agents.analysis.stats.fetch_understat_lookup",
            new_callable=AsyncMock, return_value={},
        ),
        patch(
            "fpl_cli.agents.analysis.stats.prepare_scoring_data",
            new_callable=AsyncMock,
            return_value=_scoring_data(
                next_gw_id=next_gw_id, player_priors=player_priors, players=players,
            ),
        ),
    ):
        return await agent.run()


class TestStatsAgentEarlySeasonNotice:
    """The ownership views say whether their scores are prior-informed (#206).

    Only the agent knows whether the priors loaded — the loader swallows a
    failed history fetch and returns None — so the notice is decided here and
    carried in the result for the command to route to its reader's channel.
    """

    @staticmethod
    def _prior_map():
        from fpl_cli.services.player_prior import PlayerPrior

        return {1: PlayerPrior(0.5, 0.6, "history")}

    async def test_targets_notice_names_only_the_target_score(self):
        result = await _run_stats({"targets"}, player_priors=self._prior_map())
        warnings = result.data["warnings"]
        assert [w["code"] for w in warnings] == ["early_season_prior_informed"]
        assert "target_score" in warnings[0]["message"]
        assert "differential_score" not in warnings[0]["message"]

    async def test_differentials_notice_names_only_the_differential_score(self):
        result = await _run_stats({"differentials"}, player_priors=self._prior_map())
        message = result.data["warnings"][0]["message"]
        assert "differential_score" in message
        assert "target_score" not in message

    async def test_both_views_name_both_scores(self):
        result = await _run_stats(
            {"targets", "differentials"}, player_priors=self._prior_map(),
        )
        message = result.data["warnings"][0]["message"]
        assert "target_score and differential_score" in message

    async def test_degraded_priors_report_pure_observation(self):
        """The loader returns None on a failed history fetch, and the same
        command and gameweek would otherwise print a pedigree-informed ranking
        and a raw one with identical metadata.
        """
        result = await _run_stats({"targets"}, player_priors=None)
        assert [w["code"] for w in result.data["warnings"]] == [
            "early_season_small_sample"
        ]

    async def test_no_notice_once_the_blend_has_extinguished(self):
        from fpl_cli.services.player_prior import CUTOFF_GW

        result = await _run_stats(
            {"targets"}, next_gw_id=CUTOFF_GW, player_priors=self._prior_map(),
        )
        assert result.data["warnings"] == []

    async def test_views_that_score_no_blended_field_get_no_slot(self):
        """value_picks and the xG views carry no prior-blended score, so a
        notice there would caveat a number that does not exist.

        The key itself is always present -- the minutes floor can put its own
        notice there whatever the view -- so this asserts on the contents.
        """
        result = await _run_stats({"value_picks"}, player_priors=self._prior_map())
        assert result.data["warnings"] == []


class TestStatsAgentMinutesFloor:
    """The minutes floor scales to the gameweeks played (#227).

    ``min_minutes`` defaults to 60 minutes per gameweek in the window (360 for
    the default six-gameweek window) or 450 for a whole-season run, and neither
    is reachable before GW6/GW8: after ``N`` finished gameweeks nobody can have
    played more than ``N * 90`` minutes. Left alone the agent filtered out every
    player in August and reported "0 qualified" with no way to tell that from a
    real absence of candidates.
    """

    @staticmethod
    def _players(*minutes: int):
        return [
            make_player(
                id=i, web_name=f"P{i}", team_id=1, position=PlayerPosition.MIDFIELDER,
                minutes=m, form=3.0, points_per_game=3.0, total_points=10,
                expected_goals=1.0, expected_assists=1.0,
            )
            for i, m in enumerate(minutes, start=1)
        ]

    async def test_default_window_finds_players_before_gw6(self):
        """The reported defect: at GW3 the 360-minute default admitted nobody."""
        result = await _run_stats(
            {"top_xgi_per_90"},
            next_gw_id=4,  # current GW3, two finished
            players=self._players(180, 150, 60),
            config={},  # default window of 6, no explicit floor
        )

        assert result.data["min_minutes"] == 90  # 45 * 2 finished gameweeks
        assert result.data["gameweeks_played"] == 2
        assert result.data["qualified_players"] == 2
        assert result.data["empty_reason"] is None

    async def test_window_is_clamped_to_gameweeks_played(self):
        result = await _run_stats(
            {"top_xgi_per_90"},
            next_gw_id=4,
            players=self._players(180),
            config={},
        )

        assert result.data["gameweeks"] == 2
        assert "clamped" in result.data["window_label"]

    async def test_scaled_floor_carries_a_notice(self):
        result = await _run_stats(
            {"top_xgi_per_90"},
            next_gw_id=4,
            players=self._players(180),
            config={},
        )

        codes = [w["code"] for w in result.data["warnings"]]
        assert codes == ["early_season_minutes_floor"]
        message = result.data["warnings"][0]["message"]
        assert "360-minute floor" in message
        assert "90 minutes" in message

    async def test_configured_floor_binds_once_the_season_catches_up(self):
        """From GW9 the 45-per-gameweek bar exceeds 360, so nothing is scaled."""
        result = await _run_stats(
            {"top_xgi_per_90"},
            next_gw_id=10,  # current GW9, eight finished
            players=self._players(700, 200),
            config={},
        )

        assert result.data["min_minutes"] == 360
        assert result.data["gameweeks"] == 6
        assert result.data["window_label"] == "last 6 GWs"
        assert result.data["warnings"] == []
        # Windowed branch: only the 700-minute regular clears 360 in the window.
        assert result.data["qualified_players"] == 1

    async def test_whole_season_floor_scales_too(self):
        """`fpl xg --all` asks for 450 minutes, unreachable until GW10."""
        result = await _run_stats(
            {"top_xgi_per_90"},
            next_gw_id=4,
            players=self._players(180),
            config={"gameweeks": None},
        )

        assert result.data["min_minutes"] == 90
        assert result.data["window_label"] == "whole season"
        assert result.data["qualified_players"] == 1

    async def test_explicit_floor_is_honoured_verbatim(self):
        """`fpl targets --min-minutes` is a filter the reader chose."""
        result = await _run_stats(
            {"top_xgi_per_90"},
            next_gw_id=4,
            players=self._players(180, 40),
            config={"gameweeks": None, "min_minutes": 60},
        )

        assert result.data["min_minutes"] == 60
        assert result.data["qualified_players"] == 1
        assert result.data["warnings"] == []

    async def test_empty_result_blames_the_floor_when_players_have_played(self):
        result = await _run_stats(
            {"top_xgi_per_90"},
            next_gw_id=4,
            players=self._players(120, 30),
            config={"gameweeks": None, "min_minutes": 400},
        )

        assert result.data["qualified_players"] == 0
        assert result.data["empty_reason"]["code"] == "below_minutes_floor"
        assert "400-minute floor" in result.data["empty_reason"]["message"]
        assert "120 minutes" in result.data["empty_reason"]["message"]

    async def test_empty_result_blames_the_data_before_a_ball_is_kicked(self):
        result = await _run_stats(
            {"top_xgi_per_90"},
            next_gw_id=2,  # current GW1, none finished
            players=self._players(0, 0),
            config={},
        )

        assert result.data["gameweeks_played"] == 0
        assert result.data["qualified_players"] == 0
        assert result.data["empty_reason"]["code"] == "no_minutes_played"
        # The scaled floor bottoms out at a minute rather than zero: a player
        # who has not played is never "qualified".
        assert result.data["min_minutes"] == 1
        assert result.data["window_label"] == "no gameweek played yet"


class TestStatsAgentWindowPrefilter:
    """The history prefilter cannot sit above the floor it feeds (#243 review).

    The windowed path first narrows to players with enough *season* minutes to
    be worth a history fetch. Season minutes are never below windowed minutes,
    so a prefilter above the applied floor stops bounding the fetch and starts
    deciding the result -- dropping players the floor admits and reproducing
    #227 one filter earlier.
    """

    async def test_a_scaled_floor_below_90_admits_a_short_season(self):
        players = [
            make_player(
                id=1, web_name="Cameo", team_id=1, position=PlayerPosition.MIDFIELDER,
                minutes=60, form=3.0, points_per_game=3.0, total_points=10,
                expected_goals=1.0, expected_assists=1.0,
            ),
        ]
        result = await _run_stats(
            {"top_xgi_per_90"},
            next_gw_id=3,  # current GW2, one finished -> floor scales to 45
            players=players,
            config={"gameweeks": 1},
            histories={1: [{
                "round": 2, "minutes": 60, "goals_scored": 0, "assists": 0,
                "expected_goals": "0.3", "expected_assists": "0.2", "total_points": 3,
            }]},
        )

        assert result.data["min_minutes"] == 45
        # 60 season minutes is under the old fixed 90 prefilter, over the floor.
        assert result.data["qualified_players"] == 1

    async def test_the_prefilter_still_holds_at_90_once_the_floor_is_higher(self):
        """Mid-season the floor is well above 90, so the prefilter does not move."""
        players = [
            make_player(
                id=1, web_name="Regular", team_id=1, position=PlayerPosition.MIDFIELDER,
                minutes=700, form=6.0, points_per_game=5.0, total_points=80,
                expected_goals=5.0, expected_assists=3.0,
            ),
            make_player(
                id=2, web_name="Fringe", team_id=1, position=PlayerPosition.MIDFIELDER,
                minutes=80, form=1.0, points_per_game=1.0, total_points=5,
                expected_goals=0.2, expected_assists=0.1,
            ),
        ]
        result = await _run_stats(
            {"top_xgi_per_90"},
            next_gw_id=10,  # current GW9, eight finished -> floor binds at 360
            players=players,
            config={},
        )

        assert result.data["min_minutes"] == 360
        assert result.data["qualified_players"] == 1


class TestFinishedGameweekIds:
    """One predicate for "which gameweeks have finished" (#243 review).

    Three call sites reduce it differently -- the count (`fpl xg`'s minutes
    floor), the maximum (`fpl ratings update --use-xg`'s sample size, league
    recap's live-gameweek test) -- and a postponement separates the two.
    """

    def test_returns_only_finished_ids_in_order(self):
        from fpl_cli.api.fpl import finished_gameweek_ids

        gameweeks = [
            {"id": 3, "finished": True},
            {"id": 1, "finished": True},
            {"id": 4, "finished": False},
            {"id": 2, "finished": True},
        ]
        assert finished_gameweek_ids(gameweeks) == [1, 2, 3]

    def test_a_postponed_gameweek_lowers_the_count_but_not_the_maximum(self):
        from fpl_cli.api.fpl import finished_gameweek_ids

        # GW2 postponed and still unfinished while GW3 and GW4 completed.
        gameweeks = [
            {"id": 1, "finished": True},
            {"id": 2, "finished": False},
            {"id": 3, "finished": True},
            {"id": 4, "finished": True},
        ]
        ids = finished_gameweek_ids(gameweeks)
        assert len(ids) == 3   # football actually played
        assert max(ids) == 4   # how far the season has got

    def test_a_season_that_has_not_started_is_empty(self):
        from fpl_cli.api.fpl import finished_gameweek_ids

        assert finished_gameweek_ids([{"id": 1, "finished": False}]) == []
        assert finished_gameweek_ids([]) == []

    def test_a_missing_finished_key_is_not_finished(self):
        from fpl_cli.api.fpl import finished_gameweek_ids

        assert finished_gameweek_ids([{"id": 1}, {"id": 2, "finished": True}]) == [2]
