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
    season_dir,
)
from fpl_cli.api.historical import historical_season_windows
from fpl_cli.api.vaastav import (
    BASE_URL as VAASTAV_BASE,
)
from fpl_cli.api.vaastav import (
    PLAYERS_RAW_REQUIRED_COLUMNS,
)
from fpl_cli.cli import main
from fpl_cli.models.player import Player
from fpl_cli.season import core_insights_season, understat_season

FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
DRAFT_BOOTSTRAP_URL = "https://draft.premierleague.com/api/bootstrap-static"
UNDERSTAT_URL = f"https://understat.com/getLeagueData/EPL/{understat_season()}"
FD_STANDINGS_URL = "https://api.football-data.org/v4/competitions/PL/standings"

# The allocation the probes read from (#101): vaastav the two oldest seasons
# of the window, Core-Insights last season and the one in progress.
VAASTAV_SEASONS = historical_season_windows().vaastav
CI_SEASONS = historical_season_windows().core_insights
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


def _csv(
    required: frozenset[str], rows: int = 400, blank: frozenset[str] = frozenset()
) -> str:
    """A CSV carrying every required column; `blank` columns are present but empty.

    The blank set is the #142 shape: a header that satisfies every column
    check over values the parser cannot convert.
    """
    cols = sorted(required)
    row = ",".join("" if col in blank else "1" for col in cols)
    return "\n".join([",".join(cols)] + [row] * rows) + "\n"


