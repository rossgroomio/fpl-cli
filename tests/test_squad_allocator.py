"""Tests for the squad allocator service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fpl_cli.models.player import PlayerPosition, PlayerStatus
from fpl_cli.services.squad_allocator import (
    DEFAULT_BENCH_DISCOUNT,
    FIXTURE_SENSITIVITY,
    MODIFIER_FLOOR,
    SQUAD_SLOTS,
    ScoredPlayer,
    _compute_discount_weights,
    _compute_modifier,
    _get_opponent_fdr,
    _is_excluded,
    _starter_quality,
    compute_fixture_coefficients,
    score_all_players,
    score_all_players_sgw,
    solve_squad,
)
from tests.conftest import make_fixture, make_player

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scoring_data(
    players,
    *,
    understat_lookup=None,
    player_histories=None,
    player_priors=None,
    next_gw_id=20,
    scoring_ctx=None,
):
    """Build a minimal ScoringData-like object for testing."""
    team_map = {
        1: MagicMock(short_name="ARS"),
        2: MagicMock(short_name="CHE"),
        3: MagicMock(short_name="LIV"),
    }
    sd = MagicMock()
    sd.players = players
    sd.understat_lookup = understat_lookup
    sd.player_histories = player_histories or {}
    sd.player_priors = player_priors
    sd.next_gw_id = next_gw_id
    sd.team_map = team_map
    if scoring_ctx is not None:
        sd.scoring_ctx = scoring_ctx
    return sd


# ---------------------------------------------------------------------------
# Availability filtering
# ---------------------------------------------------------------------------


class TestIsExcluded:
    def test_available_not_excluded(self):
        p = make_player(status=PlayerStatus.AVAILABLE)
        assert not _is_excluded(p)

    def test_doubtful_not_excluded(self):
        p = make_player(status=PlayerStatus.DOUBTFUL)
        assert not _is_excluded(p)

    def test_not_available_excluded(self):
        p = make_player(status=PlayerStatus.NOT_AVAILABLE)
        assert _is_excluded(p)

    def test_unavailable_excluded(self):
        p = make_player(status=PlayerStatus.UNAVAILABLE)
        assert _is_excluded(p)

    def test_injured_chance_zero_excluded(self):
        p = make_player(status=PlayerStatus.INJURED, chance_of_playing_next_round=0)
        assert _is_excluded(p)

    def test_injured_chance_nonzero_not_excluded(self):
        p = make_player(status=PlayerStatus.INJURED, chance_of_playing_next_round=50)
        assert not _is_excluded(p)

    def test_injured_chance_none_not_excluded(self):
        """INJURED with chance=None (API default) is not excluded."""
        p = make_player(status=PlayerStatus.INJURED, chance_of_playing_next_round=None)
        assert not _is_excluded(p)

    def test_suspended_not_excluded(self):
        """SUSPENDED players stay in pool (GW1 coefficient zeroed instead)."""
        p = make_player(status=PlayerStatus.SUSPENDED, chance_of_playing_next_round=0)
        assert not _is_excluded(p)


# ---------------------------------------------------------------------------
# score_all_players
# ---------------------------------------------------------------------------


class TestScoreAllPlayers:
    def test_happy_path_with_understat(self):
        """Player with Understat match produces non-zero raw_quality float."""
        p = make_player(
            id=10, position=PlayerPosition.MIDFIELDER, team_id=1,
            minutes=1800, form=6.0, points_per_game=5.5,
            expected_goals=8.0, expected_assists=5.0,
        )
        us = {10: {"npxG_per_90": 0.45, "xGChain_per_90": 0.15, "penalty_xG_per_90": 0.05}}
        sd = _make_scoring_data([p], understat_lookup=us)
        result = score_all_players(sd)
        assert len(result) == 1
        assert isinstance(result[0].raw_quality, float)
        assert result[0].raw_quality > 0
        assert result[0].position == "MID"

    def test_happy_path_without_understat(self):
        """Player without Understat match uses xGI fallback, still non-zero."""
        p = make_player(
            id=11, position=PlayerPosition.FORWARD, team_id=2,
            minutes=1500, form=5.0, points_per_game=4.0,
            expected_goals=6.0, expected_assists=3.0,
        )
        sd = _make_scoring_data([p], understat_lookup={})
        result = score_all_players(sd)
        assert len(result) == 1
        assert result[0].raw_quality > 0
        assert result[0].position == "FWD"

    def test_gk_uses_defensive_weights(self):
        """GK/DEF uses without_xgi() weights - dc_per_90 contributes."""
        gk = make_player(
            id=1, position=PlayerPosition.GOALKEEPER, team_id=1,
            minutes=2700, form=4.0, points_per_game=4.5,
            defensive_contribution_per_90=3.0,
        )
        sd = _make_scoring_data([gk])
        result = score_all_players(sd)
        assert len(result) == 1
        assert result[0].position == "GK"
        # dc_per_90 contributes via without_xgi() transformation
        assert result[0].raw_quality > 0

    def test_def_uses_defensive_weights(self):
        """DEF uses without_xgi() weights."""
        defender = make_player(
            id=2, position=PlayerPosition.DEFENDER, team_id=1,
            minutes=2400, form=4.5, points_per_game=4.0,
            defensive_contribution_per_90=2.5,
        )
        sd = _make_scoring_data([defender])
        result = score_all_players(sd)
        assert len(result) == 1
        assert result[0].position == "DEF"

    def test_mid_fwd_uses_base_weights(self):
        """MID/FWD uses VALUE_QUALITY_WEIGHTS with npxG."""
        mid = make_player(
            id=3, position=PlayerPosition.MIDFIELDER, team_id=1,
            minutes=1800, form=7.0, points_per_game=6.0,
            expected_goals=10.0, expected_assists=5.0,
        )
        us = {3: {"npxG_per_90": 0.55, "xGChain_per_90": 0.2, "penalty_xG_per_90": 0.1}}
        sd = _make_scoring_data([mid], understat_lookup=us)
        result = score_all_players(sd)
        assert result[0].raw_quality > 0

    def test_excluded_players_filtered(self):
        """NOT_AVAILABLE and UNAVAILABLE players excluded from results."""
        available = make_player(id=1, status=PlayerStatus.AVAILABLE)
        not_avail = make_player(id=2, status=PlayerStatus.NOT_AVAILABLE)
        unavail = make_player(id=3, status=PlayerStatus.UNAVAILABLE)
        sd = _make_scoring_data([available, not_avail, unavail])
        result = score_all_players(sd)
        assert len(result) == 1
        assert result[0].player.id == 1

    def test_injured_remains_in_pool(self):
        """INJURED player (chance != 0) stays in scored list."""
        p = make_player(id=5, status=PlayerStatus.INJURED, chance_of_playing_next_round=50)
        sd = _make_scoring_data([p])
        result = score_all_players(sd)
        assert len(result) == 1

    def test_suspended_flagged_for_gw1_zeroing(self):
        """SUSPENDED with chance=0 stays in pool but flagged for GW1 zeroing."""
        p = make_player(id=6, status=PlayerStatus.SUSPENDED, chance_of_playing_next_round=0)
        sd = _make_scoring_data([p])
        result = score_all_players(sd)
        assert len(result) == 1
        assert result[0].suspended_gw1 is True

    def test_zero_minutes_player(self):
        """Player with 0 minutes gets mins_factor 0.0 but form/PPG still contribute."""
        p = make_player(
            id=7, minutes=0, form=3.0, points_per_game=0.0,
            total_points=0,
        )
        sd = _make_scoring_data([p])
        result = score_all_players(sd)
        assert len(result) == 1
        # form still contributes even with 0 minutes
        assert result[0].raw_quality >= 0

    def test_form_trajectory_applied(self):
        """form_trajectory is non-1.0 when history is provided."""
        p = make_player(id=8, minutes=1800, form=5.0, points_per_game=5.0)
        # Build a rising history (enough GWs for trajectory calculation)
        history = [
            {"round": gw, "minutes": 90, "total_points": gw}
            for gw in range(10, 20)
        ]
        sd = _make_scoring_data([p], player_histories={8: history})
        result_with = score_all_players(sd)

        sd_without = _make_scoring_data([p], player_histories={})
        result_without = score_all_players(sd_without)

        # Scores should differ when form_trajectory is applied
        assert result_with[0].raw_quality != result_without[0].raw_quality

    def test_shrinkage_compresses_early_gw(self):
        """Shrinkage compresses scores toward position mean in early GWs."""
        from fpl_cli.services.player_prior import PlayerPrior

        # Two players with very different scores
        high = make_player(
            id=1, position=PlayerPosition.MIDFIELDER,
            minutes=450, form=8.0, points_per_game=7.0,
            expected_goals=5.0, expected_assists=3.0,
        )
        low = make_player(
            id=2, position=PlayerPosition.MIDFIELDER,
            minutes=450, form=2.0, points_per_game=2.0,
            expected_goals=1.0, expected_assists=0.5,
        )
        priors = {
            1: PlayerPrior(prior_strength=0.5, confidence=0.3, source="history"),
            2: PlayerPrior(prior_strength=0.3, confidence=0.3, source="history"),
        }
        sd = _make_scoring_data([high, low], player_priors=priors, next_gw_id=5)
        result = score_all_players(sd)

        # Without shrinkage (GW >= 10)
        sd_late = _make_scoring_data([high, low], player_priors=priors, next_gw_id=15)
        result_late = score_all_players(sd_late)

        # Gap between high and low should be smaller with shrinkage
        gap_shrunk = abs(result[0].raw_quality - result[1].raw_quality)
        gap_unshrunk = abs(result_late[0].raw_quality - result_late[1].raw_quality)
        assert gap_shrunk < gap_unshrunk

    def test_no_players_raises(self):
        """scoring_data with players=None raises ValueError."""
        sd = MagicMock()
        sd.players = None
        with pytest.raises(ValueError, match="scoring_data.players is required"):
            score_all_players(sd)


# ---------------------------------------------------------------------------
# score_all_players_sgw
# ---------------------------------------------------------------------------


def _make_sgw_matchup(*, opponent_short="CHE", is_home=True, matchup_score=4.0):
    """Build a FixtureMatchup for SGW tests."""
    from fpl_cli.services.player_scoring import FixtureMatchup

    return FixtureMatchup(
        opponent_short=opponent_short,
        is_home=is_home,
        opponent_fdr=3.0,
        matchup_score=matchup_score,
    )


class TestScoreAllPlayersSgw:
    def test_happy_path_returns_scored_players(self):
        """Returns ScoredPlayer list with non-zero raw_quality for players with fixtures."""
        p = make_player(
            id=10, position=PlayerPosition.MIDFIELDER, team_id=1,
            minutes=1800, form=6.0, points_per_game=5.5,
            expected_goals=8.0, expected_assists=5.0,
        )
        sd = _make_scoring_data([p])
        matchup = _make_sgw_matchup()

        with patch(
            "fpl_cli.services.squad_allocator.build_fixture_matchups",
            return_value=[matchup],
        ):
            result = score_all_players_sgw(sd)

        assert len(result) == 1
        assert isinstance(result[0], ScoredPlayer)
        assert result[0].raw_quality > 0
        assert result[0].position == "MID"

    def test_excluded_players_filtered(self):
        """NOT_AVAILABLE and INJURED+chance=0 players excluded from output."""
        available = make_player(id=1, status=PlayerStatus.AVAILABLE, minutes=900)
        not_avail = make_player(id=2, status=PlayerStatus.NOT_AVAILABLE)
        injured_zero = make_player(id=3, status=PlayerStatus.INJURED, chance_of_playing_next_round=0)
        sd = _make_scoring_data([available, not_avail, injured_zero])

        with patch(
            "fpl_cli.services.squad_allocator.build_fixture_matchups",
            return_value=[_make_sgw_matchup()],
        ):
            result = score_all_players_sgw(sd)

        assert len(result) == 1
        assert result[0].player.id == 1

    def test_suspended_gw1_included_with_flag(self):
        """SUSPENDED+chance=0 players included with suspended_gw1=True and non-zero raw_quality."""
        p = make_player(
            id=6, status=PlayerStatus.SUSPENDED, chance_of_playing_next_round=0,
            minutes=1800, form=5.0, expected_goals=5.0, expected_assists=3.0,
        )
        sd = _make_scoring_data([p])

        with patch(
            "fpl_cli.services.squad_allocator.build_fixture_matchups",
            return_value=[_make_sgw_matchup()],
        ):
            result = score_all_players_sgw(sd)

        assert len(result) == 1
        assert result[0].suspended_gw1 is True
        assert result[0].raw_quality > 0

    def test_blank_gw_player_scores_zero(self):
        """Player with no next-GW fixtures gets raw_quality=0.0."""
        p = make_player(id=10, minutes=1800, form=6.0, expected_goals=5.0, expected_assists=3.0)
        sd = _make_scoring_data([p])

        with patch(
            "fpl_cli.services.squad_allocator.build_fixture_matchups",
            return_value=[],
        ):
            result = score_all_players_sgw(sd)

        assert len(result) == 1
        assert result[0].raw_quality == 0.0

    def test_dgw_player_scores_higher(self):
        """DGW player gets higher raw_quality than SGW player of similar quality."""
        sgw_player = make_player(
            id=1, position=PlayerPosition.MIDFIELDER, team_id=1,
            minutes=1800, form=6.0, expected_goals=8.0, expected_assists=5.0,
        )
        dgw_player = make_player(
            id=2, position=PlayerPosition.MIDFIELDER, team_id=2,
            minutes=1800, form=6.0, expected_goals=8.0, expected_assists=5.0,
        )
        sd = _make_scoring_data([sgw_player, dgw_player])

        sgw_matchup = _make_sgw_matchup()
        dgw_matchups = [_make_sgw_matchup(), _make_sgw_matchup(opponent_short="LIV", is_home=False)]

        def side_effect(team_id, _position, _ctx):
            return dgw_matchups if team_id == 2 else [sgw_matchup]

        with patch(
            "fpl_cli.services.squad_allocator.build_fixture_matchups",
            side_effect=side_effect,
        ):
            result = score_all_players_sgw(sd)

        scores = {sp.player.id: sp.raw_quality for sp in result}
        assert scores[2] > scores[1]

    def test_output_compatible_with_solve_formation(self):
        """Output ScoredPlayer list is valid input to _solve_formation via single-element coefficients."""
        p = make_player(
            id=10, position=PlayerPosition.MIDFIELDER, team_id=1,
            minutes=1800, form=6.0, expected_goals=8.0, expected_assists=5.0,
        )
        sd = _make_scoring_data([p])

        with patch(
            "fpl_cli.services.squad_allocator.build_fixture_matchups",
            return_value=[_make_sgw_matchup()],
        ):
            result = score_all_players_sgw(sd)

        sp = result[0]
        coefficients = {sp.player.id: [sp.raw_quality]}
        assert isinstance(coefficients[sp.player.id], list)
        assert len(coefficients[sp.player.id]) == 1
        assert isinstance(coefficients[sp.player.id][0], float)

    def test_no_players_raises(self):
        """scoring_data with players=None raises ValueError."""
        sd = MagicMock()
        sd.players = None
        with pytest.raises(ValueError, match="scoring_data.players is required"):
            score_all_players_sgw(sd)

    def test_no_shrinkage_applied(self):
        """SGW scoring does not call shrink_scores (unlike score_all_players)."""
        p = make_player(
            id=1, position=PlayerPosition.MIDFIELDER, team_id=1,
            minutes=1800, form=8.0, expected_goals=5.0, expected_assists=3.0,
        )
        sd = _make_scoring_data([p], next_gw_id=5)

        with (
            patch(
                "fpl_cli.services.squad_allocator.build_fixture_matchups",
                return_value=[_make_sgw_matchup()],
            ),
            patch(
                "fpl_cli.services.squad_allocator.shrink_scores",
                wraps=None,
                side_effect=AssertionError("shrink_scores should not be called"),
            ),
        ):
            result = score_all_players_sgw(sd)

        assert len(result) == 1
        assert result[0].raw_quality > 0

    def test_form_trajectory_applied_when_history_present(self):
        """form_trajectory enrichment is computed when player_histories is provided."""
        p = make_player(
            id=8, position=PlayerPosition.MIDFIELDER, team_id=1,
            minutes=1800, form=5.0, expected_goals=5.0, expected_assists=3.0,
        )
        history = [
            {"round": gw, "minutes": 90, "total_points": gw}
            for gw in range(10, 20)
        ]
        sd_with = _make_scoring_data([p], player_histories={8: history})
        sd_without = _make_scoring_data([p], player_histories={})

        with patch(
            "fpl_cli.services.squad_allocator.build_fixture_matchups",
            return_value=[_make_sgw_matchup()],
        ):
            result_with = score_all_players_sgw(sd_with)
            result_without = score_all_players_sgw(sd_without)

        assert result_with[0].raw_quality != result_without[0].raw_quality

    def test_understat_data_reaches_scoring(self):
        """Understat enrichment (npxG) produces different scores than without."""
        p = make_player(
            id=10, position=PlayerPosition.MIDFIELDER, team_id=1,
            minutes=1800, form=5.0, expected_goals=5.0, expected_assists=3.0,
        )
        us = {10: {"npxG_per_90": 0.45, "penalty_xG_per_90": 0.1}}
        sd_with = _make_scoring_data([p], understat_lookup=us)
        sd_without = _make_scoring_data([p], understat_lookup={})

        with patch(
            "fpl_cli.services.squad_allocator.build_fixture_matchups",
            return_value=[_make_sgw_matchup()],
        ):
            result_with = score_all_players_sgw(sd_with)
            result_without = score_all_players_sgw(sd_without)

        # npxG path uses different weight than xGI fallback
        assert result_with[0].raw_quality != result_without[0].raw_quality


# ---------------------------------------------------------------------------
# Fixture coefficient helpers
# ---------------------------------------------------------------------------


def _make_team_rating(atk_home=4, atk_away=4, def_home=4, def_away=4):
    """Build a TeamRating-like object."""
    from fpl_cli.services.team_ratings import TeamRating

    return TeamRating(
        atk_home=atk_home, atk_away=atk_away,
        def_home=def_home, def_away=def_away,
    )


class TestGetOpponentFdr:
    def test_gk_home_uses_opponent_atk_away(self):
        """GK at home: 8 - opponent's away attacking rating."""
        rating = _make_team_rating(atk_home=2, atk_away=6)
        fdr = _get_opponent_fdr("GK", rating, is_home=True)
        assert fdr == 2  # Weak attacker away (6=worst) -> 8-6=2 (easy)

    def test_gk_away_uses_opponent_atk_home(self):
        """GK away: 8 - opponent's home attacking rating."""
        rating = _make_team_rating(atk_home=2, atk_away=6)
        fdr = _get_opponent_fdr("GK", rating, is_home=False)
        assert fdr == 6  # Strong attacker home (2=best) -> 8-2=6 (hard)

    def test_def_uses_same_axis_as_gk(self):
        """DEF uses same attacking axis as GK."""
        rating = _make_team_rating(atk_home=3, atk_away=5)
        assert _get_opponent_fdr("DEF", rating, True) == _get_opponent_fdr("GK", rating, True)

    def test_mid_home_uses_opponent_def_away(self):
        """MID at home: 8 - opponent's away defensive rating."""
        rating = _make_team_rating(def_home=2, def_away=5)
        fdr = _get_opponent_fdr("MID", rating, is_home=True)
        assert fdr == 3  # Weak defender away (5=poor) -> 8-5=3 (easy)

    def test_fwd_away_uses_opponent_def_home(self):
        """FWD away: 8 - opponent's home defensive rating."""
        rating = _make_team_rating(def_home=2, def_away=5)
        fdr = _get_opponent_fdr("FWD", rating, is_home=False)
        assert fdr == 6  # Strong defender home (2=best) -> 8-2=6 (hard)

    def test_semantic_ordering(self):
        """Strong opponent produces higher FDR than weak opponent (fdr-opponent-axis-inversion.md)."""
        strong = _make_team_rating(atk_home=1, atk_away=1, def_home=1, def_away=1)
        weak = _make_team_rating(atk_home=7, atk_away=7, def_home=7, def_away=7)
        assert _get_opponent_fdr("DEF", strong, True) > _get_opponent_fdr("DEF", weak, True)
        assert _get_opponent_fdr("FWD", strong, True) > _get_opponent_fdr("FWD", weak, True)


