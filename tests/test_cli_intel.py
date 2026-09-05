"""Tests for the `fpl intel` command group.

Gameweek resolution and team-name validation are the only parts that touch the
network; both are patched here so the command's own behaviour is what is under
test.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
import yaml
from click.testing import CliRunner

import fpl_cli.cli.intel as intel_mod
from fpl_cli.cli import main
from fpl_cli.season import get_season_year, season_label
from fpl_cli.services.season_previews import previews_dir

CURRENT_SEASON = season_label()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Serve a fixed gameweek and team list instead of calling the FPL API."""

    async def _gameweek(explicit: int | None) -> int:
        return explicit if explicit is not None else 1

    async def _gameweek_and_teams(explicit: int | None) -> tuple[int, set[str]]:
        return await _gameweek(explicit), {"ARS", "LIV", "MCI"}

    monkeypatch.setattr(intel_mod, "_resolve_gameweek", _gameweek)
    monkeypatch.setattr(intel_mod, "_gameweek_and_teams", _gameweek_and_teams)


def write_preview(name: str, **overrides) -> None:
    target = previews_dir()
    target.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 1,
        "team": name,
        "season": CURRENT_SEASON,
        "source": "Example Weekly",
        "published": date(get_season_year(), 8, 15),
        "narrative": "Something worth knowing.",
    }
    data.update(overrides)
    (target / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def run(*args: str):
    return CliRunner().invoke(main, ["intel", *args])


def parse(result) -> dict:
    return json.loads(result.stdout)


class TestSummary:
    def test_reports_nothing_found_without_previews(self):
        result = run()
        assert result.exit_code == 0
        assert "No previews found" in result.stdout

    def test_json_envelope_shape(self):
        write_preview("ARS")
        payload = parse(run("--format", "json"))
        assert payload["command"] == "intel"
        assert isinstance(payload["data"], list)
        metadata = payload["metadata"]
        for key in (
            "gameweek", "season", "previews_dir", "coverage", "usage_policy",
            "sections_live", "sections_expired", "decay_schedule", "unknown_teams",
            "unresolved_players", "warnings",
        ):
            assert key in metadata, key

    def test_gameweek_flag_drives_decay(self):
        write_preview("ARS", players=[{"name": "Saliba", "code": 1, "injury": "Out."}])
        assert "injuries" in parse(run("--format", "json"))["metadata"]["sections_live"]
        later = parse(run("-g", "9", "--format", "json"))["metadata"]
        assert "injuries" in later["sections_expired"]
        assert later["gameweek"] == 9

    def test_coverage_policy_is_reported(self):
        write_preview("ARS")
        metadata = parse(run("--format", "json"))["metadata"]
        assert metadata["coverage"] == {"teams": 1, "of": 20, "pct": 0.05,
                                        "usable_as": "negative_filter_only"}
        assert "never to promote" in metadata["usage_policy"]

    def test_unknown_team_is_reported(self):
        write_preview("XYZ")
        metadata = parse(run("--format", "json"))["metadata"]
        assert metadata["unknown_teams"] == ["XYZ"]
        assert "XYZ" in metadata["team_set_warning"]

    def test_no_drift_warning_when_every_team_is_real(self):
        write_preview("ARS")
        assert parse(run("--format", "json"))["metadata"]["team_set_warning"] is None

    def test_drift_warning_goes_to_stderr(self):
        write_preview("XYZ")
        result = CliRunner().invoke(main, ["intel"])
        assert "XYZ" in result.stderr

    def test_unresolved_players_are_reported(self):
        write_preview("ARS", players=[{"name": "Nameless"}])
        unresolved = parse(run("--format", "json"))["metadata"]["unresolved_players"]
        assert unresolved == [{"team": "ARS", "name": "Nameless"}]

    def test_load_warnings_reach_json_metadata(self):
        previews_dir().mkdir(parents=True, exist_ok=True)
        (previews_dir() / "BAD.yaml").write_text("team: [unclosed\n", encoding="utf-8")
        assert parse(run("--format", "json"))["metadata"]["warnings"]

    def test_load_warnings_go_to_stderr_in_table_mode(self):
        previews_dir().mkdir(parents=True, exist_ok=True)
        (previews_dir() / "BAD.yaml").write_text("team: [unclosed\n", encoding="utf-8")
        result = CliRunner().invoke(main, ["intel"])
        assert "BAD" in result.stderr
        assert "BAD" not in result.stdout

    def test_show_decay_lists_the_schedule(self):
        write_preview("ARS")
        result = run("--show-decay")
        assert "Decay schedule" in result.stdout
        assert "projected_xi" in result.stdout

    def test_stub_is_labelled_rather_than_counted(self):
        write_preview("ARS", narrative=None)
        result = run()
        assert "stub, not filled in" in result.stdout
        assert "0/20" in result.stdout


class TestShow:
    def test_renders_a_team(self):
        write_preview("ARS", predicted_finish=2, players=[{"name": "Saka", "code": 1,
                                                          "status": "starter"}])
        result = run("show", "ARS")
        assert result.exit_code == 0
        assert "ARS preview" in result.stdout
        assert "Saka" in result.stdout

    def test_attribution_is_shown(self):
        write_preview("ARS", author="A. Writer")
        assert "A. Writer" in run("show", "ARS").stdout

    def test_missing_team_exits_nonzero(self):
        result = run("show", "ARS")
        assert result.exit_code == 1
        # On stderr, like every table-mode failure (#162).
        assert "No preview for" in result.stderr
        assert result.stdout == ""

    def test_missing_team_json_emits_error_envelope_on_stdout(self):
        """#141: success and failure envelopes share one stream."""
        result = CliRunner().invoke(main, ["intel", "show", "ARS", "--format", "json"])
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"].startswith("No preview for")
        assert "{" not in result.stderr

    def test_json_payload_is_decayed(self):
        write_preview("ARS", players=[{"name": "Saliba", "code": 1, "status": "starter",
                                       "injury": "Out."}])
        early = parse(run("show", "ARS", "--format", "json"))["data"]
        assert early["players"][0]["injury"] == "Out."
        late = parse(run("show", "ARS", "-g", "5", "--format", "json"))["data"]
        assert "injury" not in late["players"][0]

    def test_group_level_gameweek_flag_reaches_show(self):
        # `fpl intel -g 5 show ARS` parses -g on the group; it must drive the
        # decay rather than being silently discarded.
        write_preview("ARS", players=[{"name": "Saliba", "code": 1, "status": "starter",
                                       "injury": "Out."}])
        payload = parse(run("-g", "5", "show", "ARS", "--format", "json"))
        assert payload["metadata"]["gameweek"] == 5
        assert "injury" not in payload["data"]["players"][0]

    def test_expired_sections_are_noted_in_table_mode(self):
        write_preview("ARS")
        assert "Expired at GW9" in run("show", "ARS", "-g", "9").stdout


class TestInit:
    @pytest.fixture(autouse=True)
    def _teams(self, monkeypatch):
        class _Team:
            def __init__(self, short_name: str, name: str):
                self.short_name = short_name
                self.name = name

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def get_teams(self):
                return [_Team("ARS", "Arsenal"), _Team("LIV", "Liverpool")]

        monkeypatch.setattr("fpl_cli.api.fpl.FPLClient", _Client)

    def test_creates_one_file_per_team(self):
        result = run("init")
        assert result.exit_code == 0
        assert (previews_dir() / "ARS.yaml").exists()
        assert (previews_dir() / "LIV.yaml").exists()

    def test_stubs_do_not_count_as_coverage(self):
        run("init")
        assert parse(run("--format", "json"))["metadata"]["coverage"]["teams"] == 0

    def test_stubs_load_without_warnings(self):
        run("init")
        assert parse(run("--format", "json"))["metadata"]["warnings"] == []

    def test_existing_files_are_left_alone(self):
        write_preview("ARS", source="Mine")
        run("init")
        assert "Mine" in (previews_dir() / "ARS.yaml").read_text()

    def test_force_overwrites(self):
        write_preview("ARS", source="Mine")
        run("init", "--force")
        assert "Mine" not in (previews_dir() / "ARS.yaml").read_text()


class TestResolve:
    @pytest.fixture(autouse=True)
    def _squad(self, monkeypatch):
        class _Named:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        players = [
            _Named(code=208706, web_name="Bruno G.", first_name="Bruno",
                   second_name="Guimaraes Rodriguez Moura", team_id=1),
            _Named(code=462424, web_name="Saliba", first_name="William",
                   second_name="Saliba", team_id=1),
            _Named(code=1, web_name="Salah", first_name="Mohamed",
                   second_name="Salah", team_id=2),
        ]
        teams = [_Named(id=1, short_name="ARS"), _Named(id=2, short_name="LIV")]

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def get_players(self):
                return players

            async def get_teams(self):
                return teams

        monkeypatch.setattr("fpl_cli.api.fpl.FPLClient", _Client)

    def test_dry_run_reports_matches_without_writing(self):
        write_preview("ARS", players=[{"name": "Bruno Guimaraes"}])
        path = previews_dir() / "ARS.yaml"
        before = path.read_text()

        result = run("resolve", "ARS")
        assert result.exit_code == 0
        assert "208706" in result.stdout
        assert path.read_text() == before

    def test_write_saves_codes(self):
        write_preview("ARS", players=[{"name": "Saliba"}])
        assert run("resolve", "ARS", "--write").exit_code == 0
        assert "code: 462424" in (previews_dir() / "ARS.yaml").read_text()

    def test_only_the_named_team_squad_is_searched(self):
        write_preview("ARS", players=[{"name": "Salah"}])
        payload = parse(run("resolve", "ARS", "--format", "json"))
        assert payload["metadata"]["squad_size"] == 2
        assert payload["data"][0]["how"] == "unmatched"

    def test_json_metadata_counts(self):
        write_preview("ARS", players=[{"name": "Saliba"}, {"name": "Nobody"}])
        metadata = parse(run("resolve", "ARS", "--format", "json"))["metadata"]
        assert metadata["resolved"] == 1
        assert metadata["unresolved"] == 1
        assert metadata["written"] == 0

    def test_already_resolved_players_are_skipped(self):
        write_preview("ARS", players=[{"name": "Saliba", "code": 462424}])
        assert "already has a code" in run("resolve", "ARS").stdout

    def test_all_flag_re_resolves(self):
        write_preview("ARS", players=[{"name": "Saliba", "code": 462424}])
        payload = parse(run("resolve", "ARS", "--all", "--format", "json"))
        assert [m["name"] for m in payload["data"]] == ["Saliba"]

    def test_all_write_replaces_a_wrong_code(self):
        # The documented fix path for a hand-typed wrong code: --all re-resolves
        # it and --write must actually save the correction.
        write_preview("ARS", players=[{"name": "Saliba", "code": 999}])
        payload = parse(run("resolve", "ARS", "--all", "--write", "--format", "json"))
        assert payload["metadata"]["written"] == 1
        assert "code: 462424" in (previews_dir() / "ARS.yaml").read_text()

    def test_write_without_all_leaves_existing_codes_alone(self):
        write_preview("ARS", players=[{"name": "Saliba", "code": 999}])
        run("resolve", "ARS", "--write")
        assert "999" in (previews_dir() / "ARS.yaml").read_text()

    def test_missing_team_exits_nonzero(self):
        result = run("resolve", "ARS")
        assert result.exit_code == 1
        assert "No preview for" in result.stderr
        assert result.stdout == ""

    def test_unknown_team_code_is_reported(self):
        write_preview("XYZ", players=[{"name": "Saliba"}])
        result = run("resolve", "XYZ")
        assert result.exit_code == 1
        assert "No Premier League squad" in result.stderr
        assert result.stdout == ""


class TestSchema:
    def test_prints_the_shipped_reference(self):
        result = run("schema")
        assert result.exit_code == 0
        assert "schema_version: 1" in result.stdout
        assert "predicted_finish" in result.stdout

    def test_shipped_example_parses_and_is_skipped_by_the_loader(self):
        """The reference must be valid YAML, and must never load as real intel."""
        from fpl_cli.services.season_previews import SeasonPreviewsService, example_file

        data = yaml.safe_load(example_file().read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert {"team", "season", "source", "published"} <= set(data)

        target = previews_dir()
        target.mkdir(parents=True, exist_ok=True)
        (target / "EXAMPLE.yaml").write_text(example_file().read_text(), encoding="utf-8")
        service = SeasonPreviewsService()
        assert service.get_previews() == []
        assert service.load_warnings == []
