"""Tests for resolve_player() in fpl_cli/models/player.py."""

import pytest

from fpl_cli.models.player import (
    AmbiguousPlayerError,
    PlayerPosition,
    resolve_player,
    resolve_player_or_report,
    resolve_players,
    resolve_players_or_report,
)
from tests.conftest import make_player, make_team


def _players():
    return [
        make_player(id=1, web_name="Salah", first_name="Mohamed", second_name="Salah", team_id=3),
        make_player(id=2, web_name="Saka", first_name="Bukayo", second_name="Saka", team_id=1),
        make_player(id=3, web_name="Palmer", first_name="Cole", second_name="Palmer", team_id=4),
        make_player(id=4, web_name="De Bruyne", first_name="Kevin", second_name="De Bruyne", team_id=5),
        make_player(id=5, web_name="Gyökeres", first_name="Viktor", second_name="Gyökeres", team_id=1),
        make_player(id=6, web_name="Raúl", first_name="Raúl", second_name="Jiménez Rodríguez", team_id=6),
    ]


def _teams():
    return [
        make_team(id=1, name="Arsenal", short_name="ARS"),
        make_team(id=3, name="Liverpool", short_name="LIV"),
        make_team(id=4, name="Chelsea", short_name="CHE"),
        make_team(id=5, name="Manchester City", short_name="MCI"),
        make_team(id=6, name="Fulham", short_name="FUL"),
    ]


class TestResolvePlayerExactMatch:
    def test_exact_web_name(self):
        assert resolve_player("Salah", _players()).id == 1

    def test_exact_full_name(self):
        assert resolve_player("Mohamed Salah", _players()).id == 1

    def test_case_insensitive(self):
        assert resolve_player("salah", _players()).id == 1
        assert resolve_player("SAKA", _players()).id == 2


class TestResolvePlayerSubstring:
    def test_substring_web_name(self):
        assert resolve_player("Bru", _players()).id == 4

    def test_substring_full_name(self):
        assert resolve_player("Bukayo", _players()).id == 2

    def test_multiple_substring_matches_returns_first(self):
        players = [
            make_player(id=10, web_name="Smith", first_name="Adam", second_name="Smith"),
            make_player(id=11, web_name="Smithson", first_name="Bob", second_name="Smithson"),
        ]
        assert resolve_player("Smith", players).id == 10


class TestResolvePlayerEdgeCases:
    def test_no_match_returns_none(self):
        assert resolve_player("Nonexistent", _players()) is None

    def test_empty_query_returns_none(self):
        assert resolve_player("", _players()) is None

    def test_whitespace_only_returns_none(self):
        assert resolve_player("   ", _players()) is None

    def test_exact_match_preferred_over_substring(self):
        """'Sal' is a substring of 'Salah', but 'Salah' is an exact match."""
        assert resolve_player("Salah", _players()).id == 1


class TestResolvePlayerDiacritics:
    def test_ascii_matches_accented_web_name(self):
        assert resolve_player("gyokeres", _players()).id == 5

    def test_ascii_matches_accented_exact(self):
        assert resolve_player("raul", _players()).id == 6

    def test_accented_query_still_works(self):
        assert resolve_player("Gyökeres", _players()).id == 5

    def test_ascii_substring_of_accented_full_name(self):
        assert resolve_player("jimenez", _players()).id == 6


class TestResolvePlayerById:
    def test_numeric_id_exact(self):
        assert resolve_player("1", _players()).id == 1

    def test_numeric_id_not_found(self):
        assert resolve_player("999", _players()) is None


class TestResolvePlayerWithTeam:
    def test_name_with_team_code(self):
        assert resolve_player("Salah (LIV)", _players(), teams=_teams()).id == 1

    def test_name_with_team_code_case_insensitive(self):
        assert resolve_player("Salah (liv)", _players(), teams=_teams()).id == 1

    def test_disambiguates_by_team(self):
        players = _players() + [
            make_player(id=7, web_name="Neto", first_name="Pedro", second_name="Lomba Neto", team_id=4),
            make_player(id=8, web_name="João Pedro", first_name="João Pedro",
                        second_name="Junqueira de Jesus", team_id=4),
        ]
        teams = _teams()
        # "pedro (CHE)" should match within Chelsea only - Neto first (substring on "Pedro Lomba Neto")
        result = resolve_player("pedro (CHE)", players, teams=teams)
        assert result is not None
        assert result.team_id == 4
        assert result.id == 7

    def test_unknown_team_code_returns_none(self):
        assert resolve_player("Salah (XXX)", _players(), teams=_teams()) is None

    def test_team_syntax_ignored_without_teams_param(self):
        # Without teams, "(LIV)" is treated as part of the name and won't match
        assert resolve_player("Salah (LIV)", _players()) is None


