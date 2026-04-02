"""Tests for fine evaluation logic."""

from fpl_cli.cli._fines import FinesLeagueData, FinesTeamPlayer, evaluate_fines
from fpl_cli.cli._fines_config import FineRule, FinesConfig


def _config(classic: list[FineRule] | None = None, draft: list[FineRule] | None = None) -> FinesConfig:
    return FinesConfig(classic=classic or [], draft=draft or [])


LAST_PLACE_RULE = FineRule(type="last-place")
RED_CARD_RULE = FineRule(type="red-card")
THRESHOLD_RULE = FineRule(type="below-threshold", threshold=25)
CUSTOM_PENALTY_RULE = FineRule(type="last-place", penalty="Buy a round")


class TestLastPlace:
    def test_user_is_last_triggers_fine(self):
        league: FinesLeagueData = {
            "user_gw_points": 30,
            "worst_performers": [{"is_user": True, "name": "Alice", "points": 30, "gross_points": 34}],
        }
        results = evaluate_fines(_config(classic=[LAST_PLACE_RULE]), "classic", league, [])
        assert results[0].triggered is True
        assert "FINE TRIGGERED" in results[0].message
        assert "last in the gameweek" in results[0].message

    def test_user_not_last_no_fine(self):
        league: FinesLeagueData = {
            "user_gw_points": 40,
            "worst_performers": [{"is_user": False, "name": "Bob", "points": 28, "gross_points": 28}],
        }
        results = evaluate_fines(_config(classic=[LAST_PLACE_RULE]), "classic", league, [])
        assert results[0].triggered is False
        assert "No last-place fine" in results[0].message
        assert "Bob" in results[0].message

    def test_no_league_data(self):
        results = evaluate_fines(_config(classic=[LAST_PLACE_RULE]), "classic", None, [])
        assert results[0].triggered is False
        assert "No league data" in results[0].message

    def test_use_net_points_shows_net_label(self):
        league: FinesLeagueData = {
            "user_gw_points": 30,
            "worst_performers": [{"is_user": True, "name": "Alice", "points": 30, "gross_points": 34}],
        }
        results = evaluate_fines(_config(classic=[LAST_PLACE_RULE]), "classic", league, [], use_net_points=True)
        assert results[0].triggered is True
        assert "30 net pts" in results[0].message

    def test_gross_points_when_net_disabled(self):
        league: FinesLeagueData = {
            "user_gw_points": 34,
            "worst_performers": [{"is_user": True, "name": "Alice", "points": 30, "gross_points": 34}],
        }
        results = evaluate_fines(_config(classic=[LAST_PLACE_RULE]), "classic", league, [])
        assert results[0].triggered is True
        assert "34 pts" in results[0].message

    def test_draft_last_place(self):
        league: FinesLeagueData = {
            "user_gw_points": 40,
            "worst_performers": [{"is_user": True, "name": "Alice", "points": 40, "gross_points": 40}],
        }
        results = evaluate_fines(_config(draft=[LAST_PLACE_RULE]), "draft", league, [])
        assert results[0].triggered is True


class TestRedCard:
    def test_red_card_starter_triggers_fine(self):
        team: list[FinesTeamPlayer] = [{"name": "Trent", "red_cards": 1, "contributed": True, "auto_sub_out": False}]
        results = evaluate_fines(_config(classic=[RED_CARD_RULE]), "classic", None, team)
        assert results[0].triggered is True
        assert "Red card" in results[0].message
        assert "Trent" in results[0].message

    def test_red_card_bench_player_no_fine(self):
        team: list[FinesTeamPlayer] = [{"name": "Trent", "red_cards": 1, "contributed": False, "auto_sub_out": False}]
        results = evaluate_fines(_config(classic=[RED_CARD_RULE]), "classic", None, team)
        assert results[0].triggered is False
        assert "No red card fine" in results[0].message

    def test_red_card_auto_subbed_out_no_fine(self):
        team: list[FinesTeamPlayer] = [{"name": "Trent", "red_cards": 1, "contributed": True, "auto_sub_out": True}]
        results = evaluate_fines(_config(classic=[RED_CARD_RULE]), "classic", None, team)
        assert results[0].triggered is False


