"""Tests for fpl_cli.services.matchup."""

from __future__ import annotations

from unittest.mock import patch

from tests.conftest import make_fixture

from fpl_cli.services.matchup import calculate_matchup_score, compute_3gw_matchup


# ---------------------------------------------------------------------------
# calculate_matchup_score smoke test
# ---------------------------------------------------------------------------


def test_calculate_matchup_score_returns_expected_keys():
    """Smoke test: returns dict with expected keys and score in 0-10 range."""
    team_form = {
        "gs_home": 8, "gc_home": 4,
        "gs_away": 5, "gc_away": 6,
        "pts_6": 10, "league_position": 5,
    }
    opp_form = {
        "gs_home": 6, "gc_home": 7,
        "gs_away": 4, "gc_away": 8,
        "pts_6": 6, "league_position": 12,
    }
    result = calculate_matchup_score(team_form, opp_form, "MID", is_home=True)
    assert "matchup_score" in result
    assert "attack_matchup" in result
    assert "defence_matchup" in result
    assert "form_differential" in result
    assert "position_differential" in result
    assert "reasoning" in result
    assert 0 <= result["matchup_score"] <= 10


# ---------------------------------------------------------------------------
# compute_3gw_matchup tests (mock calculate_matchup_score for control)
# ---------------------------------------------------------------------------

def _make_fixtures_for_gws(team_id: int, opponent_id: int, gw_start: int, gw_count: int, extras: dict | None = None):
    """Helper to create one fixture per GW for the given team."""
    fixtures = []
    for i in range(gw_count):
        gw = gw_start + i
        fixtures.append(make_fixture(
            id=100 + i,
            gameweek=gw,
            home_team_id=team_id,
            away_team_id=opponent_id,
        ))
    if extras:
        for gw, extra_fixtures in extras.items():
            fixtures.extend(extra_fixtures)
    return fixtures


DUMMY_FORM = {1: {"some": "form"}, 2: {"some": "form"}, 3: {"some": "form"}}


@patch("fpl_cli.services.matchup.calculate_matchup_score")
def test_3gw_happy_path(mock_calc):
    """3 GWs with scores 8.0, 4.0, 4.0 -> 8*0.5 + 4*0.3 + 4*0.2 = 6.0."""
    scores = [8.0, 4.0, 4.0]
    mock_calc.side_effect = [
        {"matchup_score": s} for s in scores
    ]
    fixtures = _make_fixtures_for_gws(team_id=1, opponent_id=2, gw_start=10, gw_count=3)

    result = compute_3gw_matchup(
        team_id=1,
        all_fixtures=fixtures,
        next_gw_id=10,
        team_form_by_id=DUMMY_FORM,
        position="MID",
    )
    assert result == 6.0


@patch("fpl_cli.services.matchup.calculate_matchup_score")
def test_3gw_dgw_in_first_gw(mock_calc):
    """DGW in GW 10 (two fixtures summing to 12.0): 12*0.5 + 4*0.3 + 4*0.2 = 8.0."""
    # GW 10: two fixtures -> scores 6.0 + 6.0 = 12.0
    # GW 11: one fixture -> 4.0
    # GW 12: one fixture -> 4.0
    mock_calc.side_effect = [
        {"matchup_score": 6.0},  # GW10 fixture 1
        {"matchup_score": 6.0},  # GW10 fixture 2
        {"matchup_score": 4.0},  # GW11
        {"matchup_score": 4.0},  # GW12
    ]
    fixtures = [
        make_fixture(id=1, gameweek=10, home_team_id=1, away_team_id=2),
        make_fixture(id=2, gameweek=10, home_team_id=1, away_team_id=3),
        make_fixture(id=3, gameweek=11, home_team_id=1, away_team_id=2),
        make_fixture(id=4, gameweek=12, home_team_id=1, away_team_id=2),
    ]

    result = compute_3gw_matchup(
        team_id=1,
        all_fixtures=fixtures,
        next_gw_id=10,
        team_form_by_id={1: {"f": 1}, 2: {"f": 1}, 3: {"f": 1}},
        position="FWD",
    )
    assert result == 8.0


@patch("fpl_cli.services.matchup.calculate_matchup_score")
def test_3gw_blank_nearest_gw(mock_calc):
    """Blank in nearest GW (weight 0.5): 0*0.5 + 7*0.3 + 7*0.2 = 3.5."""
    mock_calc.side_effect = [
        {"matchup_score": 7.0},  # GW11
        {"matchup_score": 7.0},  # GW12
    ]
    # No fixture in GW 10 for team 1
    fixtures = [
        make_fixture(id=1, gameweek=11, home_team_id=1, away_team_id=2),
        make_fixture(id=2, gameweek=12, home_team_id=1, away_team_id=2),
    ]

    result = compute_3gw_matchup(
        team_id=1,
        all_fixtures=fixtures,
        next_gw_id=10,
        team_form_by_id=DUMMY_FORM,
        position="DEF",
    )
    assert result == 3.5