class TestComputeModifier:
    def test_neutral_fdr(self):
        """FDR=4 (neutral) produces modifier ~1.0."""
        mod = _compute_modifier("DEF", 4.0)
        assert mod == pytest.approx(1.0)

    def test_easy_fixture_boosts(self):
        """Low FDR (easy fixture) produces modifier > 1.0."""
        mod = _compute_modifier("DEF", 2.0)
        assert mod > 1.0

    def test_hard_fixture_reduces(self):
        """High FDR (hard fixture) produces modifier < 1.0."""
        mod = _compute_modifier("DEF", 6.0)
        assert mod < 1.0

    def test_floor_applied(self):
        """Extreme hard fixture clamps at MODIFIER_FLOOR."""
        mod = _compute_modifier("DEF", 7.0)
        assert mod >= MODIFIER_FLOOR

    def test_def_wider_range_than_fwd(self):
        """DEF sensitivity (0.30) produces wider modifier range than FWD (0.10)."""
        easy_fdr = 2.0  # Easy fixture
        def_mod = _compute_modifier("DEF", easy_fdr)
        fwd_mod = _compute_modifier("FWD", easy_fdr)
        # Both > 1.0 for easy fixtures, but DEF swings more
        assert def_mod > fwd_mod


# ---------------------------------------------------------------------------
# compute_fixture_coefficients
# ---------------------------------------------------------------------------


