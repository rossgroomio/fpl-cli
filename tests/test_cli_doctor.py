"""Tests for `fpl doctor` command."""

import json
import os
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import yaml
from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.cli.doctor import _season_of_timestamp
from fpl_cli.paths import SHIPPED_CONFIG_DIR
from fpl_cli.season import get_season_year, season_label
from fpl_cli.services.returnee_radar import SNAPSHOT_FILENAME
from tests.conftest import make_team

CURRENT_YEAR = get_season_year()
CURRENT_SEASON = season_label()
PREVIOUS_SEASON = season_label(CURRENT_YEAR - 1)
CURRENT_TS = f"{CURRENT_YEAR}-08-01T00:00:00Z"


def _http_404() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test")
    return httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request)
    )


def _shipped_manager_teams() -> list:
    """Teams matching the shipped team_managers.yaml, so its check passes."""
    managers = yaml.safe_load(
        (SHIPPED_CONFIG_DIR / "team_managers.yaml").read_text(encoding="utf-8")
    )
    return [make_team(id=i, short_name=short) for i, short in enumerate(managers, start=1)]


def _mock_client(
    teams=None,
    manager_entry=None,
    classic_league=None,
    entry_error=None,
    league_error=None,
    teams_error=None,
):
    client = MagicMock()
    if teams_error is not None:
        client.get_teams = AsyncMock(side_effect=teams_error)
    else:
        client.get_teams = AsyncMock(
            return_value=teams if teams is not None else _shipped_manager_teams()
        )
    if entry_error is not None:
        client.get_manager_entry = AsyncMock(side_effect=entry_error)
    else:
        client.get_manager_entry = AsyncMock(
            return_value=manager_entry
            or {
                "name": "My Team",
                "player_first_name": "Ross",
                "player_last_name": "G",
                "leagues": {"classic": [{"id": 99}]},
            }
        )
    if league_error is not None:
        client.get_classic_league_standings = AsyncMock(side_effect=league_error)
    else:
        client.get_classic_league_standings = AsyncMock(
            return_value=classic_league
            or {"league": {"name": "Test League", "created": CURRENT_TS}}
        )
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _mock_draft_client(league_details=None, entry_profile=None, league_error=None, entry_error=None):
    client = MagicMock()
    if league_error is not None:
        client.get_league_details = AsyncMock(side_effect=league_error)
    else:
        client.get_league_details = AsyncMock(
            return_value=league_details
            or {"league": {"name": "Draft League", "draft_dt": CURRENT_TS}}
        )
    if entry_error is not None:
        client.get_entry_profile = AsyncMock(side_effect=entry_error)
    else:
        client.get_entry_profile = AsyncMock(
            return_value=entry_profile
            or {
                "entry": {
                    "name": "Draft Team",
                    "player_first_name": "Ross",
                    "player_last_name": "G",
                    "league_set": [4321],
                }
            }
        )
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _run(client, settings=None, draft_client=None, args=None):
    settings = settings if settings is not None else {"fpl": {}}
    runner = CliRunner()
    with ExitStack() as stack:
        stack.enter_context(patch("fpl_cli.cli.load_settings", return_value=settings))
        stack.enter_context(patch("fpl_cli.cli.doctor.load_settings", return_value=settings))
        stack.enter_context(patch("fpl_cli.api.fpl.FPLClient", return_value=client))
        if draft_client is not None:
            stack.enter_context(
                patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft_client)
            )
        return runner.invoke(main, ["doctor", *(args or [])])


