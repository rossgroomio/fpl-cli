"""Tests for `fpl doctor --providers` — the live provider contract probes.

Every provider endpoint is mocked at the HTTP layer with respx, so the real
clients, fetchers, and parsers run end-to-end: a probe can only pass here the
same way it passes against the live provider.
"""
from __future__ import annotations

import json

import httpx
import respx
from click.testing import CliRunner
from httpx import Response

from fpl_cli.api.core_insights import (
    BASE_URL as CI_BASE,
)
from fpl_cli.api.core_insights import (
    GW_STATS_REQUIRED_COLUMNS,
    MATCHES_REQUIRED_COLUMNS,
    PLAYERMATCHSTATS_REQUIRED_COLUMNS,
    PLAYERS_CSV_REQUIRED_COLUMNS,
    PLAYERSTATS_REQUIRED_COLUMNS,
)
from fpl_cli.api.vaastav import (
    BASE_URL as VAASTAV_BASE,
)
from fpl_cli.api.vaastav import (
    PLAYERS_RAW_REQUIRED_COLUMNS,
)
from fpl_cli.cli import main
from fpl_cli.models.player import Player
from fpl_cli.season import (
    core_insights_season,
    get_season_year,
    season_label_range,
    understat_season,
)

FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
DRAFT_BOOTSTRAP_URL = "https://draft.premierleague.com/api/bootstrap-static"
UNDERSTAT_URL = f"https://understat.com/getLeagueData/EPL/{understat_season()}"
FD_STANDINGS_URL = "https://api.football-data.org/v4/competitions/PL/standings"

VAASTAV_SEASONS = season_label_range(get_season_year() - 1, count=3)
CI_SEASON = core_insights_season()

# The JSON keys the probe asserts on a bootstrap element — same derivation as
# the probe itself, so fixtures track the model.
PLAYER_KEYS = [field.alias or name for name, field in Player.model_fields.items()]


def _element(pid: int, drop: tuple[str, ...] = ()) -> dict:
    element = {key: 0 for key in PLAYER_KEYS if key not in drop}
    element["id"] = pid
    return element


def _bootstrap(finished_gws: int = 5, drop_player_key: tuple[str, ...] = ()) -> dict:
    return {
        "teams": [
            {"id": i, "name": f"Team {i:02d}", "short_name": f"T{i:02d}", "code": i}
            for i in range(1, 21)
        ],
        "events": [{"id": i, "finished": i <= finished_gws} for i in range(1, 39)],
        "elements": [_element(i, drop=drop_player_key) for i in range(1, 401)],
    }


def _csv(required: frozenset[str], rows: int = 400) -> str:
    cols = sorted(required)
    lines = [",".join(cols)] + [",".join("1" for _ in cols)] * rows
    return "\n".join(lines) + "\n"


def _understat_players(team_names: list[str]) -> dict:
    return {
        "players": [
            {"id": str(i), "player_name": f"Player {i}", "team_title": team, "time": "900"}
            for i, team in enumerate(team_names, start=1)
        ]
    }


def _fd_standings(tlas: list[str]) -> dict:
    return {
        "standings": [
            {
                "type": "TOTAL",
                "table": [
                    {
                        "position": i,
                        "team": {"id": 100 + i, "shortName": f"Club {i}", "tla": tla},
                        "playedGames": 2,
                        "won": 1,
                        "draw": 0,
                        "lost": 1,
                        "goalDifference": 0,
                        "points": 3,
                    }
                    for i, tla in enumerate(tlas, start=1)
                ],
            }
        ]
    }