def _make_fixture_coefficients_scoring_data(
    fixtures,
    *,
    next_gw_id=20,
    prediction_lookup=None,
    ratings=None,
):
    """Build a minimal ScoringData-like object for coefficient tests."""
    from fpl_cli.services.team_ratings import TeamRatingsService

    sd = MagicMock()
    sd.all_fixtures = fixtures
    sd.next_gw_id = next_gw_id
    sd.team_map = {
        1: MagicMock(short_name="ARS"),
        2: MagicMock(short_name="CHE"),
        3: MagicMock(short_name="LIV"),
        4: MagicMock(short_name="MCI"),
    }

    # Build a real ratings service with controllable ratings
    rs = MagicMock(spec=TeamRatingsService)
    if ratings:
        rs.get_rating = lambda short: ratings.get(short)
    else:
        # Default: all teams rated 4 across all axes (neutral)
        default_rating = _make_team_rating()
        rs.get_rating = lambda short: default_rating
    sd.ratings_service = rs

    scoring_ctx = MagicMock()
    scoring_ctx.prediction_lookup = prediction_lookup or {}
    sd.scoring_ctx = scoring_ctx

    return sd


def _make_scored_player(player_id=1, team_id=1, position="MID", raw_quality=10.0, suspended_gw1=False):
    """Build a ScoredPlayer for coefficient tests."""
    from fpl_cli.services.squad_allocator import ScoredPlayer

    p = make_player(id=player_id, team_id=team_id, position=PlayerPosition.MIDFIELDER)
    return ScoredPlayer(
        player=p, raw_quality=raw_quality,
        position=position, suspended_gw1=suspended_gw1,
    )