def _hendersons():
    """Two players sharing a web_name - the collision from issue #180."""
    return [
        make_player(id=100, web_name="Henderson", first_name="Dean",
                    second_name="Henderson", team_id=7,
                    position=PlayerPosition.GOALKEEPER),
        make_player(id=200, web_name="Henderson", first_name="Jordan",
                    second_name="Henderson", team_id=4,
                    position=PlayerPosition.MIDFIELDER),
    ]


class TestResolvePlayerAmbiguity:
    def test_two_exact_matches_raise(self):
        with pytest.raises(AmbiguousPlayerError) as exc:
            resolve_player("Henderson", _hendersons())
        assert exc.value.query == "Henderson"
        assert [p.id for p in exc.value.matches] == [100, 200]

    def test_message_names_both_clubs_when_teams_given(self):
        teams = _teams() + [make_team(id=7, name="Crystal Palace", short_name="CRY")]
        with pytest.raises(AmbiguousPlayerError) as exc:
            resolve_player("Henderson", _hendersons(), teams=teams)
        msg = str(exc.value)
        assert "matches 2 players" in msg
        assert "Henderson (CRY)" in msg
        assert "Henderson (CHE)" in msg
        assert "disambiguate with 'Henderson (CRY)'" in msg

    def test_message_falls_back_to_ids_without_teams(self):
        """Without *teams* the (TEAM) disambiguator is inert, so offer IDs."""
        with pytest.raises(AmbiguousPlayerError) as exc:
            resolve_player("Henderson", _hendersons())
        msg = str(exc.value)
        assert "Dean Henderson [id 100]" in msg
        assert "Jordan Henderson [id 200]" in msg
        assert "disambiguate by player ID, e.g. '100'" in msg

    def test_team_disambiguator_resolves_the_tie(self):
        teams = _teams() + [make_team(id=7, name="Crystal Palace", short_name="CRY")]
        assert resolve_player("Henderson (CRY)", _hendersons(), teams=teams).id == 100
        assert resolve_player("Henderson (CHE)", _hendersons(), teams=teams).id == 200

    def test_player_id_resolves_the_tie(self):
        assert resolve_player("200", _hendersons()).id == 200

    def test_full_name_resolves_the_tie(self):
        assert resolve_player("Dean Henderson", _hendersons()).id == 100

    def test_substring_ties_still_return_first(self):
        """Substring is a fuzzy shortlist, not a tie - first-wins is the contract."""
        players = [
            make_player(id=10, web_name="Smith", first_name="Adam", second_name="Smith"),
            make_player(id=11, web_name="Smithson", first_name="Bob", second_name="Smithson"),
        ]
        assert resolve_player("Smit", players).id == 10

    def test_same_club_tie_offers_ids_not_a_club_that_cannot_separate(self):
        """Both at ARS: `Name (ARS)` would raise this same error again."""
        players = [
            make_player(id=100, web_name="Smith", first_name="Adam",
                        second_name="Smith", team_id=1),
            make_player(id=200, web_name="Smith", first_name="Ben",
                        second_name="Smith", team_id=1),
        ]
        with pytest.raises(AmbiguousPlayerError) as exc:
            resolve_player("Smith", players, teams=_teams())
        msg = str(exc.value)
        assert "disambiguate by player ID, e.g. '100'" in msg
        assert "disambiguate with" not in msg
        # The club still appears as context, just not as the suggested handle.
        assert "Adam Smith (ARS) [id 100]" in msg
        assert "Ben Smith (ARS) [id 200]" in msg

    def test_partially_separating_clubs_also_offer_ids(self):
        """Two of three share a club, so no single `Name (TEAM)` picks one out."""
        players = _hendersons() + [
            make_player(id=300, web_name="Henderson", first_name="Sam",
                        second_name="Henderson", team_id=4),
        ]
        teams = _teams() + [make_team(id=7, name="Crystal Palace", short_name="CRY")]
        with pytest.raises(AmbiguousPlayerError) as exc:
            resolve_player("Henderson", players, teams=teams)
        assert "disambiguate by player ID" in str(exc.value)

    def test_resolve_players_returns_both_without_raising(self):
        assert [p.id for p in resolve_players("Henderson", _hendersons())] == [100, 200]

    def test_ambiguous_error_is_a_value_error(self):
        assert issubclass(AmbiguousPlayerError, ValueError)