def _register_routes(
    *,
    finished_gws: int = 5,
    drop_player_key: tuple[str, ...] = (),
    fpl_error: Exception | None = None,
    vaastav_overrides: dict[str, Response] | None = None,
    understat_teams: list[str] | None = None,
    gw_missing: tuple[int, ...] = (),
    fd_tlas: list[str] | None = None,
) -> None:
    bootstrap = _bootstrap(finished_gws=finished_gws, drop_player_key=drop_player_key)
    fpl_route = respx.get(FPL_BOOTSTRAP_URL)
    if fpl_error is not None:
        fpl_route.mock(side_effect=fpl_error)
    else:
        fpl_route.mock(return_value=Response(200, json=bootstrap))
    respx.get(DRAFT_BOOTSTRAP_URL).mock(return_value=Response(200, json=bootstrap))

    overrides = vaastav_overrides or {}
    for season in VAASTAV_SEASONS:
        response = overrides.get(
            season, Response(200, text=_csv(PLAYERS_RAW_REQUIRED_COLUMNS))
        )
        respx.get(f"{VAASTAV_BASE}/{season}/players_raw.csv").mock(return_value=response)

    respx.get(f"{CI_BASE}/{CI_SEASON}/players.csv").mock(
        return_value=Response(200, text=_csv(PLAYERS_CSV_REQUIRED_COLUMNS))
    )
    respx.get(f"{CI_BASE}/{CI_SEASON}/playerstats.csv").mock(
        return_value=Response(200, text=_csv(PLAYERSTATS_REQUIRED_COLUMNS))
    )
    for gw in {finished_gws, finished_gws - 1}:
        if gw < 1:
            continue
        tournament = f"{CI_BASE}/{CI_SEASON}/By Tournament/Premier League/GW{gw}"
        gw_files = (
            (f"{tournament}/matches.csv", MATCHES_REQUIRED_COLUMNS),
            (f"{tournament}/playermatchstats.csv", PLAYERMATCHSTATS_REQUIRED_COLUMNS),
            (
                f"{CI_BASE}/{CI_SEASON}/By Gameweek/GW{gw}/player_gameweek_stats.csv",
                GW_STATS_REQUIRED_COLUMNS,
            ),
        )
        for url, columns in gw_files:
            if gw in gw_missing:
                respx.get(url).mock(return_value=Response(404))
            else:
                respx.get(url).mock(return_value=Response(200, text=_csv(columns, rows=5)))

    teams = understat_teams if understat_teams is not None else [
        f"Team {i:02d}" for i in range(1, 21)
    ]
    respx.get(UNDERSTAT_URL).mock(return_value=Response(200, json=_understat_players(teams)))

    tlas = fd_tlas if fd_tlas is not None else [f"T{i:02d}" for i in range(1, 21)]
    respx.get(FD_STANDINGS_URL).mock(return_value=Response(200, json=_fd_standings(tlas)))


def _run(args: list[str] | None = None):
    runner = CliRunner()
    return runner.invoke(main, ["doctor", "--providers", *(args or [])])


def _flat(result) -> str:
    """Output with normalised whitespace, so console line-wrapping cannot split a phrase."""
    return " ".join(result.output.split())