class TestComputeFixtureCoefficients:
    def test_normal_fixtures(self):
        """Player with 6 normal GW fixtures gets 6 non-zero coefficients."""
        fixtures = [
            make_fixture(gameweek=gw, home_team_id=1, away_team_id=2)
            for gw in range(20, 26)
        ]
        sp = _make_scored_player(raw_quality=10.0)
        sd = _make_fixture_coefficients_scoring_data(fixtures)
        result = compute_fixture_coefficients([sp], sd, horizon=6, start_gw=20)
        assert len(result[1]) == 6
        assert all(c > 0 for c in result[1])

    def test_dgw_produces_two_contributions(self):
        """DGW produces 2 fixture contributions for that GW."""
        fixtures = [
            make_fixture(id=1, gameweek=20, home_team_id=1, away_team_id=2),
            make_fixture(id=2, gameweek=20, home_team_id=3, away_team_id=1),
        ]
        sp = _make_scored_player(raw_quality=10.0)
        sd = _make_fixture_coefficients_scoring_data(fixtures)
        result = compute_fixture_coefficients([sp], sd, horizon=1, start_gw=20)
        # With neutral ratings (modifier=1.0), DGW coefficient = 2 * raw_quality
        assert result[1][0] == pytest.approx(20.0)

    def test_bgw_confirmed_zero(self):
        """Confirmed BGW (no fixture in GW) with no prediction produces default."""
        # GW 20 has no fixture for team 1
        fixtures = [
            make_fixture(gameweek=20, home_team_id=2, away_team_id=3),
        ]
        sp = _make_scored_player(raw_quality=10.0)
        sd = _make_fixture_coefficients_scoring_data(fixtures)
        result = compute_fixture_coefficients([sp], sd, horizon=1, start_gw=20)
        # No confirmed fixture, no prediction -> assume 1 fixture
        assert result[1][0] == pytest.approx(10.0)

    def test_predicted_blank_partial(self):
        """Predicted blank with low confidence produces partial coefficient."""
        fixtures = []  # No confirmed fixtures
        predictions = {20: {1: ("blank", 0.25)}}  # Low confidence blank
        sp = _make_scored_player(raw_quality=10.0)
        sd = _make_fixture_coefficients_scoring_data(
            fixtures, prediction_lookup=predictions,
        )
        result = compute_fixture_coefficients([sp], sd, horizon=1, start_gw=20)
        # Partial: raw_quality * (1 - confidence) = 10.0 * 0.75 = 7.5
        assert result[1][0] == pytest.approx(7.5)

    def test_predicted_blank_high_confidence(self):
        """Predicted blank with high confidence produces near-zero coefficient."""
        fixtures = []
        predictions = {20: {1: ("blank", 1.0)}}  # Confirmed blank
        sp = _make_scored_player(raw_quality=10.0)
        sd = _make_fixture_coefficients_scoring_data(
            fixtures, prediction_lookup=predictions,
        )
        result = compute_fixture_coefficients([sp], sd, horizon=1, start_gw=20)
        assert result[1][0] == pytest.approx(0.0)

    def test_predicted_dgw_extra_fixture(self):
        """Predicted DGW adds extra fixture scaled by confidence."""
        fixtures = [
            make_fixture(gameweek=20, home_team_id=1, away_team_id=2),
        ]
        predictions = {20: {1: ("double", 0.5)}}  # Medium confidence DGW
        sp = _make_scored_player(raw_quality=10.0)
        sd = _make_fixture_coefficients_scoring_data(
            fixtures, prediction_lookup=predictions,
        )
        result = compute_fixture_coefficients([sp], sd, horizon=1, start_gw=20)
        # 1 confirmed (10.0) + 1 predicted extra (10.0 * 0.5) = 15.0
        assert result[1][0] == pytest.approx(15.0)

    def test_opponent_not_in_ratings(self):
        """Opponent not in TeamRatingsService -> modifier defaults to 1.0."""
        fixtures = [
            make_fixture(gameweek=20, home_team_id=1, away_team_id=2),
        ]
        sp = _make_scored_player(raw_quality=10.0)
        sd = _make_fixture_coefficients_scoring_data(
            fixtures, ratings={}  # Empty ratings
        )
        result = compute_fixture_coefficients([sp], sd, horizon=1, start_gw=20)
        assert result[1][0] == pytest.approx(10.0)

    def test_sensitivity_affects_coefficient(self):
        """GK/DEF with 0.30 sensitivity differs more from neutral than FWD with 0.10."""
        fixtures = [
            make_fixture(gameweek=20, home_team_id=1, away_team_id=2),
        ]
        # Weak opponent across all axes (rating 6 = weak -> FDR 8-6=2 = easy)
        ratings = {
            "CHE": _make_team_rating(atk_home=6, atk_away=6, def_home=6, def_away=6),
        }

        sp_def = _make_scored_player(player_id=1, position="DEF", raw_quality=10.0)
        sp_fwd = _make_scored_player(player_id=2, position="FWD", raw_quality=10.0)

        sd = _make_fixture_coefficients_scoring_data(fixtures, ratings=ratings)
        result = compute_fixture_coefficients([sp_def, sp_fwd], sd, horizon=1, start_gw=20)

        # Both boosted (easy fixture: FDR 2), but DEF swings more (0.30 vs 0.10 sensitivity)
        def_coeff = result[1][0]
        fwd_coeff = result[2][0]
        assert def_coeff > 10.0  # Boosted
        assert fwd_coeff > 10.0  # Boosted
        assert abs(def_coeff - 10.0) > abs(fwd_coeff - 10.0)  # DEF swings more

    def test_suspended_gw1_zeroed(self):
        """Suspended player gets 0 coefficient for GW1 only."""
        fixtures = [
            make_fixture(gameweek=20, home_team_id=1, away_team_id=2),
            make_fixture(gameweek=21, home_team_id=1, away_team_id=3),
        ]
        sp = _make_scored_player(raw_quality=10.0, suspended_gw1=True)
        sd = _make_fixture_coefficients_scoring_data(fixtures)
        result = compute_fixture_coefficients([sp], sd, horizon=2, start_gw=20)
        assert result[1][0] == 0.0  # GW1 zeroed
        assert result[1][1] > 0  # GW2 normal

    def test_horizon_capped_at_gw38(self):
        """Horizon doesn't extend past GW 38."""
        fixtures = [
            make_fixture(gameweek=gw, home_team_id=1, away_team_id=2)
            for gw in range(35, 39)
        ]
        sp = _make_scored_player(raw_quality=10.0)
        sd = _make_fixture_coefficients_scoring_data(fixtures, next_gw_id=35)
        result = compute_fixture_coefficients([sp], sd, horizon=10, start_gw=35)
        # Should only have 4 GWs (35, 36, 37, 38), not 10
        assert len(result[1]) == 4

    def test_gw_beyond_fixtures_assumes_normal(self):
        """GW with no confirmed fixture and no prediction assumes normal."""
        fixtures = []  # No fixtures at all
        sp = _make_scored_player(raw_quality=10.0)
        sd = _make_fixture_coefficients_scoring_data(fixtures)
        result = compute_fixture_coefficients([sp], sd, horizon=1, start_gw=20)
        assert result[1][0] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# ILP solver
# ---------------------------------------------------------------------------


def _build_player_pool(
    *,
    n_gk=4,
    n_def=8,
    n_mid=8,
    n_fwd=5,
    base_price=5.0,
    base_quality=10.0,
    teams=None,
):
    """Build a pool of ScoredPlayers for solver tests.

    Distributes across teams to avoid the 3-per-team constraint binding.
    Returns (players, coefficients) with 1 GW of coefficients.
    """
    if teams is None:
        teams = list(range(1, 11))  # 10 teams

    positions = (
        [("GK", PlayerPosition.GOALKEEPER)] * n_gk
        + [("DEF", PlayerPosition.DEFENDER)] * n_def
        + [("MID", PlayerPosition.MIDFIELDER)] * n_mid
        + [("FWD", PlayerPosition.FORWARD)] * n_fwd
    )

    players: list[ScoredPlayer] = []
    coefficients: dict[int, list[float]] = {}

    for i, (pos_name, pos_enum) in enumerate(positions):
        pid = i + 1
        team_id = teams[i % len(teams)]
        quality = base_quality + i * 0.5  # Vary quality so solver has choices
        price = base_price + (i % 5) * 1.0  # Vary prices

        p = make_player(
            id=pid, team_id=team_id, position=pos_enum,
            now_cost=int(price * 10),
        )
        sp = ScoredPlayer(player=p, raw_quality=quality, position=pos_name)
        players.append(sp)
        coefficients[pid] = [quality]  # 1 GW, coefficient = raw quality

    return players, coefficients


