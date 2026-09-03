"""Tests for .agents/skills/gw-prep/scripts/_resolve.py, shared by the three
analysis scripts (bench_order, starting_xi, transfer_eval)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from fpl_cli.models.player import PlayerPosition
from tests.conftest import make_player, make_team


def _load_module() -> ModuleType:
    """Load `_resolve.py` the way the scripts import it (as a sys.path sibling)."""
    scripts_dir = Path(__file__).parent.parent / ".agents/skills/gw-prep/scripts"
    sys.path.insert(0, str(scripts_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "gw_prep_resolve", scripts_dir / "_resolve.py",
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(scripts_dir))
    return mod


_mod = _load_module()
resolve_one = _mod.resolve_one
resolve_all = _mod.resolve_all


@pytest.fixture
def players():
    return [
        make_player(id=1, web_name="Salah", first_name="Mohamed", second_name="Salah",
                    team_id=1, position=PlayerPosition.MIDFIELDER),
        make_player(id=2, web_name="Mbeumo", first_name="Bryan", second_name="Mbeumo",
                    team_id=4, position=PlayerPosition.MIDFIELDER),
        # Shared surname across two clubs - the issue #180 collision.
        make_player(id=3, web_name="Henderson", first_name="Dean", second_name="Henderson",
                    team_id=7, position=PlayerPosition.GOALKEEPER),
        make_player(id=4, web_name="Henderson", first_name="Jordan", second_name="Henderson",
                    team_id=4, position=PlayerPosition.MIDFIELDER),
    ]


@pytest.fixture
def teams():
    return [
        make_team(id=1, name="Arsenal", short_name="ARS"),
        make_team(id=4, name="Chelsea", short_name="CHE"),
        make_team(id=7, name="Crystal Palace", short_name="CRY"),
    ]


class TestResolveOne:
    def test_returns_the_player_and_leaves_errors_empty(self, players, teams):
        errors: list[str] = []
        assert resolve_one("Salah", players, teams, label="OUT", errors=errors).id == 1
        assert errors == []

    def test_unresolvable_reports_the_name_and_label(self, players, teams):
        errors: list[str] = []
        assert resolve_one("Nobody", players, teams, label="OUT", errors=errors) is None
        assert errors == ["Could not resolve OUT player: 'Nobody'"]

    def test_ambiguous_reports_both_candidates(self, players, teams):
        errors: list[str] = []
        assert resolve_one("Henderson", players, teams, label="OUT", errors=errors) is None
        assert len(errors) == 1
        assert errors[0].startswith("Ambiguous OUT player:")
        assert "Henderson (CRY)" in errors[0]
        assert "Henderson (CHE)" in errors[0]

    def test_team_disambiguator_resolves_the_tie(self, players, teams):
        errors: list[str] = []
        assert resolve_one("Henderson (CRY)", players, teams, label="OUT", errors=errors).id == 3
        assert errors == []


class TestResolveAll:
    def test_returns_players_in_order(self, players, teams):
        errors: list[str] = []
        resolved = resolve_all(
            ["Mbeumo", "Salah"], players, teams, label="bench", errors=errors,
        )
        assert [p.id for p in resolved] == [2, 1]
        assert errors == []

    def test_reports_every_failure_rather_than_stopping_at_the_first(self, players, teams):
        """A run that mistypes two names should hear about both."""
        errors: list[str] = []
        resolved = resolve_all(
            ["Nobody", "Salah", "Henderson"], players, teams, label="squad", errors=errors,
        )
        assert [p.id for p in resolved] == [1]
        assert len(errors) == 2
        assert "Could not resolve squad player: 'Nobody'" in errors
        assert any(e.startswith("Ambiguous squad player:") for e in errors)

    def test_accumulates_into_a_shared_error_list(self, players, teams):
        """Callers resolve two lists into one list of errors before reporting."""
        errors: list[str] = []
        resolve_all(["Nobody"], players, teams, label="starting", errors=errors)
        resolve_all(["Nobody2"], players, teams, label="bench", errors=errors)
        assert errors == [
            "Could not resolve starting player: 'Nobody'",
            "Could not resolve bench player: 'Nobody2'",
        ]

    def test_empty_names_resolve_to_nothing(self, players, teams):
        errors: list[str] = []
        assert resolve_all([], players, teams, label="bench", errors=errors) == []
        assert errors == []