class TestHealthyProviders:
    @respx.mock
    def test_all_probes_pass(self, monkeypatch):
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")
        _register_routes()
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "20 teams, 400 players, 38 gameweeks" in flat
        assert "player fields present" in flat
        for season in VAASTAV_SEASONS:
            assert f"vaastav {season}" in flat
        assert "all per-GW files present" in flat
        assert "resolve to an Understat team" in flat
        assert "resolve to FPL short names through TLA_TO_FPL" in flat

    @respx.mock
    def test_football_data_unconfigured_is_skipped(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes()
        result = _run()
        assert result.exit_code == 0
        assert "FOOTBALL_DATA_API_KEY not set" in _flat(result)


class TestShapeDrift:
    @respx.mock
    def test_missing_player_field_is_broken(self, monkeypatch):
        # The silent-default trap: a renamed bootstrap key validates cleanly
        # and zeroes the stat for every player, so presence is the contract.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(drop_player_key=("expected_goals",))
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "expected_goals" in flat
        assert "silently read as 0" in flat

    @respx.mock
    def test_vaastav_missing_season_is_broken(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(vaastav_overrides={VAASTAV_SEASONS[0]: Response(404)})
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert f"vaastav {VAASTAV_SEASONS[0]}" in flat
        assert "missing upstream" in flat

    @respx.mock
    def test_vaastav_renamed_column_is_broken(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        drifted = _csv(PLAYERS_RAW_REQUIRED_COLUMNS).replace("now_cost", "cost_now")
        _register_routes(
            vaastav_overrides={VAASTAV_SEASONS[1]: Response(200, text=drifted)}
        )
        result = _run()
        assert result.exit_code == 1
        assert "missing column(s) now_cost" in _flat(result)

    @respx.mock
    def test_understat_unresolved_club_is_broken_once_settled(self, monkeypatch):
        # Team 20 exists in the bootstrap but nothing maps to it in
        # Understat's own data — the #94 shape, past the early-season window.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=5,
            understat_teams=[f"Team {i:02d}" for i in range(1, 20)],
        )
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "Team 20" in flat
        assert "TEAM_NAME_MAP" in flat

    @respx.mock
    def test_understat_comma_joined_title_resolves_both_clubs(self, monkeypatch):
        # A transferred player carries both clubs in one title (#94), padded
        # here to pin the trimming: neither club may read as unresolved.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=5,
            understat_teams=[f"Team {i:02d}" for i in range(1, 19)] + ["Team 19, Team 20"],
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "19 players across 20 teams" in flat
        assert "resolve to an Understat team" in flat
        assert "TEAM_NAME_MAP" not in flat

    @respx.mock
    def test_understat_unresolved_club_is_stale_early_season(self, monkeypatch):
        # Understat only lists a club once it has ingested a match for it, so
        # in the first gameweeks an unresolved name may be lag, not drift.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=2,
            understat_teams=[f"Team {i:02d}" for i in range(1, 20)],
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "Team 20" in flat
        assert "may be ingestion lag" in flat

    @respx.mock
    def test_football_data_tla_mismatch_is_broken(self, monkeypatch):
        # The #110 shape: 20 rows in, 20 out, and the break only visible as a
        # set mismatch between mapped TLAs and the live short names.
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")
        _register_routes(fd_tlas=[f"T{i:02d}" for i in range(1, 20)] + ["XXX"])
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "TLA_TO_FPL is missing T20" in flat
        assert "still maps XXX" in flat
        assert "re-rated as promoted" in flat


class TestLagAndUnreachability:
    @respx.mock
    def test_ci_gw_publishing_lag_is_stale(self, monkeypatch):
        # Newest finished GW absent, previous present: a publishing lag that
        # self-corrects, not a layout change.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(finished_gws=5, gw_missing=(5,))
        result = _run()
        assert result.exit_code == 0
        assert "not published upstream yet" in _flat(result)

    @respx.mock
    def test_ci_gw_missing_two_gameweeks_is_broken(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(finished_gws=5, gw_missing=(5, 4))
        result = _run()
        assert result.exit_code == 1
        assert "folder layout may have changed" in _flat(result)

    @respx.mock
    def test_empty_understat_with_unreachable_fpl_is_unchecked(self, monkeypatch):
        # finished_gws defaults to 0 when the bootstrap was unreachable, which
        # must not read as "season not started" — that would classify a
        # genuinely drifted empty Understat response as skipped and exit 0.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            fpl_error=httpx.ConnectError(
                "boom", request=httpx.Request("GET", FPL_BOOTSTRAP_URL)
            ),
            understat_teams=[],
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "could not determine whether the season has started" in flat
        assert "Understat publishes once matches are played" not in flat

    @respx.mock
    def test_unreachable_fpl_api_is_unchecked_not_broken(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            fpl_error=httpx.ConnectError(
                "boom", request=httpx.Request("GET", FPL_BOOTSTRAP_URL)
            )
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "could not reach the provider" in flat
        # The cross-source checks depend on the live team list and must
        # report unchecked rather than guessing.
        assert "could not fetch the live team list" in flat


class TestJsonOutput:
    @respx.mock
    def test_json_envelope_and_counts(self, monkeypatch):
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")
        _register_routes(drop_player_key=("expected_goals",))
        result = _run(["--format", "json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["command"] == "doctor"
        assert set(payload["data"]) == {"providers"}
        assert payload["metadata"]["broken"] == 1
        fields_row = next(
            c for c in payload["data"]["providers"] if c["name"] == "FPL player fields"
        )
        assert fields_row["status"] == "broken"

    @respx.mock
    def test_json_healthy_run_exits_zero(self, monkeypatch):
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")
        _register_routes()
        result = _run(["--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["metadata"]["broken"] == 0
        statuses = {c["status"] for c in payload["data"]["providers"]}
        assert statuses == {"ok"}
