"""Tests for fine evaluation logic."""

from fpl_cli.cli._fines import (
    COHORT_ONLY_RULE_TYPES,
    FinesLeagueData,
    FinesTeamPlayer,
    evaluate_fines,
    evaluate_rules,
    rules_for_format,
)
from fpl_cli.cli._fines_config import VALID_RULE_TYPES, FineRule, FinesConfig


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

    def test_the_fine_names_who_it_fined_as_references_not_only_as_prose(self):
        """#176: the message spells out whatever the bootstrap called a player
        at ruling time. A caller storing the ruling needs to know *which*
        player that was, so the name can be restated after a rename."""
        team: list[FinesTeamPlayer] = [
            {"name": "Sávio", "red_cards": 1, "contributed": True, "auto_sub_out": False,
             "code": 510_281},
            {"name": "Calm", "red_cards": 0, "contributed": True, "auto_sub_out": False,
             "code": 118_748},
        ]
        results = evaluate_fines(_config(classic=[RED_CARD_RULE]), "classic", None, team)
        assert [(p.name, p.code) for p in results[0].players] == [("Sávio", 510_281)]

    def test_a_pick_that_never_resolved_is_named_with_no_reference(self):
        team: list[FinesTeamPlayer] = [
            {"name": "Trent", "red_cards": 1, "contributed": True, "auto_sub_out": False},
        ]
        results = evaluate_fines(_config(classic=[RED_CARD_RULE]), "classic", None, team)
        assert [(p.name, p.code) for p in results[0].players] == [("Trent", None)]

    def test_the_offenders_are_named_in_squad_order(self):
        team: list[FinesTeamPlayer] = [
            {"name": "First", "red_cards": 1, "contributed": True, "auto_sub_out": False},
            {"name": "Benched", "red_cards": 1, "contributed": False, "auto_sub_out": False},
            {"name": "Second", "red_cards": 1, "contributed": True, "auto_sub_out": False},
        ]
        results = evaluate_fines(_config(classic=[RED_CARD_RULE]), "classic", None, team)
        assert [p.name for p in results[0].players] == ["First", "Second"]
        assert "(First, Second)" in results[0].message

    def test_a_rule_that_names_nobody_records_no_players(self):
        """`last-place` and `below-threshold` describe a score, not a squad --
        so an empty list here is a real answer rather than a gap."""
        league: FinesLeagueData = {
            "user_gw_points": 20,
            "worst_performers": [{"is_user": True, "name": "Alice", "points": 20, "gross_points": 20}],
        }
        results = evaluate_fines(
            _config(classic=[LAST_PLACE_RULE, THRESHOLD_RULE]), "classic", league, [],
        )
        assert [r.triggered for r in results] == [True, True]
        assert all(r.players == () for r in results)


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

    def test_no_league_data_does_not_fine(self):
        """An unknown score must not be read as a 0-point score."""
        results = evaluate_fines(_config(draft=[THRESHOLD_RULE]), "draft", None, [])
        assert results[0].triggered is False
        assert results[0].message == "No league data available."

    def test_league_data_without_points_does_not_fine(self):
        """GW1 before the first standings build: a league name but no scores."""
        league: FinesLeagueData = {"league_name": "Office League"}  # type: ignore[typeddict-unknown-key]
        results = evaluate_fines(_config(draft=[THRESHOLD_RULE]), "draft", league, [])
        assert results[0].triggered is False
        assert results[0].message == "No league data available."

    def test_zero_points_still_fines(self):
        """A genuine 0 is still a fine - only an absent score is exempt."""
        league: FinesLeagueData = {"user_gw_points": 0}
        results = evaluate_fines(_config(draft=[THRESHOLD_RULE]), "draft", league, [])
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


class TestRuleNarrowing:
    """Splitting the rules a caller can actually rule from the ones it can't
    (issue #136): the coarse ledger tier has headline numbers and no squad."""

    def test_every_valid_rule_type_is_classified_as_cohort_only_or_not(self):
        """A new rule type must be a deliberate choice, not a default. This
        fails the moment one is added without deciding whether the coarse
        backfill can rule it."""
        assert COHORT_ONLY_RULE_TYPES <= VALID_RULE_TYPES
        needs_squad = VALID_RULE_TYPES - COHORT_ONLY_RULE_TYPES
        assert needs_squad == {"red-card"}

    def test_a_rule_declared_cohort_only_really_ignores_the_squad(self):
        """The declaration is what `_coarse_fine_rules` trusts when it runs a
        handler against no squad at all. A rule that reads `team_data` but is
        declared squad-free would rule differently there and record the
        difference as fact, so the claim is checked rather than taken
        (#165 review)."""
        from fpl_cli.cli._fines import _RULE_HANDLERS

        league: FinesLeagueData = {
            "user_gw_points": 20,
            "worst_performers": [{"is_user": True, "points": 20, "gross_points": 20, "name": "A"}],
        }
        squad = [
            {"name": "P1", "red_cards": 1, "contributed": True, "auto_sub_out": False},
            {"name": "P2", "red_cards": 0, "contributed": True, "auto_sub_out": False},
        ]

        for rule_type, rule in _RULE_HANDLERS.items():
            if rule.needs_squad:
                continue
            configured = FineRule(type=rule_type, threshold=30, penalty="Pint")
            assert rule.evaluate(configured, league, [], False) == rule.evaluate(
                configured, league, squad, False,
            ), f"'{rule_type}' is declared cohort-only but reads the squad"

    def test_rules_for_format_returns_the_configured_order(self):
        config = _config(classic=[THRESHOLD_RULE, LAST_PLACE_RULE], draft=[RED_CARD_RULE])
        assert [r.type for r in rules_for_format(config, "classic")] == [
            "below-threshold", "last-place",
        ]
        assert [r.type for r in rules_for_format(config, "draft")] == ["red-card"]

    def test_narrowing_abstains_where_evaluating_would_falsely_acquit(self):
        """An empty squad makes the red-card handler answer "no red card
        fine", which reads as a ruling. Dropping the rule records nothing at
        all instead."""
        league: FinesLeagueData = {"user_gw_points": 40}
        rules = [THRESHOLD_RULE, RED_CARD_RULE]

        acquitted = evaluate_rules(rules, league, [])
        abstained = evaluate_rules(
            [r for r in rules if r.type in COHORT_ONLY_RULE_TYPES], league, [],
        )

        assert [r.rule_type for r in acquitted] == ["below-threshold", "red-card"]
        assert [r.rule_type for r in abstained] == ["below-threshold"]

    def test_evaluate_fines_still_rules_every_configured_rule(self):
        league: FinesLeagueData = {"user_gw_points": 40}
        results = evaluate_fines(_config(classic=[THRESHOLD_RULE, RED_CARD_RULE]), "classic", league, [])
        assert [r.rule_type for r in results] == ["below-threshold", "red-card"]
