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


def _element(pid: int, drop: tuple[str, ...] = (), joins: bool = False) -> dict:
    element = {key: 0 for key in PLAYER_KEYS if key not in drop}
    element["id"] = pid
    if joins:
        # A player the name-join probe will actually ask about: minutes played,
        # at team `pid`, named as `_understat_players` names that team's row.
        element["web_name"] = f"Player {pid}"
        element["team"] = pid
        element["minutes"] = 900
        element["element_type"] = 3
    return element


def _bootstrap(
    finished_gws: int = 5,
    drop_player_key: tuple[str, ...] = (),
    team_names: list[str] | None = None,
    joining_players: int = 0,
) -> dict:
    names = team_names if team_names is not None else [f"Team {i:02d}" for i in range(1, 21)]
    return {
        "teams": [
            {"id": i, "name": name, "short_name": f"T{i:02d}", "code": i}
            for i, name in enumerate(names, start=1)
        ],
        "events": [{"id": i, "finished": i <= finished_gws} for i in range(1, 39)],
        "elements": [
            _element(i, drop=drop_player_key, joins=i <= joining_players)
            for i in range(1, 401)
        ],
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

# The #142 shape on the columns a match row cannot do without, so every row
# drops out of the join. Elo was this shape until #431 made a blank one
# unknown rather than fatal.
UNJOINABLE = frozenset({"gameweek", "home_team"})


def _understat_players(team_names: list[str], player_names: list[str] | None = None) -> dict:
    return {
        "players": [
            {
                "id": str(i),
                "player_name": player_names[i - 1] if player_names else f"Player {i}",
                "team_title": team,
                "time": "900",
            }
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
    understat_player_names: list[str] | None = None,
    bootstrap_team_names: list[str] | None = None,
    joining_players: int = 20,
    gw_missing: tuple[int, ...] = (),
    gw_texts: dict[tuple[int, str], str] | None = None,
    gw_404: frozenset[tuple[int, str]] = frozenset(),
    gw_errors: frozenset[tuple[int, str]] = frozenset(),
    players_text: str | None = None,
    fd_tlas: list[str] | None = None,
    draft_teams: list[dict] | None = None,
    draft_error: Exception | None = None,
) -> None:
    bootstrap = _bootstrap(
        finished_gws=finished_gws,
        drop_player_key=drop_player_key,
        team_names=bootstrap_team_names,
        joining_players=joining_players,
    )
    fpl_route = respx.get(FPL_BOOTSTRAP_URL)
    if fpl_error is not None:
        fpl_route.mock(side_effect=fpl_error)
    else:
        fpl_route.mock(return_value=Response(200, json=bootstrap))
    draft_bootstrap = bootstrap if draft_teams is None else {**bootstrap, "teams": draft_teams}
    draft_route = respx.get(DRAFT_BOOTSTRAP_URL)
    if draft_error is not None:
        draft_route.mock(side_effect=draft_error)
    else:
        draft_route.mock(return_value=Response(200, json=draft_bootstrap))

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
    # Every finished gameweek, not only the two the per-GW probe compares:
    # the Elo check scans the window scoring reads.
    for gw in range(1, finished_gws + 1):
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
    respx.get(UNDERSTAT_URL).mock(
        return_value=Response(200, json=_understat_players(teams, understat_player_names))
    )

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
        assert "20 of 20 players with minutes join by name" in flat
        assert "resolve to FPL short names through TLA_TO_FPL" in flat

    @respx.mock
    def test_bootstrap_static_is_fetched_once(self, monkeypatch):
        """The season label rides on the probe's own bootstrap (#308 review).

        `FPLClient` memoises bootstrap-static on the instance alone, so
        resolving the season through a client of its own downloaded the same
        few megabytes a second time on every run.
        """
        monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-key")
        _register_routes()
        result = _run()
        assert result.exit_code == 0
        fetches = [c for c in respx.calls if str(c.request.url) == FPL_BOOTSTRAP_URL]
        assert len(fetches) == 1

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
    def test_understat_mapped_club_with_no_rows_is_broken(self, monkeypatch):
        # The gap #229 opened: TEAM_NAME_MAP has an entry for the club, so a
        # probe that only checked the map's keys would pass it — but no row in
        # the list the scorer scans carries the mapped title, which is exactly
        # what costs that club's players their xG enrichment.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        clubs = ["Coventry City"] + [f"Team {i:02d}" for i in range(2, 21)]
        _register_routes(
            finished_gws=5,
            bootstrap_team_names=clubs,
            understat_teams=[f"Team {i:02d}" for i in range(2, 21)],
        )
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "Coventry City" in flat
        assert "TEAM_NAME_MAP" in flat

    @respx.mock
    def test_understat_mapped_club_resolves_through_its_understat_name(self, monkeypatch):
        # And the other half: the club's rows carry Understat's name for it,
        # not FPL's, so the probe must resolve it through the map rather than
        # reading the mismatch as a miss.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        clubs = ["Coventry City"] + [f"Team {i:02d}" for i in range(2, 21)]
        _register_routes(
            finished_gws=5,
            bootstrap_team_names=clubs,
            understat_teams=["Coventry"] + [f"Team {i:02d}" for i in range(2, 21)],
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "all 20 clubs resolve to an Understat team" in flat
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
    def test_unjoinable_matches_at_first_finished_gw_is_not_ok(self, monkeypatch):
        # The reported shape: GW1 complete, every column present but the
        # match keys empty, so every row drops out of the match join. Nothing
        # earlier in the season to compare against, so it reads as a backfill
        # still in progress.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=1,
            gw_texts={
                (1, "matches.csv"): _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=UNJOINABLE)
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
    def test_unjoinable_matches_are_stale_while_the_previous_gw_parses(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=5,
            gw_texts={
                (5, "matches.csv"): _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=UNJOINABLE)
            },
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "GW5: matches.csv + playermatchstats.csv parse to 0 records" in flat
        assert "self-corrects" in flat

    @respx.mock
    def test_unjoinable_matches_two_gameweeks_running_is_broken(self, monkeypatch):
        # Not a backfill in progress: the join is gone for good.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        blank = _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=UNJOINABLE)
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
                (5, "matches.csv"): _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=UNJOINABLE)
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
        blank = _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=UNJOINABLE)
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
                (5, "matches.csv"): _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=UNJOINABLE)
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
                (5, "matches.csv"): _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=UNJOINABLE),
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
        blank = _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=UNJOINABLE)
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