def _data_dir() -> Path:
    path = Path(os.environ["FPL_CLI_DATA_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _flat(result) -> str:
    """Output with normalised whitespace, so console line-wrapping cannot split a phrase."""
    return " ".join(result.output.split())


def _write_preview(team: str, published: str | None = None) -> None:
    previews = Path(os.environ["FPL_CLI_CONFIG_DIR"]) / "previews"
    previews.mkdir(parents=True, exist_ok=True)
    (previews / f"{team}.yaml").write_text(
        f"team: {team}\nsource: Test Preview\npublished: {published or f'{CURRENT_YEAR}-08-01'}\n",
        encoding="utf-8",
    )


def _write_snapshot(season: str, gameweek: int = 5) -> None:
    (_data_dir() / SNAPSHOT_FILENAME).write_text(
        json.dumps({"metadata": {"season": season, "gameweek": gameweek}, "players": {}}),
        encoding="utf-8",
    )


class TestSeasonOfTimestamp:
    def test_parses_iso_z_string(self):
        assert _season_of_timestamp(f"{CURRENT_YEAR}-08-01T10:00:00Z") == CURRENT_SEASON

    def test_accepts_datetime(self):
        assert _season_of_timestamp(datetime(CURRENT_YEAR - 1, 9, 1)) == PREVIOUS_SEASON

    def test_rejects_garbage(self):
        assert _season_of_timestamp("not-a-date") is None
        assert _season_of_timestamp(None) is None
        assert _season_of_timestamp(123) is None


class TestEnvironmentSection:
    def test_reports_dirs_and_override_source(self):
        result = _run(_mock_client())
        assert result.exit_code == 0
        assert "FPL_CLI_CONFIG_DIR override" in result.output
        assert "FPL_CLI_DATA_DIR override" in result.output
        assert "FPL_CLI_CACHE_DIR override" in result.output

    def test_missing_settings_yaml_is_stale_with_init_hint(self):
        Path(os.environ["FPL_CLI_CONFIG_DIR"], "settings.yaml").unlink()
        result = _run(_mock_client())
        assert "shipped defaults" in result.output
        assert "fpl init" in result.output


class TestIdChecks:
    def test_unset_ids_are_skipped(self):
        result = _run(_mock_client())
        assert result.exit_code == 0
        assert result.output.count("not set") == 4

    def test_classic_entry_reports_team_and_manager(self):
        result = _run(_mock_client(), settings={"fpl": {"classic_entry_id": 123}})
        assert result.exit_code == 0
        assert "My Team" in result.output
        assert "Ross G" in result.output

    def test_dead_classic_entry_is_broken(self):
        result = _run(
            _mock_client(entry_error=_http_404()),
            settings={"fpl": {"classic_entry_id": 123}},
        )
        assert result.exit_code == 1
        assert "does not resolve" in result.output

    def test_classic_league_created_stamp_never_flags_the_league(self):
        # `created` is not a staleness signal for a classic league: the ID
        # sequence restarts each July, so the stamp is always current and an
        # odd one proves nothing. Pins that no season assertion creeps back in.
        client = _mock_client(
            classic_league={"league": {"name": "Old League", "created": "2019-08-01T00:00:00Z"}}
        )
        result = _run(client, settings={"fpl": {"classic_league_id": 99}})
        assert result.exit_code == 0
        assert "Old League" in result.output

    def test_classic_entry_in_configured_league_is_ok(self):
        result = _run(
            _mock_client(), settings={"fpl": {"classic_entry_id": 123, "classic_league_id": 99}}
        )
        assert result.exit_code == 0
        assert "in classic league 99" in _flat(result)

    def test_classic_entry_in_wrong_league_is_broken(self):
        # The classic half of issue 57: FPL reissues entry IDs each season, so
        # last season's ID resolves to a live team belonging to someone else.
        entry = {
            "name": "Someone Else",
            "player_first_name": "Other",
            "player_last_name": "Manager",
            "leagues": {"classic": [{"id": 111}]},
        }
        result = _run(
            _mock_client(manager_entry=entry),
            settings={"fpl": {"classic_entry_id": 123, "classic_league_id": 99}},
        )
        assert result.exit_code == 1
        assert "Someone Else" in result.output
        assert "reissued" in _flat(result)

    def test_classic_entry_not_condemned_when_league_is_dead(self):
        # A stale league ID makes the membership miss meaningless -- it may be
        # the league that is wrong, so the entry must not be condemned for it.
        entry = {
            "name": "My Team",
            "player_first_name": "Ross",
            "player_last_name": "G",
            "leagues": {"classic": [{"id": 111}]},
        }
        result = _run(
            _mock_client(manager_entry=entry, league_error=_http_404()),
            settings={"fpl": {"classic_entry_id": 123, "classic_league_id": 99}},
        )
        assert result.exit_code == 1  # the league row alone is broken
        assert "reissued ID" not in _flat(result)
        assert "membership not checked" in _flat(result)

    def test_classic_entry_membership_holds_when_league_check_failed(self):
        # Membership comes from the entry's own payload, so it is provable
        # even when the league lookup errored.
        request = httpx.Request("GET", "https://example.test")
        result = _run(
            _mock_client(league_error=httpx.ConnectError("boom", request=request)),
            settings={"fpl": {"classic_entry_id": 123, "classic_league_id": 99}},
        )
        assert result.exit_code == 0
        assert "in classic league 99" in _flat(result)

    def test_classic_entry_without_listed_leagues_is_not_condemned(self):
        # An entry payload carrying no classic leagues is a shape change, not
        # proof the entry left the league.
        entry = {"name": "My Team", "player_first_name": "Ross", "player_last_name": "G"}
        result = _run(
            _mock_client(manager_entry=entry),
            settings={"fpl": {"classic_entry_id": 123, "classic_league_id": 99}},
        )
        assert result.exit_code == 0
        assert "listed no classic leagues" in _flat(result)

    def test_classic_entry_without_league_id_notes_unchecked_membership(self):
        result = _run(_mock_client(), settings={"fpl": {"classic_entry_id": 123}})
        assert result.exit_code == 0
        assert "classic_league_id is not set" in _flat(result)

    def test_classic_league_current_season_is_ok(self):
        result = _run(_mock_client(), settings={"fpl": {"classic_league_id": 99}})
        assert result.exit_code == 0
        assert "Test League" in result.output

    def test_dead_draft_league_is_broken(self):
        result = _run(
            _mock_client(),
            settings={"fpl": {"draft_league_id": 598}},
            draft_client=_mock_draft_client(league_error=_http_404()),
        )
        assert result.exit_code == 1
        assert "598 does not resolve" in result.output

    def test_draft_entry_in_wrong_league_is_broken(self):
        # The issue-57 case: the recycled ID resolves fine, to a stranger's
        # team in a different league -- membership is what must fail it.
        profile = {
            "entry": {
                "name": "Someone Else",
                "player_first_name": "Other",
                "player_last_name": "Manager",
                "league_set": [111],
            }
        }
        result = _run(
            _mock_client(),
            settings={"fpl": {"draft_league_id": 4321, "draft_entry_id": 90368}},
            draft_client=_mock_draft_client(entry_profile=profile),
        )
        assert result.exit_code == 1
        assert "Someone Else" in result.output
        assert "recycled" in result.output

    def test_draft_entry_in_configured_league_is_ok(self):
        result = _run(
            _mock_client(),
            settings={"fpl": {"draft_league_id": 4321, "draft_entry_id": 90368}},
            draft_client=_mock_draft_client(),
        )
        assert result.exit_code == 0
        assert "Draft Team" in result.output

    def test_draft_entry_without_listed_leagues_is_not_condemned(self):
        # A draft entry payload carrying no leagues is a shape change, not
        # proof the entry left the league (mirrors the classic case).
        profile = {
            "entry": {
                "name": "My Team",
                "player_first_name": "Ross",
                "player_last_name": "G",
                "league_set": [],
            }
        }
        result = _run(
            _mock_client(),
            settings={"fpl": {"draft_league_id": 4321, "draft_entry_id": 90368}},
            draft_client=_mock_draft_client(entry_profile=profile),
        )
        assert result.exit_code == 0
        assert "listed no draft leagues" in _flat(result)

    def test_draft_entry_not_condemned_when_league_is_dead(self):
        # When the league ID itself is stale, the membership miss proves
        # nothing about the entry -- the recycled-ID verdict must not fire.
        profile = {
            "entry": {
                "name": "My Team",
                "player_first_name": "Ross",
                "player_last_name": "G",
                "league_set": [111],
            }
        }
        result = _run(
            _mock_client(),
            settings={"fpl": {"draft_league_id": 598, "draft_entry_id": 90368}},
            draft_client=_mock_draft_client(entry_profile=profile, league_error=_http_404()),
        )
        assert result.exit_code == 1  # the league row alone is broken
        assert "recycled" not in result.output
        assert "membership not checked" in _flat(result)
        payload_rows = _flat(result)
        assert "598 does not resolve" in payload_rows

    def test_draft_entry_membership_holds_when_league_check_failed_but_set_matches(self):
        # league_set comes from the entry itself, so membership is provable
        # even when the league lookup errored (e.g. transient HTTP failure).
        request = httpx.Request("GET", "https://example.test")
        error = httpx.ConnectError("boom", request=request)
        result = _run(
            _mock_client(),
            settings={"fpl": {"draft_league_id": 4321, "draft_entry_id": 90368}},
            draft_client=_mock_draft_client(league_error=error),
        )
        assert result.exit_code == 0
        assert "in draft league 4321" in _flat(result)

    def test_draft_entry_without_league_id_notes_unchecked_membership(self):
        result = _run(
            _mock_client(),
            settings={"fpl": {"draft_entry_id": 90368}},
            draft_client=_mock_draft_client(),
        )
        assert result.exit_code == 0
        assert "membership not checked" in _flat(result)

    def test_unreachable_api_is_unchecked_not_broken(self):
        request = httpx.Request("GET", "https://example.test")
        error = httpx.ConnectError("boom", request=request)
        client = _mock_client(teams_error=error, entry_error=error)
        result = _run(client, settings={"fpl": {"classic_entry_id": 123}})
        assert result.exit_code == 0
        assert "could not" in result.output


class TestDataFileChecks:
    def test_shipped_managers_match_live_teams(self):
        result = _run(_mock_client())
        assert "covers all 20 clubs" in result.output

    def test_manager_drift_is_broken(self):
        teams = [make_team(id=i, short_name=f"X{i:02d}") for i in range(1, 21)]
        result = _run(_mock_client(teams=teams))
        assert result.exit_code == 1
        assert "team_managers.yaml is missing" in result.output

    def test_finances_from_previous_season_is_broken(self):
        (_data_dir() / "team_finances.json").write_text(
            json.dumps({"scraped_at": f"{CURRENT_YEAR - 1}-08-15T12:00:00"}),
            encoding="utf-8",
        )
        result = _run(_mock_client())
        assert result.exit_code == 1
        assert "previous season" in result.output

    def test_finances_from_current_season_is_ok(self):
        (_data_dir() / "team_finances.json").write_text(
            json.dumps({"scraped_at": f"{CURRENT_YEAR}-08-15T12:00:00"}),
            encoding="utf-8",
        )
        result = _run(_mock_client())
        assert result.exit_code == 0
        assert f"scraped {CURRENT_YEAR}-08-15" in result.output

    def test_player_prior_wrong_season_is_stale(self):
        (_data_dir() / "player_prior.yaml").write_text(
            yaml.dump({"metadata": {"season": PREVIOUS_SEASON, "gameweek": 38}, "priors": {}}),
            encoding="utf-8",
        )
        result = _run(_mock_client())
        assert result.exit_code == 0
        assert "rebuilt automatically" in _flat(result)

    def test_returnee_snapshot_absent_is_skipped(self):
        result = _run(_mock_client())
        assert result.exit_code == 0
        assert SNAPSHOT_FILENAME in _flat(result)
        assert "fpl returnees" in _flat(result)

    def test_returnee_snapshot_wrong_season_is_stale(self):
        _write_snapshot(PREVIOUS_SEASON)
        result = _run(_mock_client())
        assert result.exit_code == 0
        assert PREVIOUS_SEASON in _flat(result)
        assert "rebuilt" in _flat(result)

    def test_returnee_snapshot_current_season_is_ok(self):
        _write_snapshot(CURRENT_SEASON, gameweek=7)
        result = _run(_mock_client())
        assert result.exit_code == 0
        assert f"season {CURRENT_SEASON}" in _flat(result)
        assert "GW7" in _flat(result)

    def test_returnee_snapshot_unreadable_is_broken(self):
        (_data_dir() / SNAPSHOT_FILENAME).write_text("{ not json", encoding="utf-8")
        result = _run(_mock_client())
        assert result.exit_code == 1
        assert "unreadable" in _flat(result)
        assert "delete the file" in _flat(result)

    def test_team_ratings_missing_is_stale(self):
        result = _run(_mock_client())
        assert "team_ratings.yaml" in result.output
        assert "fpl ratings update" in result.output

    def test_previews_absent_is_skipped(self):
        result = _run(_mock_client())
        assert result.exit_code == 0
        assert "fpl intel init" in _flat(result)

    def test_previews_for_live_club_is_ok(self):
        _write_preview("ARS")
        result = _run(_mock_client())
        assert result.exit_code == 0
        assert "1 of 20 clubs covered" in _flat(result)

    def test_previews_for_unknown_club_is_broken(self):
        # A file for a relegated or misnamed club loads and counts toward the
        # coverage gate, unlike a file the loader skips -- so it is broken.
        _write_preview("XXX")
        result = _run(_mock_client())
        assert result.exit_code == 1
        assert "covers XXX" in _flat(result)

    def test_previews_from_previous_season_are_stale(self):
        _write_preview("ARS", published=f"{CURRENT_YEAR - 1}-08-01")
        result = _run(_mock_client())
        assert result.exit_code == 0
        assert "1 file(s) skipped" in _flat(result)
        assert "fpl intel" in _flat(result)

    def test_empty_team_list_does_not_flag_previews(self):
        # An empty live team list means "cannot check", never "every preview
        # covers an unknown club".
        _write_preview("ARS")
        result = _run(_mock_client(teams=[]))
        assert result.exit_code == 0
        assert "covers ARS" not in _flat(result)
        assert "could not fetch the live team list" in _flat(result)

    def test_broken_data_dir_still_reports_instead_of_aborting(self, tmp_path, monkeypatch):
        # The command diagnosing directory misconfiguration must survive it:
        # a UserDirError from a file check becomes a broken row, keeping the
        # JSON envelope and the already-produced results intact.
        blocker = tmp_path / "blocker"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(blocker / "data"))
        from fpl_cli.paths import user_data_dir

        user_data_dir.cache_clear()
        result = _run(_mock_client(), args=["--format", "json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["metadata"]["broken"] >= 2  # the dir itself + file checks
        data_dir_row = next(c for c in payload["data"]["environment"] if c["name"] == "data dir")
        assert data_dir_row["status"] == "broken"
        ratings_row = next(
            c for c in payload["data"]["data_files"] if c["name"] == "team_ratings.yaml"
        )
        assert ratings_row["status"] == "broken"
        assert "FPL_CLI_DATA_DIR" in ratings_row["detail"]
        snapshot_row = next(
            c for c in payload["data"]["data_files"] if c["name"] == SNAPSHOT_FILENAME
        )
        assert snapshot_row["status"] == "broken"

    def test_broken_config_dir_still_reports_instead_of_aborting(self, tmp_path, monkeypatch):
        # Same guarantee for the config dir: settings become unreadable, so the
        # ID rows say so, and the directories section carries the diagnosis.
        blocker = tmp_path / "blocker"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(blocker / "config"))
        from fpl_cli.paths import user_config_dir

        user_config_dir.cache_clear()
        runner = CliRunner()
        with patch("fpl_cli.api.fpl.FPLClient", return_value=_mock_client()):
            result = runner.invoke(main, ["doctor", "--format", "json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        config_row = next(c for c in payload["data"]["environment"] if c["name"] == "config dir")
        assert config_row["status"] == "broken"
        entry_row = next(
            c for c in payload["data"]["settings_ids"] if c["name"] == "classic_entry_id"
        )
        assert "settings could not be read" in entry_row["detail"]


class TestJsonOutput:
    def test_json_envelope_and_exit_code(self):
        result = _run(
            _mock_client(entry_error=_http_404()),
            settings={"fpl": {"classic_entry_id": 123}},
            args=["--format", "json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["command"] == "doctor"
        assert payload["metadata"]["season"] == CURRENT_SEASON
        assert payload["metadata"]["broken"] == 1
        sections = payload["data"]
        assert set(sections) == {"environment", "settings_ids", "data_files"}
        entry_check = next(
            c for c in sections["settings_ids"] if c["name"] == "classic_entry_id"
        )
        assert entry_check["status"] == "broken"
        assert entry_check["fix"]

    def test_json_lists_the_returnee_snapshot_check(self):
        _write_snapshot(CURRENT_SEASON)
        result = _run(_mock_client(), args=["--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        row = next(c for c in payload["data"]["data_files"] if c["name"] == SNAPSHOT_FILENAME)
        assert row["status"] == "ok"
        assert CURRENT_SEASON in row["detail"]

    def test_json_healthy_run_exits_zero(self):
        result = _run(_mock_client(), args=["--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["metadata"]["broken"] == 0
