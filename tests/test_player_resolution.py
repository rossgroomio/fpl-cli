"""Tests for resolve_player() in fpl_cli/models/player.py."""

from fpl_cli.models.player import resolve_player
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