BLANK_ELO = frozenset({"home_team_elo", "away_team_elo"})


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
    ci_overrides: dict[tuple[str, str], Response] | None = None,
    understat_teams: list[str] | None = None,
    gw_missing: tuple[int, ...] = (),
    gw_texts: dict[tuple[int, str], str] | None = None,
    gw_404: frozenset[tuple[int, str]] = frozenset(),
    gw_errors: frozenset[tuple[int, str]] = frozenset(),
    players_text: str | None = None,
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

    # Root files for every season Core-Insights serves; `ci_overrides` is
    # keyed by (season label, filename) and `players_text` applies to the
    # season in progress, the one the per-GW files join against.
    ci_responses = ci_overrides or {}
    for season in CI_SEASONS:
        directory = season_dir(season)
        players = _csv(PLAYERS_CSV_REQUIRED_COLUMNS)
        if players_text is not None and season == CI_SEASONS[-1]:
            players = players_text
        respx.get(f"{CI_BASE}/{directory}/players.csv").mock(
            return_value=ci_responses.get((season, "players.csv"), Response(200, text=players))
        )
        respx.get(f"{CI_BASE}/{directory}/playerstats.csv").mock(
            return_value=ci_responses.get(
                (season, "playerstats.csv"),
                Response(200, text=_csv(PLAYERSTATS_REQUIRED_COLUMNS)),
            )
        )
    overridden = gw_texts or {}
    for gw in {finished_gws, finished_gws - 1}:
        if gw < 1:
            continue
        tournament = f"{CI_BASE}/{CI_SEASON}/By Tournament/Premier League/GW{gw}"
        gw_files = (
            ("matches.csv", f"{tournament}/matches.csv", MATCHES_REQUIRED_COLUMNS),
            (
                "playermatchstats.csv",
                f"{tournament}/playermatchstats.csv",
                PLAYERMATCHSTATS_REQUIRED_COLUMNS,
            ),
            (
                "player_gameweek_stats.csv",
                f"{CI_BASE}/{CI_SEASON}/By Gameweek/GW{gw}/player_gameweek_stats.csv",
                GW_STATS_REQUIRED_COLUMNS,
            ),
        )
        for filename, url, columns in gw_files:
            if gw in gw_missing or (gw, filename) in gw_404:
                respx.get(url).mock(return_value=Response(404))
                continue
            if (gw, filename) in gw_errors:
                respx.get(url).mock(return_value=Response(500))
                continue
            text = overridden.get((gw, filename), _csv(columns, rows=5))
            respx.get(url).mock(return_value=Response(200, text=text))

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
        for season in CI_SEASONS:
            assert f"Core-Insights {season}" in flat
        assert "all per-GW files present, parsing to" in flat
        assert "player-match records" in flat
        assert "players resolve for the join" in flat
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
    def test_core_insights_missing_completed_season_is_broken(self, monkeypatch):
        # Core-Insights is the only source for last season (#101): a vanished
        # directory costs every profile a whole season, and it is not a
        # rollover lag the way a missing current-season directory can be.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        last_season = CI_SEASONS[0]
        _register_routes(ci_overrides={(last_season, "playerstats.csv"): Response(404)})
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert f"sole source for {last_season} player history" in flat
        assert "may not exist yet" not in flat
        # The season in progress is still probed in full.
        assert "all per-GW files present, parsing to" in flat

    @respx.mock
    def test_core_insights_missing_current_season_may_be_rollover_lag(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(ci_overrides={(CI_SEASONS[-1], "players.csv"): Response(404)})
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "sole current-season source" in flat
        assert "may not exist yet" in flat
        assert "no players.csv lookup to join against" in flat

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


class TestParseDrift:
    """#142: correct columns, values the parser yields nothing from.

    The probe re-implemented a header check while the runtime join needed
    more, so `doctor --providers` reported the per-GW files ok in the same
    session every scoring command warned they had parsed to 0 records.
    """

    @respx.mock
    def test_blank_elo_at_first_finished_gw_is_not_ok(self, monkeypatch):
        # The reported shape: GW1 complete, Elo columns present but empty, so
        # every row drops out of the match join. Nothing earlier in the season
        # to compare against, so it reads as a backfill still in progress.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=1,
            gw_texts={
                (1, "matches.csv"): _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=BLANK_ELO)
            },
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "parse to 0 records" in flat
        assert "opponent-adjusted xG signals" in flat
        assert "broken if it persists" in flat
        assert "all per-GW files present" not in flat

    @respx.mock
    def test_blank_elo_is_stale_while_the_previous_gw_parses(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=5,
            gw_texts={
                (5, "matches.csv"): _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=BLANK_ELO)
            },
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "GW5: matches.csv + playermatchstats.csv parse to 0 records" in flat
        assert "self-corrects" in flat

    @respx.mock
    def test_blank_elo_two_gameweeks_running_is_broken(self, monkeypatch):
        # Not a backfill in progress: the join is gone for good.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        blank = _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=BLANK_ELO)
        _register_routes(
            finished_gws=5,
            gw_texts={(5, "matches.csv"): blank, (4, "matches.csv"): blank},
        )
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "GW4 yields nothing either" in flat
        assert "no row survives the join" in flat
        assert "opponent-adjusted xG signals are unavailable" in flat

    @respx.mock
    def test_unparseable_gameweek_stats_is_broken(self, monkeypatch):
        # The same drift in the other per-GW parser: price-trend rows, not
        # match records, and the row must name the signals it costs.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        blank = _csv(GW_STATS_REQUIRED_COLUMNS, rows=5, blank=frozenset({"now_cost"}))
        _register_routes(
            finished_gws=5,
            gw_texts={
                (5, "player_gameweek_stats.csv"): blank,
                (4, "player_gameweek_stats.csv"): blank,
            },
        )
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "player_gameweek_stats.csv parse to 0 records" in flat
        assert "price-trend and transfer-momentum signals are unavailable" in flat

    @respx.mock
    def test_unparseable_players_csv_is_broken_and_per_gw_unchecked(self, monkeypatch):
        # players.csv resolves the id every other file joins on. When it
        # parses to nothing the per-GW records are empty too — attribute that
        # to the join table, not to the per-GW files.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            players_text=_csv(
                PLAYERS_CSV_REQUIRED_COLUMNS, blank=frozenset({"player_id"})
            )
        )
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "none parse into a player" in flat
        # The per-GW row must point at the players.csv row, not name a cause
        # of its own: an unreachable or column-drifted players.csv lands here
        # too, and neither is a parse failure.
        assert "no players.csv lookup to join against (see the players.csv row)" in flat

    @respx.mock
    def test_unit_that_changes_failure_kind_is_still_broken(self, monkeypatch):
        # 404 at GW4, published-but-unparseable at GW5: two gameweeks with no
        # match join either way. Comparing absent against absent and empty
        # against empty would read this as two unrelated one-off lags.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=5,
            gw_404=frozenset({(4, "matches.csv")}),
            gw_texts={
                (5, "matches.csv"): _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=BLANK_ELO)
            },
        )
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "GW4 yields nothing either" in flat
        assert "not publishing lag" in flat

    @respx.mock
    def test_unrelated_file_error_does_not_discard_the_diagnosis(self, monkeypatch):
        # The match join is provably dead at both gameweeks; an unrelated
        # per-GW file failing at the comparison gameweek must not turn that
        # into "could not check".
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        blank = _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=BLANK_ELO)
        _register_routes(
            finished_gws=5,
            gw_texts={(5, "matches.csv"): blank, (4, "matches.csv"): blank},
            gw_errors=frozenset({(4, "player_gameweek_stats.csv")}),
        )
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "GW4 yields nothing either" in flat
        assert "could not check" not in flat

    @respx.mock
    def test_unfetchable_comparison_gameweek_says_it_could_not_confirm(self, monkeypatch):
        # The problem unit itself is what would not fetch at GW4, so lag and
        # break really are indistinguishable — say that instead of implying a
        # comparison that never happened.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=5,
            gw_texts={
                (5, "matches.csv"): _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=BLANK_ELO)
            },
            gw_errors=frozenset({(4, "matches.csv")}),
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "GW4 could not be fetched to confirm" in flat
        assert "may already be broken" in flat
        assert "self-corrects" not in flat

    @respx.mock
    def test_broken_row_names_a_column_drift_at_the_comparison_gameweek(self, monkeypatch):
        # GW5 has every column and parses to nothing; GW4 lost the column
        # outright. The row carries that cause rather than reporting both
        # gameweeks as the same unexplained empty join.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        renamed = _csv(MATCHES_REQUIRED_COLUMNS, rows=5).replace(
            "home_team_elo", "home_elo"
        )
        _register_routes(
            finished_gws=5,
            gw_texts={
                (5, "matches.csv"): _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=BLANK_ELO),
                (4, "matches.csv"): renamed,
            },
        )
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "GW4 yields nothing either" in flat
        assert "missing column(s) home_team_elo" in flat

    @respx.mock
    def test_json_reports_the_parse_failure_as_broken(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        blank = _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=BLANK_ELO)
        _register_routes(
            finished_gws=5,
            gw_texts={(5, "matches.csv"): blank, (4, "matches.csv"): blank},
        )
        result = _run(["--format", "json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        row = next(
            c
            for c in payload["data"]["providers"]
            if c["name"] == "Core-Insights per-GW files"
        )
        assert row["status"] == "broken"


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