class TestBelowThreshold:
    def test_below_threshold_triggers_fine(self):
        league: FinesLeagueData = {"user_gw_points": 24}
        results = evaluate_fines(_config(draft=[THRESHOLD_RULE]), "draft", league, [])
        assert results[0].triggered is True
        assert "24 pts" in results[0].message

    def test_exactly_at_threshold_no_fine(self):
        league: FinesLeagueData = {"user_gw_points": 25}
        results = evaluate_fines(_config(draft=[THRESHOLD_RULE]), "draft", league, [])
        assert results[0].triggered is False
        assert "No sub-25 fine" in results[0].message

    def test_above_threshold_no_fine(self):
        league: FinesLeagueData = {"user_gw_points": 26}
        results = evaluate_fines(_config(draft=[THRESHOLD_RULE]), "draft", league, [])
        assert results[0].triggered is False

    def test_no_league_data_scores_zero(self):
        results = evaluate_fines(_config(draft=[THRESHOLD_RULE]), "draft", None, [])
        assert results[0].triggered is True
        assert "0 pts" in results[0].message

    def test_use_net_points_uses_user_gw_net_points(self):
        """Classic below-threshold with use_net_points reads user_gw_net_points."""
        league: FinesLeagueData = {"user_gw_points": 27, "user_gw_net_points": 23}
        results = evaluate_fines(_config(classic=[THRESHOLD_RULE]), "classic", league, [], use_net_points=True)
        assert results[0].triggered is True
        assert "23 net pts" in results[0].message

    def test_use_net_points_falls_back_to_user_gw_points(self):
        """Classic below-threshold with use_net_points falls back when net_points absent."""
        league: FinesLeagueData = {"user_gw_points": 24}
        results = evaluate_fines(_config(classic=[THRESHOLD_RULE]), "classic", league, [], use_net_points=True)
        assert results[0].triggered is True
        assert "24 net pts" in results[0].message

    def test_use_net_points_above_threshold_no_fine(self):
        league: FinesLeagueData = {"user_gw_points": 20, "user_gw_net_points": 28}
        results = evaluate_fines(_config(classic=[THRESHOLD_RULE]), "classic", league, [], use_net_points=True)
        assert results[0].triggered is False
        assert "28 net pts" in results[0].message


class TestEvaluateFines:
    def test_empty_rules_returns_empty(self):
        results = evaluate_fines(_config(), "classic", None, [])
        assert results == []

    def test_custom_penalty_text_in_message(self):
        league: FinesLeagueData = {
            "user_gw_points": 30,
            "worst_performers": [{"is_user": True, "name": "Alice", "points": 30, "gross_points": 30}],
        }
        results = evaluate_fines(_config(classic=[CUSTOM_PENALTY_RULE]), "classic", league, [])
        assert results[0].triggered is True
        assert "Buy a round" in results[0].message

    def test_default_penalty_text_in_message(self):
        league: FinesLeagueData = {
            "user_gw_points": 30,
            "worst_performers": [{"is_user": True, "name": "Alice", "points": 30, "gross_points": 30}],
        }
        results = evaluate_fines(_config(classic=[LAST_PLACE_RULE]), "classic", league, [])
        assert "Fine triggered" in results[0].message

    def test_multiple_rules_evaluated(self):
        league: FinesLeagueData = {
            "user_gw_points": 40,
            "worst_performers": [{"is_user": False, "name": "Bob", "points": 28, "gross_points": 28}],
        }
        team: list[FinesTeamPlayer] = [{"name": "Trent", "red_cards": 1, "contributed": True, "auto_sub_out": False}]
        config = _config(classic=[LAST_PLACE_RULE, RED_CARD_RULE])
        results = evaluate_fines(config, "classic", league, team)
        assert len(results) == 2
        assert results[0].triggered is False  # last-place
        assert results[1].triggered is True   # red-card