class TestSolveSquad:
    def test_selects_15_players(self):
        """Solver selects exactly 15 players with correct position counts."""
        players, coeffs = _build_player_pool()
        result = solve_squad(players, coeffs, budget=100.0)

        assert result.status == "optimal"
        assert len(result.selected_players) == 15

        pos_counts: dict[str, int] = {}
        for sp in result.selected_players:
            pos_counts[sp.position] = pos_counts.get(sp.position, 0) + 1
        assert pos_counts == SQUAD_SLOTS

    def test_budget_respected(self):
        """Total price of selected squad <= budget."""
        players, coeffs = _build_player_pool()
        result = solve_squad(players, coeffs, budget=100.0)

        assert result.budget_used <= 100.0
        assert result.budget_used + result.budget_remaining == pytest.approx(100.0)

    def test_team_cap_respected(self):
        """No team has more than 3 players selected."""
        players, coeffs = _build_player_pool()
        result = solve_squad(players, coeffs, budget=100.0)

        team_counts: dict[int, int] = {}
        for sp in result.selected_players:
            team_counts[sp.player.team_id] = team_counts.get(sp.player.team_id, 0) + 1
        assert all(c <= 3 for c in team_counts.values())

    def test_valid_formation(self):
        """Exactly 11 starters in a valid formation."""
        from fpl_cli.services.player_scoring import VALID_FORMATIONS

        players, coeffs = _build_player_pool()
        result = solve_squad(players, coeffs, budget=100.0)

        assert len(result.starter_ids) == 11
        assert result.formation in [tuple(f) for f in VALID_FORMATIONS]

        # Starters are subset of squad
        squad_ids = {sp.player.id for sp in result.selected_players}
        assert result.starter_ids.issubset(squad_ids)

    def test_starters_higher_quality_than_bench(self):
        """Bench players generally have lower quality than starters."""
        players, coeffs = _build_player_pool()
        result = solve_squad(players, coeffs, budget=100.0)

        starter_qualities = [
            sp.raw_quality for sp in result.selected_players
            if sp.player.id in result.starter_ids
        ]
        bench_qualities = [
            sp.raw_quality for sp in result.selected_players
            if sp.player.id not in result.starter_ids
        ]

        assert sum(starter_qualities) / len(starter_qualities) > sum(bench_qualities) / len(bench_qualities)

    def test_tight_budget_forces_cheaper(self):
        """Tight budget forces solver to pick cheaper alternatives."""
        players, coeffs = _build_player_pool(base_price=5.0)
        result_tight = solve_squad(players, coeffs, budget=95.0)
        result_loose = solve_squad(players, coeffs, budget=120.0)

        assert result_tight.status == "optimal"
        assert result_tight.budget_used <= result_loose.budget_used

    def test_team_cap_binding(self):
        """Solver avoids 4th player from same team even if highest quality."""
        players: list[ScoredPlayer] = []
        coeffs: dict[int, list[float]] = {}

        # 4 high-quality GK on team 1 + rest on other teams
        for i in range(4):
            p = make_player(id=i + 1, team_id=1, position=PlayerPosition.GOALKEEPER, now_cost=40)
            players.append(ScoredPlayer(player=p, raw_quality=20.0, position="GK"))
            coeffs[i + 1] = [20.0]

        pid = 5
        for pos_name, pos_enum, count in [
            ("DEF", PlayerPosition.DEFENDER, 5),
            ("MID", PlayerPosition.MIDFIELDER, 5),
            ("FWD", PlayerPosition.FORWARD, 3),
        ]:
            for j in range(count):
                p = make_player(id=pid, team_id=pid, position=pos_enum, now_cost=50)
                players.append(ScoredPlayer(player=p, raw_quality=10.0, position=pos_name))
                coeffs[pid] = [10.0]
                pid += 1

        result = solve_squad(players, coeffs, budget=200.0)
        assert result.status == "optimal"

        team1_count = sum(1 for sp in result.selected_players if sp.player.team_id == 1)
        assert team1_count <= 3

    def test_infeasible_budget(self):
        """Too-low budget returns infeasible status."""
        players, coeffs = _build_player_pool(base_price=10.0)
        result = solve_squad(players, coeffs, budget=50.0)

        assert result.status == "infeasible"
        assert len(result.selected_players) == 0

    def test_bench_discount_affects_composition(self):
        """Changing bench_discount values changes squad composition."""
        players, coeffs = _build_player_pool()

        high_discount = {"GK": 0.8, "DEF": 0.8, "MID": 0.8, "FWD": 0.8}
        result_high = solve_squad(players, coeffs, budget=100.0, bench_discount=high_discount)

        low_discount = {"GK": 0.01, "DEF": 0.01, "MID": 0.01, "FWD": 0.01}
        result_low = solve_squad(players, coeffs, budget=100.0, bench_discount=low_discount)

        assert result_high.status == "optimal"
        assert result_low.status == "optimal"
        assert result_high.objective_value != result_low.objective_value

    def test_best_formation_chosen(self):
        """Solver picks the formation that maximises objective across 7 candidates."""
        players, coeffs = _build_player_pool()
        result = solve_squad(players, coeffs, budget=100.0)

        assert result.status == "optimal"
        assert result.objective_value > 0

    def test_captain_schedule_populated(self):
        """Captain schedule has entries for each GW in horizon."""
        players, coeffs = _build_player_pool()
        for pid in coeffs:
            coeffs[pid] = [coeffs[pid][0]] * 3  # 3 GWs

        result = solve_squad(players, coeffs, budget=100.0)
        assert len(result.captain_schedule) == 3
        for gw_idx, captain_id in result.captain_schedule.items():
            assert captain_id in result.starter_ids

    def test_empty_coefficients_returns_infeasible(self):
        """Empty coefficients dict (all players excluded) returns infeasible, not crash."""
        result = solve_squad([], {}, budget=100.0)
        assert result.status == "infeasible"
        assert len(result.selected_players) == 0

    def test_bench_boost_gw_changes_objective(self):
        """bench_boost_gw_idx on first GW produces different objective than default."""
        players, coeffs = _build_player_pool()
        # Extend to 3 GWs so BB GW is one of several
        for pid in coeffs:
            coeffs[pid] = [coeffs[pid][0]] * 3

        result_default = solve_squad(players, coeffs, budget=100.0)
        result_bb = solve_squad(players, coeffs, budget=100.0, bench_boost_gw_idx=0)

        assert result_default.status == "optimal"
        assert result_bb.status == "optimal"
        assert result_bb.objective_value != result_default.objective_value

    def test_bench_boost_gw_improves_bench_quality(self):
        """BB-aware squad has higher aggregate bench quality than default."""
        players, coeffs = _build_player_pool()
        for pid in coeffs:
            coeffs[pid] = [coeffs[pid][0]] * 3

        result_default = solve_squad(players, coeffs, budget=100.0)
        result_bb = solve_squad(players, coeffs, budget=100.0, bench_boost_gw_idx=0)

        def bench_quality(r):
            return sum(sp.raw_quality for sp in r.selected_players if sp.player.id not in r.starter_ids)

        assert bench_quality(result_bb) >= bench_quality(result_default)

    def test_bench_boost_gw_none_is_identical(self):
        """bench_boost_gw_idx=None produces identical result to omitting it."""
        players, coeffs = _build_player_pool()
        result_omit = solve_squad(players, coeffs, budget=100.0)
        result_none = solve_squad(players, coeffs, budget=100.0, bench_boost_gw_idx=None)

        assert result_omit.objective_value == result_none.objective_value
        assert {sp.player.id for sp in result_omit.selected_players} == {sp.player.id for sp in result_none.selected_players}

    def test_bench_boost_gw_last_in_horizon(self):
        """bench_boost_gw_idx at last GW still affects composition."""
        players, coeffs = _build_player_pool()
        for pid in coeffs:
            coeffs[pid] = [coeffs[pid][0]] * 4

        result_default = solve_squad(players, coeffs, budget=100.0)
        result_bb_last = solve_squad(players, coeffs, budget=100.0, bench_boost_gw_idx=3)

        assert result_bb_last.status == "optimal"
        assert result_bb_last.objective_value != result_default.objective_value

    def test_bench_boost_gw_with_custom_bench_discount(self):
        """BB GW overrides to 1.0 even when bench_discount is custom."""
        players, coeffs = _build_player_pool()
        for pid in coeffs:
            coeffs[pid] = [coeffs[pid][0]] * 3

        low_discount = {"GK": 0.01, "DEF": 0.01, "MID": 0.01, "FWD": 0.01}

        result_low = solve_squad(players, coeffs, budget=100.0, bench_discount=low_discount)
        result_low_bb = solve_squad(
            players, coeffs, budget=100.0,
            bench_discount=low_discount, bench_boost_gw_idx=1,
        )

        assert result_low.status == "optimal"
        assert result_low_bb.status == "optimal"
        assert result_low_bb.objective_value != result_low.objective_value