class TestEloGap:
    """#431: blank Elo keeps the match join, so the row has to name it.

    Before, blank Elo emptied the join and the row said only that no row
    survived. Now the records survive with a neutral opponent factor, and a
    row that read ok would hide that the opponent adjustment is gone. It is
    judged across the window scoring reads, not the newest gameweek alone.
    """

    @staticmethod
    def _blank(gws) -> dict:
        blank = _csv(MATCHES_REQUIRED_COLUMNS, rows=5, blank=BLANK_ELO)
        return {(gw, "matches.csv"): blank for gw in gws}

    @staticmethod
    def _row(result) -> dict:
        payload = json.loads(result.output)
        return next(
            c for c in payload["data"]["providers"] if c["name"] == "Core-Insights per-GW files"
        )

    @respx.mock
    def test_blank_elo_at_the_first_finished_gw_is_a_backfill(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(finished_gws=1, gw_texts=self._blank([1]))
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "opponent Elo is missing for 1 of 1 matches in GW1" in flat
        assert "count unadjusted in the opponent-adjusted xG signals" in flat
        assert "only GW1 lacks it, so this is likely a backfill in progress" in flat
        assert "parse to 0 records" not in flat
        assert "all per-GW files present" not in flat

    @respx.mock
    def test_most_of_the_window_blank_is_an_upstream_gap(self, monkeypatch):
        # The 2026-27 shape: Elo for the first gameweeks, blank since. Still
        # stale rather than broken, because nothing reads wrong: the scores
        # lose the opponent adjustment, and the Monday probe stays free to
        # fail on real drift.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(finished_gws=5, gw_texts=self._blank([3, 4, 5]))
        result = _run(["--format", "json"])
        assert result.exit_code == 0
        row = self._row(result)
        assert row["status"] == "stale"
        assert "missing for 3 of 5 matches in GW1-5, the window scoring reads" in row["detail"]
        assert "every match from GW3 lacks it" in row["detail"]
        assert "upstream gap rather than a backfill in progress" in row["detail"]

    @respx.mock
    def test_blanks_left_behind_in_earlier_gameweeks_still_count(self, monkeypatch):
        # Upstream fixed the newest gameweek and left the rest blank: the
        # runtime still scores those matches unadjusted, so it is not ok.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(finished_gws=5, gw_texts=self._blank([1, 2, 3, 4]))
        result = _run(["--format", "json"])
        assert result.exit_code == 0
        row = self._row(result)
        assert row["status"] == "stale"
        assert "missing for 4 of 5 matches" in row["detail"]
        assert "upstream gap" in row["detail"]
        assert "every match from" not in row["detail"]

    @respx.mock
    def test_a_minority_without_elo_is_a_note_on_an_ok_row(self, monkeypatch):
        # One gameweek of five: the adjustment still holds for most matches,
        # so the row stays ok and only says how many count unadjusted.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(finished_gws=5, gw_texts=self._blank([5]))
        result = _run(["--format", "json"])
        assert result.exit_code == 0
        row = self._row(result)
        assert row["status"] == "ok"
        assert "all per-GW files present" in row["detail"]
        assert "1 of 5 matches in GW1-5 carry no opponent Elo and count unadjusted" in (
            row["detail"]
        )

    @respx.mock
    def test_elo_gap_rides_along_a_lag_on_the_other_unit(self, monkeypatch):
        # The gameweek stats lagging must not hide the Elo gap beside it.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=5,
            gw_404=frozenset({(5, "player_gameweek_stats.csv")}),
            gw_texts=self._blank([3, 4, 5]),
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "player_gameweek_stats.csv not published upstream yet" in flat
        assert "opponent Elo is missing for 3 of 5 matches" in flat


class TestThinJoin:
    """A join that keeps rows, but under half of them, is not ok (#431 review).

    The runtime warns on it; a probe that only tested for zero records read
    it as healthy, the probe-and-runtime divergence #229 was about.
    """

    @staticmethod
    def _thin_stats() -> str:
        # One row joins; three name a player players.csv does not carry.
        cols = sorted(PLAYERMATCHSTATS_REQUIRED_COLUMNS)
        good = ",".join("1" for _ in cols)
        bad = ",".join("999" if col == "player_id" else "1" for col in cols)
        return "\n".join([",".join(cols), good, bad, bad, bad]) + "\n"

    @respx.mock
    def test_thin_join_at_the_newest_gameweek_is_stale(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=5, gw_texts={(5, "playermatchstats.csv"): self._thin_stats()}
        )
        result = _run()
        assert result.exit_code == 0
        flat = _flat(result)
        assert "keep only 1 of 4 player-match rows" in flat
        assert "self-corrects; broken if it persists" in flat
        assert "all per-GW files present" not in flat

    @respx.mock
    def test_thin_join_two_gameweeks_running_is_broken(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        thin = self._thin_stats()
        _register_routes(
            finished_gws=5,
            gw_texts={(5, "playermatchstats.csv"): thin, (4, "playermatchstats.csv"): thin},
        )
        result = _run()
        assert result.exit_code == 1
        flat = _flat(result)
        assert "keep only 1 of 4 player-match rows" in flat
        assert "GW4 is no better, so this is not publishing lag" in flat


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


class TestUnderstatNameJoin:
    """Resolving 20 clubs said nothing about whether their players join (#263)."""

    def _row(self, result):
        payload = json.loads(result.output)
        return next(
            c for c in payload["data"]["providers"] if c["name"] == "Understat name join"
        )

    @respx.mock
    def test_a_full_join_is_ok(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(finished_gws=5)
        result = _run(["--format", "json"])
        row = self._row(result)
        assert row["status"] == "ok"
        assert "20 of 20 players with minutes join by name" in row["detail"]

    @respx.mock
    def test_a_single_miss_stays_ok_but_names_the_player(self, monkeypatch):
        # A few percent always miss — players Understat has no row for, cameos
        # in a gameweek it has not ingested. Naming them is the point: the rate
        # is what to watch, and a human can only watch it if they can see it.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        names = [f"Player {i}" for i in range(1, 21)]
        names[7] = "Somebody Else"
        _register_routes(finished_gws=5, understat_player_names=names)
        result = _run(["--format", "json"])
        row = self._row(result)
        assert row["status"] == "ok"
        assert "19 of 20 players with minutes join by name" in row["detail"]
        assert "Player 8 (Team 08, 900m)" in row["detail"]

    @respx.mock
    def test_a_wholesale_name_break_is_broken(self, monkeypatch):
        # The regression this row exists for: every club still resolves, so
        # the club check stays green while no player joins at all.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=5,
            understat_player_names=[f"Stranger {i}" for i in range(1, 21)],
        )
        result = _run(["--format", "json"])
        payload = json.loads(result.output)
        row = self._row(result)
        assert row["status"] == "broken"
        assert "100% of players with minutes match no Understat row" in row["detail"]
        # And the club check is still green, which is exactly why this row had
        # to exist rather than being folded into that one.
        club_row = next(
            c for c in payload["data"]["providers"] if c["name"] == "Understat team map"
        )
        assert club_row["status"] == "ok"

    @respx.mock
    def test_a_wholesale_name_break_is_stale_early_season(self, monkeypatch):
        # Before the pool has settled, players whose only minutes came in a
        # gameweek Understat has not ingested legitimately miss.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=2,
            understat_player_names=[f"Stranger {i}" for i in range(1, 21)],
        )
        result = _run(["--format", "json"])
        row = self._row(result)
        assert row["status"] == "stale"
        assert "may be ingestion lag" in row["detail"]

    @respx.mock
    def test_an_unresolved_club_is_not_restated_as_name_misses(self, monkeypatch):
        # Team 20 carries no Understat rows at all. That is the club check's
        # finding; counting its players here would restate one club gap as a
        # squad's worth of name misses and bury the real ones.
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            finished_gws=5,
            understat_teams=[f"Team {i:02d}" for i in range(1, 20)],
        )
        result = _run(["--format", "json"])
        row = self._row(result)
        assert row["status"] == "ok"
        assert "19 of 19 players with minutes join by name" in row["detail"]
        assert "Player 20" not in row["detail"]

    @respx.mock
    def test_no_players_with_minutes_is_skipped(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(finished_gws=5, joining_players=0)
        result = _run(["--format", "json"])
        row = self._row(result)
        assert row["status"] == "skipped"
        assert "no players with minutes at a resolved club yet" in row["detail"]

    @respx.mock
    def test_unreachable_fpl_api_is_unchecked(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            fpl_error=httpx.ConnectError(
                "boom", request=httpx.Request("GET", FPL_BOOTSTRAP_URL)
            )
        )
        result = _run(["--format", "json"])
        row = self._row(result)
        assert row["status"] == "unchecked"
        assert "could not fetch the live" in row["detail"]


def _main_teams() -> list[dict]:
    """The club list `_bootstrap` serves: ids 1-20, each club's code its id."""
    return _bootstrap()["teams"]


def _row(result, name: str) -> dict:
    payload = json.loads(result.output)
    return next(c for c in payload["data"]["providers"] if c["name"] == name)


class TestDraftClubIds:
    """#423: draft paths translate a draft club id to the main game's by club
    `code`, so the probe checks that the two lists agree by code."""

    @respx.mock
    def test_identical_lists_are_ok(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes()
        row = _row(_run(["--format", "json"]), "Draft club ids")
        assert row["status"] == "ok"
        assert row["detail"] == "all 20 clubs share their code and id in both games"

    @respx.mock
    def test_clubs_numbered_differently_are_named_but_ok(self, monkeypatch):
        """Translated by code, so not a fault, but the change is visible."""
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        draft = _main_teams()
        draft[0]["id"], draft[1]["id"] = 2, 1
        _register_routes(draft_teams=draft)
        row = _row(_run(["--format", "json"]), "Draft club ids")
        assert row["status"] == "ok"
        assert "2 numbered differently in the two games (T01, T02)" in row["detail"]

    @respx.mock
    def test_a_draft_club_with_no_main_game_counterpart_is_broken(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        draft = _main_teams()
        draft[4]["code"] = 999
        _register_routes(draft_teams=draft)
        result = _run(["--format", "json"])
        assert result.exit_code == 1
        row = _row(result, "Draft club ids")
        assert row["status"] == "broken"
        assert "draft clubs with no main-game club of the same code: T05" in row["detail"]
        assert "main-game clubs missing from the draft list: T05" in row["detail"]

    @respx.mock
    def test_a_draft_club_without_a_code_is_broken(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        draft = _main_teams()
        del draft[0]["code"]
        _register_routes(draft_teams=draft)
        row = _row(_run(["--format", "json"]), "Draft club ids")
        assert row["status"] == "broken"
        assert "no main-game club of the same code: T01" in row["detail"]

    @respx.mock
    def test_a_main_game_club_missing_from_the_draft_list_is_named_but_ok(self, monkeypatch):
        """No draft player can be at a club the draft game does not list, so
        nothing reads it: not a fault, though worth seeing at a rollover."""
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(draft_teams=_main_teams()[:19])
        row = _row(_run(["--format", "json"]), "Draft club ids")
        assert row["status"] == "ok"
        assert "main-game clubs missing from the draft list: T20" in row["detail"]

    @respx.mock
    def test_an_empty_main_club_list_is_unchecked_not_blamed_on_the_draft(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(bootstrap_team_names=[], draft_teams=_main_teams())
        row = _row(_run(["--format", "json"]), "Draft club ids")
        assert row["status"] == "unchecked"
        assert "carried no clubs" in row["detail"]

    @respx.mock
    def test_unreachable_draft_bootstrap_still_reports_the_club_row(self, monkeypatch):
        """The row set does not depend on which API is down."""
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            draft_error=httpx.ConnectError(
                "boom", request=httpx.Request("GET", DRAFT_BOOTSTRAP_URL)
            )
        )
        row = _row(_run(["--format", "json"]), "Draft club ids")
        assert row["status"] == "unchecked"

    @respx.mock
    def test_unreachable_main_bootstrap_is_unchecked(self, monkeypatch):
        monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
        _register_routes(
            fpl_error=httpx.ConnectError(
                "boom", request=httpx.Request("GET", FPL_BOOTSTRAP_URL)
            )
        )
        row = _row(_run(["--format", "json"]), "Draft club ids")
        assert row["status"] == "unchecked"


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