def test_3gw_no_fixtures_returns_zero():
    """No fixtures at all -> returns 0.0."""
    result = compute_3gw_matchup(
        team_id=1,
        all_fixtures=[],
        next_gw_id=10,
        team_form_by_id=DUMMY_FORM,
        position="MID",
    )
    assert result == 0.0


# ---------------------------------------------------------------------------
# compute_3gw_matchup with predictions
# ---------------------------------------------------------------------------


@patch("fpl_cli.services.matchup.calculate_matchup_score")
def test_predictions_ignored_when_confirmed_fixtures_exist(mock_calc):
    """Confirmed fixtures in all 3 GWs -> predictions have no effect (R1)."""
    mock_calc.side_effect = [{"matchup_score": s} for s in [8.0, 4.0, 4.0]]
    fixtures = _make_fixtures_for_gws(team_id=1, opponent_id=2, gw_start=10, gw_count=3)

    predictions = {
        10: {1: ("double", 1.0)},
        11: {1: ("blank", 1.0)},
        12: {1: ("double", 1.0)},
    }

    result = compute_3gw_matchup(
        team_id=1, all_fixtures=fixtures, next_gw_id=10,
        team_form_by_id=DUMMY_FORM, position="MID",
        predictions=predictions,
    )
    assert result == 6.0  # Same as without predictions


def test_predicted_dgw_high_confidence():
    """GW+2 predicted DGW at high (0.8): 10.0 score, weight 0.3*0.8=0.24."""
    # GW10: no fixture, GW11: no fixture (predicted DGW), GW12: no fixture
    predictions = {11: {1: ("double", 0.8)}}

    result = compute_3gw_matchup(
        team_id=1, all_fixtures=[], next_gw_id=10,
        team_form_by_id=DUMMY_FORM, position="MID",
        predictions=predictions,
    )
    # numerator: 0*0.5 + 10.0*0.24 + 0*0.2 = 2.4
    # denominator: 0.5 + 0.24 + 0.2 = 0.94
    expected = 2.4 / 0.94
    assert abs(result - expected) < 0.01


def test_predicted_bgw_medium_confidence():
    """GW+2 predicted BGW at medium (0.5): 0.0 score, weight 0.3*0.5=0.15."""
    predictions = {11: {1: ("blank", 0.5)}}

    result = compute_3gw_matchup(
        team_id=1, all_fixtures=[], next_gw_id=10,
        team_form_by_id=DUMMY_FORM, position="MID",
        predictions=predictions,
    )
    # numerator: 0*0.5 + 0*0.15 + 0*0.2 = 0.0
    # denominator: 0.5 + 0.15 + 0.2 = 0.85
    assert result == 0.0


def test_no_prediction_no_fixtures_preserves_behaviour():
    """No confirmed fixtures, no prediction -> 0.0 at full weight (R5)."""
    result_with = compute_3gw_matchup(
        team_id=1, all_fixtures=[], next_gw_id=10,
        team_form_by_id=DUMMY_FORM, position="MID",
        predictions={},
    )
    result_without = compute_3gw_matchup(
        team_id=1, all_fixtures=[], next_gw_id=10,
        team_form_by_id=DUMMY_FORM, position="MID",
        predictions=None,
    )
    assert result_with == result_without == 0.0


def test_predictions_none_backward_compat():
    """predictions=None -> identical to original behaviour."""
    result = compute_3gw_matchup(
        team_id=1, all_fixtures=[], next_gw_id=10,
        team_form_by_id=DUMMY_FORM, position="MID",
        predictions=None,
    )
    assert result == 0.0


def test_predicted_dgw_confirmed_confidence():
    """Confirmed confidence (1.0) -> full weight, same as confirmed fixture."""
    predictions = {11: {1: ("double", 1.0)}}

    result = compute_3gw_matchup(
        team_id=1, all_fixtures=[], next_gw_id=10,
        team_form_by_id=DUMMY_FORM, position="MID",
        predictions=predictions,
    )
    # numerator: 0*0.5 + 10.0*0.3 + 0*0.2 = 3.0
    # denominator: 0.5 + 0.3 + 0.2 = 1.0
    assert result == 3.0


@patch("fpl_cli.services.matchup.calculate_matchup_score")
def test_predicted_dgw_pushes_average_higher(mock_calc):
    """Team with predicted DGW scores higher than without (DGW inflation)."""
    mock_calc.side_effect = [{"matchup_score": 5.0}] * 2  # Called for both runs
    # GW10: confirmed fixture, GW11: no fixture, GW12: no fixture
    fixtures = _make_fixtures_for_gws(team_id=1, opponent_id=2, gw_start=10, gw_count=1)

    result_no_pred = compute_3gw_matchup(
        team_id=1, all_fixtures=fixtures, next_gw_id=10,
        team_form_by_id=DUMMY_FORM, position="MID",
    )

    predictions = {11: {1: ("double", 0.8)}}
    result_with_pred = compute_3gw_matchup(
        team_id=1, all_fixtures=fixtures, next_gw_id=10,
        team_form_by_id=DUMMY_FORM, position="MID",
        predictions=predictions,
    )

    assert result_with_pred > result_no_pred