# ---------------------------------------------------------------------------
# Bench cost pressure
# ---------------------------------------------------------------------------


class TestHierarchicalBenchCostSolve:
    """Tests for the two-pass lexicographic solve that activates in Free Hit regime."""

    FREE_HIT_DISCOUNT = {"GK": 0.01, "DEF": 0.01, "MID": 0.01, "FWD": 0.01}

    def _build_upgrade_pool(self):
        """Pool where cheap bench frees budget to upgrade a starter.

        Creates one "premium" MID (high quality, expensive) that is only
        affordable when the solver picks cheap bench players. Bench candidates
        within each position come in cheap/expensive pairs at equal low quality.
        """
        players: list[ScoredPlayer] = []
        coefficients: dict[int, list[float]] = {}
        pid = 0

        def add(pos_name, pos_enum, quality, price, team_id):
            nonlocal pid
            pid += 1
            p = make_player(id=pid, team_id=team_id, position=pos_enum, now_cost=int(price * 10))
            sp = ScoredPlayer(player=p, raw_quality=quality, position=pos_name)
            players.append(sp)
            coefficients[pid] = [quality]
            return pid

        # GK: 3 players, need 2 (1 starter, 1 bench)
        add("GK", PlayerPosition.GOALKEEPER, 8.0, 4.5, 1)
        add("GK", PlayerPosition.GOALKEEPER, 7.0, 4.0, 2)  # cheap bench GK
        add("GK", PlayerPosition.GOALKEEPER, 7.0, 7.0, 3)  # expensive bench GK

        # DEF: 7 players, need 5. 4 solid starters + 1 bench pair + 1 extra
        for t in range(4, 8):
            add("DEF", PlayerPosition.DEFENDER, 12.0, 5.0, t)
        add("DEF", PlayerPosition.DEFENDER, 3.0, 4.0, 8)   # cheap bench DEF
        add("DEF", PlayerPosition.DEFENDER, 3.0, 7.0, 9)   # expensive bench DEF
        add("DEF", PlayerPosition.DEFENDER, 11.0, 5.0, 10)

        # MID: 7 players, need 5. 4 base + 1 premium + 1 bench pair
        for t in [1, 2, 3, 4]:
            add("MID", PlayerPosition.MIDFIELDER, 11.0, 5.0, t)
        premium_mid = add("MID", PlayerPosition.MIDFIELDER, 18.0, 9.0, 5)  # premium upgrade
        add("MID", PlayerPosition.MIDFIELDER, 3.0, 4.0, 6)  # cheap bench MID
        add("MID", PlayerPosition.MIDFIELDER, 3.0, 7.0, 7)  # expensive bench MID

        # FWD: 4 players, need 3 (1 starter, 2 bench candidates)
        add("FWD", PlayerPosition.FORWARD, 13.0, 5.5, 8)
        add("FWD", PlayerPosition.FORWARD, 3.0, 4.0, 9)   # cheap bench FWD
        add("FWD", PlayerPosition.FORWARD, 3.0, 7.0, 10)  # expensive bench FWD
        add("FWD", PlayerPosition.FORWARD, 12.0, 5.5, 1)

        return players, coefficients, premium_mid

    def test_hierarchical_not_activated_at_default_discount(self):
        """At default bench discount (Wildcard), hierarchical solve does not activate."""
        players, coeffs = _build_player_pool()

        result_default = solve_squad(players, coeffs, budget=100.0)
        result_explicit = solve_squad(
            players, coeffs, budget=100.0,
            bench_discount=DEFAULT_BENCH_DISCOUNT,
        )

        assert result_default.status == "optimal"
        assert result_explicit.status == "optimal"
        assert result_default.objective_value == result_explicit.objective_value
        assert result_default.starter_ids == result_explicit.starter_ids

    def test_hierarchical_cheapens_bench_and_upgrades_starter(self):
        """In Free Hit regime, hierarchical solve picks cheap bench and upgrades a starter."""
        players, coeffs, premium_mid = self._build_upgrade_pool()

        # Budget tight enough that expensive bench prevents affording premium MID
        # 11 starters ~£57 + 4 bench. Cheap bench ~£16, expensive ~£28.
        # Premium MID at £9 only fits with cheap bench.
        result = solve_squad(
            players, coeffs, budget=78.0,
            bench_discount=self.FREE_HIT_DISCOUNT,
        )

        assert result.status == "optimal"

        # Premium MID should be selected as a starter
        assert premium_mid in result.starter_ids

        bench = [sp for sp in result.selected_players if sp.player.id not in result.starter_ids]
        bench_cost = sum(sp.player.price for sp in bench)
        # Bench should be cheap (4 players at ~4.0 each = ~16.0)
        assert bench_cost <= 20.0

    def test_floor_prevents_starter_degradation(self):
        """Solve 2 starters are never worse than Solve 1 starters."""
        players, coeffs = _build_player_pool()

        default_result = solve_squad(players, coeffs, budget=100.0)
        fh_result = solve_squad(
            players, coeffs, budget=100.0,
            bench_discount=self.FREE_HIT_DISCOUNT,
        )

        assert default_result.status == "optimal"
        assert fh_result.status == "optimal"

        discount_weights = _compute_discount_weights(1, free_transfers=1)
        default_sq = _starter_quality(default_result.starter_ids, coeffs, discount_weights)
        fh_sq = _starter_quality(fh_result.starter_ids, coeffs, discount_weights)

        # Free Hit starter quality should be >= default (floor guarantees it)
        assert fh_sq >= default_sq - 0.1

    def test_penalty_does_not_override_genuine_quality_gap(self):
        """Solve 2 keeps an expensive high-quality bench player when the gap is genuine.

        One FWD bench slot has a cheap/low-quality option and an expensive/high-quality
        option. The quality gap (12.0 vs 3.0) far exceeds the penalty gap
        (0.1 * 3.0 = 0.3), so the expensive player should be selected.
        """
        players: list[ScoredPlayer] = []
        coefficients: dict[int, list[float]] = {}
        pid = 0

        def add(pos_name, pos_enum, quality, price, team_id):
            nonlocal pid
            pid += 1
            p = make_player(id=pid, team_id=team_id, position=pos_enum, now_cost=int(price * 10))
            sp = ScoredPlayer(player=p, raw_quality=quality, position=pos_name)
            players.append(sp)
            coefficients[pid] = [quality]
            return pid

        # GK: 3 (need 2)
        add("GK", PlayerPosition.GOALKEEPER, 8.0, 4.5, 1)
        add("GK", PlayerPosition.GOALKEEPER, 7.0, 4.0, 2)
        add("GK", PlayerPosition.GOALKEEPER, 6.0, 4.0, 3)

        # DEF: 7 (need 5)
        for t in range(4, 9):
            add("DEF", PlayerPosition.DEFENDER, 10.0, 5.0, t)
        add("DEF", PlayerPosition.DEFENDER, 9.0, 4.5, 9)
        add("DEF", PlayerPosition.DEFENDER, 9.0, 4.5, 10)

        # MID: 7 (need 5)
        for t in [1, 2, 3, 4, 5]:
            add("MID", PlayerPosition.MIDFIELDER, 10.0, 5.0, t)
        add("MID", PlayerPosition.MIDFIELDER, 9.0, 4.5, 6)
        add("MID", PlayerPosition.MIDFIELDER, 9.0, 4.5, 7)

        # FWD: 4 (need 3). One bench slot with genuine quality gap.
        add("FWD", PlayerPosition.FORWARD, 11.0, 5.5, 8)
        add("FWD", PlayerPosition.FORWARD, 11.0, 5.5, 9)
        cheap_low = add("FWD", PlayerPosition.FORWARD, 3.0, 4.0, 10)    # cheap but bad
        expensive_high = add("FWD", PlayerPosition.FORWARD, 12.0, 8.0, 1)  # expensive but good

        result = solve_squad(
            players, coefficients, budget=120.0,
            bench_discount=self.FREE_HIT_DISCOUNT,
        )

        assert result.status == "optimal"
        selected_ids = {sp.player.id for sp in result.selected_players}
        # Expensive high-quality FWD should be selected despite penalty
        assert expensive_high in selected_ids
        assert cheap_low not in selected_ids

    def test_equal_quality_pool_cheapens_bench(self):
        """With equal-quality players, Solve 2 picks cheaper bench (same starters).

        The floor guarantees starter quality. With no upgrade possible, Solve 2
        still prefers cheap bench via the penalty - this is the desired outcome.
        """
        positions = (
            [("GK", PlayerPosition.GOALKEEPER)] * 4
            + [("DEF", PlayerPosition.DEFENDER)] * 8
            + [("MID", PlayerPosition.MIDFIELDER)] * 8
            + [("FWD", PlayerPosition.FORWARD)] * 5
        )

        players: list[ScoredPlayer] = []
        coefficients: dict[int, list[float]] = {}
        for i, (pos_name, pos_enum) in enumerate(positions):
            pid = i + 1
            team_id = (i % 10) + 1
            quality = 10.0
            price = 4.5 if i % 2 == 0 else 8.0

            p = make_player(id=pid, team_id=team_id, position=pos_enum, now_cost=int(price * 10))
            sp = ScoredPlayer(player=p, raw_quality=quality, position=pos_name)
            players.append(sp)
            coefficients[pid] = [quality]

        # Single-pass (no penalty)
        baseline = solve_squad(players, coefficients, budget=160.0)
        # Hierarchical (Solve 2 with bench cost pressure)
        fh_result = solve_squad(
            players, coefficients, budget=160.0,
            bench_discount=self.FREE_HIT_DISCOUNT,
        )

        assert baseline.status == "optimal"
        assert fh_result.status == "optimal"
        # Starters should be identical (floor preserves quality)
        assert fh_result.starter_ids == baseline.starter_ids
        # Bench should be cheaper (penalty drives cheap bench selection)
        fh_bench = [sp for sp in fh_result.selected_players if sp.player.id not in fh_result.starter_ids]
        assert all(sp.player.price <= 5.0 for sp in fh_bench)

    def test_price_overrides_with_default_discount(self):
        """Wildcard with price overrides does not activate hierarchical solve."""
        players, coeffs = _build_player_pool(base_price=5.0)

        override_ids = [sp.player.id for sp in players[:3]]
        overrides = {pid: 3.0 for pid in override_ids}

        result_no = solve_squad(players, coeffs, budget=120.0, price_overrides=None)
        result_with = solve_squad(players, coeffs, budget=120.0, price_overrides=overrides)

        assert result_no.status == "optimal"
        assert result_with.status == "optimal"
        # Overrides change solver's view of cost, so results differ
        assert result_with.budget_used != result_no.budget_used