def _reporting_players():
    return _players() + _hendersons()


class TestResolvePlayerOrReport:
    def test_returns_the_player_and_leaves_errors_empty(self):
        errors: list[str] = []
        resolved = resolve_player_or_report(
            "Salah", _reporting_players(), _teams(), label="OUT", errors=errors,
        )
        assert resolved.id == 1
        assert errors == []

    def test_unresolvable_reports_the_name_and_label(self):
        errors: list[str] = []
        resolved = resolve_player_or_report(
            "Nobody", _reporting_players(), _teams(), label="OUT", errors=errors,
        )
        assert resolved is None
        assert errors == ["Could not resolve OUT player: 'Nobody'"]

    def test_ambiguous_reports_both_candidates(self):
        errors: list[str] = []
        teams = _teams() + [make_team(id=7, name="Crystal Palace", short_name="CRY")]
        resolved = resolve_player_or_report(
            "Henderson", _reporting_players(), teams, label="OUT", errors=errors,
        )
        assert resolved is None
        assert len(errors) == 1
        assert errors[0].startswith("Ambiguous OUT player:")
        assert "Henderson (CRY)" in errors[0]
        assert "Henderson (CHE)" in errors[0]

    def test_team_disambiguator_resolves_the_tie(self):
        errors: list[str] = []
        teams = _teams() + [make_team(id=7, name="Crystal Palace", short_name="CRY")]
        resolved = resolve_player_or_report(
            "Henderson (CRY)", _reporting_players(), teams, label="OUT", errors=errors,
        )
        assert resolved.id == 100
        assert errors == []

    def test_teams_are_optional(self):
        errors: list[str] = []
        resolved = resolve_player_or_report(
            "Salah", _reporting_players(), label="OUT", errors=errors,
        )
        assert resolved.id == 1
        assert errors == []


class TestResolvePlayersOrReport:
    def test_returns_players_in_order(self):
        errors: list[str] = []
        resolved = resolve_players_or_report(
            ["Saka", "Salah"], _reporting_players(), _teams(), label="bench", errors=errors,
        )
        assert [p.id for p in resolved] == [2, 1]
        assert errors == []

    def test_reports_every_failure_rather_than_stopping_at_the_first(self):
        """A caller who mistyped two names should hear about both."""
        errors: list[str] = []
        teams = _teams() + [make_team(id=7, name="Crystal Palace", short_name="CRY")]
        resolved = resolve_players_or_report(
            ["Nobody", "Salah", "Henderson"], _reporting_players(), teams,
            label="squad", errors=errors,
        )
        assert [p.id for p in resolved] == [1]
        assert len(errors) == 2
        assert "Could not resolve squad player: 'Nobody'" in errors
        assert any(e.startswith("Ambiguous squad player:") for e in errors)

    def test_accumulates_into_a_shared_error_list(self):
        """Callers resolve two lists into one list of errors before reporting."""
        errors: list[str] = []
        players = _reporting_players()
        resolve_players_or_report(
            ["Nobody"], players, _teams(), label="starting", errors=errors,
        )
        resolve_players_or_report(
            ["Nobody2"], players, _teams(), label="bench", errors=errors,
        )
        assert errors == [
            "Could not resolve starting player: 'Nobody'",
            "Could not resolve bench player: 'Nobody2'",
        ]

    def test_empty_names_resolve_to_nothing(self):
        errors: list[str] = []
        result = resolve_players_or_report(
            [], _reporting_players(), _teams(), label="bench", errors=errors,
        )
        assert result == []
        assert errors == []