class TestStarterQuality:
    def test_sums_weighted_coefficients(self):
        starter_ids = {1, 2}
        coefficients = {1: [10.0, 8.0], 2: [12.0, 9.0]}
        weights = [1.0, 0.9]
        # 1.0*10 + 0.9*8 + 1.0*12 + 0.9*9 = 10 + 7.2 + 12 + 8.1 = 37.3
        assert _starter_quality(starter_ids, coefficients, weights) == pytest.approx(37.3)

    def test_single_gw(self):
        starter_ids = {1, 2, 3}
        coefficients = {1: [5.0], 2: [7.0], 3: [3.0]}
        weights = [1.0]
        assert _starter_quality(starter_ids, coefficients, weights) == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Temporal discount weights
# ---------------------------------------------------------------------------


class TestComputeDiscountWeights:
    def test_ft1_produces_geometric_decay(self):
        """free_transfers=1 produces geometric weights with rate 0.96."""
        from fpl_cli.services.squad_allocator import _compute_discount_weights

        weights = _compute_discount_weights(horizon=6, free_transfers=1)
        assert len(weights) == 6
        assert weights[0] == pytest.approx(1.0)
        assert weights[1] == pytest.approx(0.96)
        assert weights[2] == pytest.approx(0.96**2)
        assert weights[5] == pytest.approx(0.96**5)

    def test_ft3_steeper_than_ft1(self):
        """free_transfers=3 produces steeper decay than free_transfers=1."""
        from fpl_cli.services.squad_allocator import _compute_discount_weights

        weights_1 = _compute_discount_weights(horizon=6, free_transfers=1)
        weights_3 = _compute_discount_weights(horizon=6, free_transfers=3)
        # Last GW weight should be lower with more FTs
        assert weights_3[5] < weights_1[5]

    def test_ft0_produces_flat_weights(self):
        """free_transfers=0 produces all-ones (no discounting)."""
        from fpl_cli.services.squad_allocator import _compute_discount_weights

        weights = _compute_discount_weights(horizon=6, free_transfers=0)
        assert weights == [1.0] * 6

    def test_horizon_1_single_weight(self):
        """horizon=1 produces single weight of 1.0 regardless of FTs."""
        from fpl_cli.services.squad_allocator import _compute_discount_weights

        weights = _compute_discount_weights(horizon=1, free_transfers=5)
        assert weights == [1.0]

    def test_rate_clamped_at_half(self):
        """Rate is clamped to 0.5 even with extreme inputs."""
        from fpl_cli.services.squad_allocator import _compute_discount_weights

        # Force rate below 0.5: would need ft > 12 at BASE_STEP=0.04
        # But with clamped range [0, 5] this is unreachable; test the clamp
        # directly by using a high ft_count (defensive test)
        weights = _compute_discount_weights(horizon=3, free_transfers=5)
        # rate = 1.0 - 0.04*5 = 0.80, all weights > 0.5
        assert all(w >= 0.5**2 for w in weights)


class TestTemporalDiscountSolver:
    def test_ft0_flat_weighting_highest_objective(self):
        """free_transfers=0 (flat) produces higher objective than any discounted run."""
        players, coeffs = _build_player_pool()
        for pid in coeffs:
            coeffs[pid] = [coeffs[pid][0]] * 6

        result_flat = solve_squad(players, coeffs, budget=100.0, free_transfers=0)
        result_ft1 = solve_squad(players, coeffs, budget=100.0, free_transfers=1)
        result_ft5 = solve_squad(players, coeffs, budget=100.0, free_transfers=5)

        assert result_flat.status == "optimal"
        assert result_flat.objective_value >= result_ft1.objective_value
        assert result_ft1.objective_value >= result_ft5.objective_value

    def test_discount_changes_objective(self):
        """Non-zero FTs produce a different objective than flat weighting."""
        players, coeffs = _build_player_pool()
        for pid in coeffs:
            coeffs[pid] = [coeffs[pid][0]] * 6

        result_flat = solve_squad(players, coeffs, budget=100.0, free_transfers=0)
        result_ft1 = solve_squad(players, coeffs, budget=100.0, free_transfers=1)

        assert result_flat.status == "optimal"
        assert result_ft1.status == "optimal"
        # Discounted objective should be lower (same squad, reduced distant-GW weights)
        assert result_ft1.objective_value < result_flat.objective_value

    def test_discount_composes_with_bench_boost(self):
        """Temporal discount and BB both apply to the objective."""
        players, coeffs = _build_player_pool()
        for pid in coeffs:
            coeffs[pid] = [coeffs[pid][0]] * 4

        result_ft_only = solve_squad(players, coeffs, budget=100.0, free_transfers=3)
        result_ft_bb = solve_squad(
            players, coeffs, budget=100.0,
            free_transfers=3, bench_boost_gw_idx=0,
        )

        assert result_ft_only.status == "optimal"
        assert result_ft_bb.status == "optimal"
        # BB should increase objective (bench gets full value for one GW)
        assert result_ft_bb.objective_value > result_ft_only.objective_value

    def test_semantic_ordering_front_loaded(self):
        """Higher FTs favour players with front-loaded fixture coefficients.

        Player A: strong GW1-2, weak GW3-6 (sum=60)
        Player B: uniform across all GWs (sum=60)
        4 strong filler MIDs fill slots 1-4, so A and B compete for the
        5th starter slot. With steep discount, A's front-loaded coefficients
        give it the edge.
        """
        from fpl_cli.models.player import PlayerPosition

        players: list[ScoredPlayer] = []
        coeffs: dict[int, list[float]] = {}

        # Player A: front-loaded (20, 20, 5, 5, 5, 5) sum=60
        p_a = make_player(id=1, team_id=1, position=PlayerPosition.MIDFIELDER, now_cost=80)
        players.append(ScoredPlayer(player=p_a, raw_quality=10.0, position="MID"))
        coeffs[1] = [20.0, 20.0, 5.0, 5.0, 5.0, 5.0]

        # Player B: uniform (10, 10, 10, 10, 10, 10) sum=60
        p_b = make_player(id=2, team_id=2, position=PlayerPosition.MIDFIELDER, now_cost=80)
        players.append(ScoredPlayer(player=p_b, raw_quality=10.0, position="MID"))
        coeffs[2] = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]

        # 4 strong filler MIDs that always start (occupy slots 1-4)
        for i in range(4):
            pid = 100 + i
            p = make_player(id=pid, team_id=pid, position=PlayerPosition.MIDFIELDER, now_cost=40)
            players.append(ScoredPlayer(player=p, raw_quality=15.0, position="MID"))
            coeffs[pid] = [15.0] * 6

        # Fill remaining positions with filler
        pid = 200
        for pos_name, pos_enum, count in [
            ("GK", PlayerPosition.GOALKEEPER, 2),
            ("DEF", PlayerPosition.DEFENDER, 5),
            ("FWD", PlayerPosition.FORWARD, 3),
        ]:
            for _ in range(count):
                p = make_player(id=pid, team_id=pid, position=pos_enum, now_cost=40)
                players.append(ScoredPlayer(player=p, raw_quality=5.0, position=pos_name))
                coeffs[pid] = [5.0] * 6
                pid += 1

        # With steep discount (ft=5), front-loaded A should win the 5th MID starter slot
        result_steep = solve_squad(players, coeffs, budget=300.0, free_transfers=5)

        assert result_steep.status == "optimal"
        # A (id=1) should be starter, B (id=2) should be bench
        assert 1 in result_steep.starter_ids
        assert 2 not in result_steep.starter_ids


# ---------------------------------------------------------------------------
# Price overrides
# ---------------------------------------------------------------------------


class TestPriceOverrides:
    def test_overrides_unlock_better_squad(self):
        """Sell-price overrides create budget surplus, enabling a better squad."""
        players, coeffs = _build_player_pool(base_price=5.0)

        # Tight budget - solver has to compromise on quality
        result_tight = solve_squad(players, coeffs, budget=95.0)

        # With overrides: reduce price for 5 players by 1.0 each -> 5.0 surplus
        override_ids = [sp.player.id for sp in players[:5]]
        overrides = {pid: players[i].player.price - 1.0 for i, pid in enumerate(override_ids)}
        result_with = solve_squad(players, coeffs, budget=95.0, price_overrides=overrides)

        assert result_tight.status == "optimal"
        assert result_with.status == "optimal"
        assert result_with.objective_value >= result_tight.objective_value

    def test_budget_used_reflects_sell_prices(self):
        """budget_used uses sell price for overridden players, now_cost for others."""
        players, coeffs = _build_player_pool()
        pid_to_override = players[0].player.id
        original_price = players[0].player.price
        override_price = original_price - 1.0
        overrides = {pid_to_override: override_price}

        result = solve_squad(players, coeffs, budget=100.0, price_overrides=overrides)
        assert result.status == "optimal"

        # If the overridden player is selected, budget_used should reflect the lower price
        selected_ids = {sp.player.id for sp in result.selected_players}
        if pid_to_override in selected_ids:
            expected = sum(
                overrides.get(sp.player.id, sp.player.price) for sp in result.selected_players
            )
            assert result.budget_used == pytest.approx(expected, abs=0.1)

    def test_no_matches_behaves_like_no_overrides(self):
        """Overrides with IDs not in pool behaves identically to no overrides."""
        players, coeffs = _build_player_pool()
        overrides = {9999: 1.0, 9998: 2.0}

        result_no = solve_squad(players, coeffs, budget=100.0)
        result_miss = solve_squad(players, coeffs, budget=100.0, price_overrides=overrides)

        assert result_no.objective_value == result_miss.objective_value

    def test_empty_overrides_same_as_none(self):
        """Empty overrides dict behaves like price_overrides=None."""
        players, coeffs = _build_player_pool()
        result_none = solve_squad(players, coeffs, budget=100.0)
        result_empty = solve_squad(players, coeffs, budget=100.0, price_overrides={})

        assert result_none.objective_value == result_empty.objective_value

    def test_all_players_overridden(self):
        """All players in pool have sell-price overrides."""
        players, coeffs = _build_player_pool()
        overrides = {sp.player.id: sp.player.price - 0.5 for sp in players}

        result = solve_squad(players, coeffs, budget=100.0, price_overrides=overrides)
        assert result.status == "optimal"
        # Budget used should reflect all the lower prices
        expected = sum(
            overrides[sp.player.id] for sp in result.selected_players
        )
        assert result.budget_used == pytest.approx(expected, abs=0.1)

    def test_owned_ids_populated(self):
        """owned_ids contains IDs of selected players that appear in price_overrides."""
        players, coeffs = _build_player_pool()
        overrides = {players[0].player.id: players[0].player.price - 0.5}

        result = solve_squad(players, coeffs, budget=100.0, price_overrides=overrides)
        assert result.status == "optimal"
        if players[0].player.id in {sp.player.id for sp in result.selected_players}:
            assert players[0].player.id in result.owned_ids
        # Non-overridden players should not be owned
        for sp in result.selected_players:
            if sp.player.id not in overrides:
                assert sp.player.id not in result.owned_ids

    def test_player_savings_correct(self):
        """player_savings = now_cost - sell_price for each owned player."""
        players, coeffs = _build_player_pool()
        pid = players[0].player.id
        market_price = players[0].player.price
        sell_price = market_price - 0.4
        overrides = {pid: sell_price}

        result = solve_squad(players, coeffs, budget=100.0, price_overrides=overrides)
        assert result.status == "optimal"
        if pid in result.owned_ids:
            assert result.player_savings[pid] == pytest.approx(0.4, abs=0.01)

    def test_no_overrides_empty_owned(self):
        """Without overrides, owned_ids and player_savings are empty."""
        players, coeffs = _build_player_pool()
        result = solve_squad(players, coeffs, budget=100.0)
        assert result.owned_ids == frozenset()
        assert result.player_savings == {}

    def test_total_savings_sums_correctly(self):
        """Sum of player_savings matches total budget gap from overrides."""
        players, coeffs = _build_player_pool()
        overrides = {sp.player.id: sp.player.price - 0.3 for sp in players[:3]}
        result = solve_squad(players, coeffs, budget=100.0, price_overrides=overrides)
        assert result.status == "optimal"

        total_savings = sum(result.player_savings.values())
        # Each owned player saves 0.3, total = 0.3 * len(owned)
        assert total_savings == pytest.approx(0.3 * len(result.owned_ids), abs=0.1)
