"""Tests for `fpl returnees` command."""

from __future__ import annotations

import json
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.models.player import PlayerPosition, PlayerStatus
from fpl_cli.season import season_label
from fpl_cli.services import returnee_radar
from fpl_cli.services.player_prior import PlayerPrior
from tests.conftest import make_player, make_team

# The radar reads the clock for lapsing and news age, and the season for
# resolving a bare "5 Sep" into a year. Neither is a CLI flag, so the tests
# pin both by wrapping the service entry point -- everything inside it
# (quality bar, window, snapshot store) still runs for real.
SEASON_YEAR = 2026
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
NEXT_GW = 3

_FIRST_DEADLINE = datetime(2026, 8, 14, 17, 30, tzinfo=timezone.utc)


def _deadline(gw: int) -> datetime:
    return _FIRST_DEADLINE + timedelta(days=7 * (gw - 1))


GAMEWEEKS: list[dict[str, Any]] = [
    {
        "id": gw,
        "deadline_time": _deadline(gw).isoformat().replace("+00:00", "Z"),
        "is_next": gw == NEXT_GW,
    }
    for gw in range(1, 11)
]


def _returning_in_gw(gw: int) -> str:
    """News text whose stated date resolves to exactly *gw*."""
    day = _deadline(gw).date()
    return f"Hamstring injury - Expected back {day.day} {day:%b}"


_REAL_RUN_RADAR = returnee_radar.run_radar


def _pinned_run_radar(players: Any, **kwargs: Any) -> Any:
    kwargs["now"] = NOW
    kwargs["season_year"] = SEASON_YEAR
    return _REAL_RUN_RADAR(players, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _flagged(
    pid: int,
    web_name: str,
    *,
    news: str = "Knee injury - Unknown return date",
    status: PlayerStatus = PlayerStatus.INJURED,
    chance: int | None = None,
    now_cost: int = 100,
) -> Any:
    return make_player(
        id=pid,
        code=1000 + pid,
        web_name=web_name,
        team_id=1,
        position=PlayerPosition.MIDFIELDER,
        now_cost=now_cost,
        status=status,
        news=news,
        news_added=(NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        chance_of_playing_next_round=chance,
    )


def _default_players() -> list[Any]:
    """Three flagged players plus one fit one, spanning the radar's branches."""
    return [
        _flagged(1, "Sidelined", chance=25),
        _flagged(2, "Duesoon", news=_returning_in_gw(4), chance=75),
        _flagged(3, "Duelater", news=_returning_in_gw(7)),
        _flagged(4, "Journeyman", now_cost=45),
        make_player(id=5, code=1005, web_name="Fitasafiddle", team_id=1,
                    position=PlayerPosition.MIDFIELDER, status=PlayerStatus.AVAILABLE),
    ]


def _default_priors() -> dict[int, PlayerPrior]:
    """Journeyman (4) is the only flagged player below the quality bar."""
    strong = PlayerPrior(prior_strength=0.9, confidence=1.0, source="history")
    return {
        1: strong,
        2: strong,
        3: strong,
        4: PlayerPrior(prior_strength=0.1, confidence=1.0, source="history"),
        5: strong,
    }


_UNSET: Any = object()


def _scoring_data(players: list[Any] | None = None, priors: Any = _UNSET) -> Any:
    """A ScoringData stand-in. `priors=None` is the degraded-priors run."""
    data = MagicMock()
    data.players = _default_players() if players is None else players
    data.player_priors = _default_priors() if priors is _UNSET else priors
    data.teams = [make_team(id=1, name="Liverpool", short_name="LIV")]
    data.next_gw_id = NEXT_GW
    return data


def _make_client() -> Any:
    client = MagicMock()
    client.get_gameweeks = AsyncMock(return_value=GAMEWEEKS)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@asynccontextmanager
async def _fake_provider(profiles: dict[int, Any] | None = None):
    provider = MagicMock()
    provider.get_all_player_histories = AsyncMock(return_value=profiles or {})
    yield provider


def _make_understat() -> Any:
    client = MagicMock()
    client.get_league_players = AsyncMock(return_value=[])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _run(args: list[str] | None = None, *, scoring_data: Any = None,
         prepare_error: Exception | None = None) -> Any:
    """Invoke `fpl returnees` with every network seam stubbed."""
    if scoring_data is None:
        scoring_data = _scoring_data()

    prepare = AsyncMock(side_effect=prepare_error) if prepare_error is not None \
        else AsyncMock(return_value=scoring_data)

    runner = CliRunner()
    with ExitStack() as stack:
        stack.enter_context(patch("fpl_cli.api.fpl.FPLClient", return_value=_make_client()))
        stack.enter_context(patch("fpl_cli.services.scoring.prepare_scoring_data", new=prepare))
        stack.enter_context(patch(
            "fpl_cli.api.historical.make_historical_provider",
            side_effect=lambda *a, **kw: _fake_provider(),
        ))
        stack.enter_context(patch(
            "fpl_cli.api.understat.UnderstatClient",
            side_effect=lambda *a, **kw: _make_understat(),
        ))
        stack.enter_context(patch(
            "fpl_cli.services.returnee_radar.run_radar", new=_pinned_run_radar,
        ))
        return runner.invoke(
            main, ["returnees"] + (args or []), env={"COLUMNS": "220"},
        )


def _json_run(args: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Parse the envelope off stdout alone — notes belong on stderr."""
    result = _run(["--format", "json"] + (args or []), **kwargs)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _names(payload: dict[str, Any]) -> list[str]:
    return [entry["web_name"] for entry in payload["data"]["entries"]]


# ---------------------------------------------------------------------------
# Table output
# ---------------------------------------------------------------------------


class TestTableOutput:
    def test_renders_a_row_per_entry(self):
        result = _run()

        assert result.exit_code == 0, result.output
        for name in ("Sidelined", "Duesoon", "Duelater"):
            assert name in result.output
        # Team, position, expected return, chance and transition columns.
        assert "Liverpool" in result.output
        assert "MID" in result.output
        assert "GW4" in result.output
        assert "75%" in result.output
        assert "Change" in result.output

    def test_quality_bar_keeps_the_weak_player_out(self):
        result = _run()

        assert "Journeyman" not in result.output

    def test_date_unknown_entry_renders_an_explicit_unknown_marker(self):
        result = _run()

        # Sidelined has no parseable date: the cell must say so, not sit empty.
        assert "Unknown" in result.output

    def test_departures_are_reported_when_a_tracked_player_is_fit_again(self):
        returnee_radar.save_snapshot(returnee_radar.RadarSnapshot(
            season=season_label(SEASON_YEAR),
            gameweek=NEXT_GW - 1,
            players={5: returnee_radar.SnapshotRecord(status="i", web_name="Fitasafiddle")},
        ))

        result = _run()

        assert result.exit_code == 0, result.output
        assert "Fitasafiddle" in result.output


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_envelope_shape(self):
        payload = _json_run()

        assert payload["command"] == "returnees"
        assert "metadata" in payload
        assert "data" in payload
        assert payload["metadata"]["season"] == season_label()
        assert payload["metadata"]["gameweek"] == NEXT_GW

    def test_metadata_carries_window_and_availability_flags(self):
        payload = _json_run()

        metadata = payload["metadata"]
        assert metadata["window"] == 6
        assert metadata["transitions_available"] is False
        assert metadata["quality_bar_available"] is True
        assert metadata["quality_bar_applied"] is True

    def test_entries_carry_the_radar_fields(self):
        payload = _json_run()

        by_name = {e["web_name"]: e for e in payload["data"]["entries"]}
        due_soon = by_name["Duesoon"]
        assert due_soon["return_gameweek"] == 4
        assert due_soon["expected_return"] == _deadline(4).date().isoformat()
        assert due_soon["return_known"] is True
        assert due_soon["chance_of_playing"] == 75
        assert due_soon["team"] == "Liverpool"
        assert due_soon["position"] == "MID"
        assert due_soon["quality"]["basis"] == "prior"

        unknown = by_name["Sidelined"]
        assert unknown["expected_return"] is None
        assert unknown["return_known"] is False

    def test_table_and_json_are_driven_from_the_same_assembled_data(self):
        players = _default_players()
        players[0] = _flagged(1, "Renamedplayer")
        scoring_data = _scoring_data(players=players)

        table = _run(scoring_data=scoring_data)
        payload = _json_run(scoring_data=_scoring_data(players=players))

        assert "Renamedplayer" in table.output
        assert "Renamedplayer" in _names(payload)


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


class TestFlags:
    def test_window_narrows_the_result_set(self):
        default = _json_run()
        narrowed = _json_run(["--window", "2"])

        assert "Duelater" in _names(default)
        assert "Duelater" not in _names(narrowed)
        assert narrowed["metadata"]["window"] == 2

    def test_all_includes_an_entry_the_quality_bar_dropped(self):
        default = _json_run()
        everyone = _json_run(["--all"])

        assert "Journeyman" not in _names(default)
        assert "Journeyman" in _names(everyone)
        assert everyone["metadata"]["quality_bar_applied"] is False

    def test_all_does_not_persist_a_filter_bypassed_watchlist(self):
        _run(["--all"])
        bypassed = returnee_radar.snapshot_path().exists()
        _run()

        # Storing the wider list would make the next ordinary run report
        # everyone it re-excluded as having dropped off the watchlist.
        assert bypassed is False
        assert returnee_radar.snapshot_path().exists()


# ---------------------------------------------------------------------------
# Week-over-week deltas
# ---------------------------------------------------------------------------


class TestDeltas:
    def test_first_run_reports_deltas_unavailable_second_run_reports_them_present(self):
        first = _json_run()
        second = _json_run()

        assert first["metadata"]["transitions_available"] is False
        assert second["metadata"]["transitions_available"] is True

    def test_table_notes_the_first_run_has_nothing_to_diff(self):
        first = _run()
        second = _run()

        assert "first" in first.output.lower()
        assert "first" not in second.output.lower()


# ---------------------------------------------------------------------------
# Empty and degraded states
# ---------------------------------------------------------------------------


class TestEmptyAndDegraded:
    def test_no_flagged_players_prints_an_empty_state_and_exits_zero(self):
        fit_only = [make_player(id=5, code=1005, web_name="Fitasafiddle", team_id=1,
                                status=PlayerStatus.AVAILABLE)]
        scoring_data = _scoring_data(players=fit_only, priors={5: _default_priors()[5]})

        result = _run(scoring_data=scoring_data)

        assert result.exit_code == 0, result.output
        assert "No" in result.output

    def test_no_flagged_players_emits_an_empty_json_list(self):
        fit_only = [make_player(id=5, code=1005, web_name="Fitasafiddle", team_id=1,
                                status=PlayerStatus.AVAILABLE)]
        payload = _json_run(scoring_data=_scoring_data(
            players=fit_only, priors={5: _default_priors()[5]},
        ))

        assert payload["data"]["entries"] == []
        assert payload["metadata"]["quality_bar_available"] is True

    def test_degraded_priors_note_goes_to_stderr_and_exits_zero(self):
        result = _run(scoring_data=_scoring_data(priors=None))

        assert result.exit_code == 0, result.output
        assert "quality bar" in result.stderr
        assert "--all" in result.stderr

    def test_degraded_priors_sets_the_metadata_flag_false(self):
        payload = _json_run(scoring_data=_scoring_data(priors=None))

        assert payload["metadata"]["quality_bar_available"] is False
        assert payload["data"]["entries"] == []

    def test_all_still_lists_flagged_players_when_priors_are_unavailable(self):
        payload = _json_run(["--all"], scoring_data=_scoring_data(priors=None))

        assert "Sidelined" in _names(payload)
        assert payload["metadata"]["quality_bar_available"] is False


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestFailures:
    def test_upstream_failure_emits_the_json_error_envelope(self):
        result = _run(["--format", "json"], prepare_error=RuntimeError("bootstrap unreachable"))

        assert result.exit_code == 1
        error = json.loads(result.stderr)
        assert error["command"] == "returnees"
        assert "bootstrap unreachable" in error["error"]

    def test_upstream_failure_in_table_mode_exits_nonzero(self):
        result = _run(prepare_error=RuntimeError("bootstrap unreachable"))

        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    @pytest.mark.parametrize("custom_analysis", [False, True])
    def test_listed_in_the_general_section_of_help(self, custom_analysis: bool):
        runner = CliRunner()
        settings = {"fpl": {}, "custom_analysis": custom_analysis}
        with patch("fpl_cli.cli._context.load_settings", return_value=settings):
            result = runner.invoke(main, ["--help"], env={"COLUMNS": "220"})

        assert result.exit_code == 0, result.output
        general = result.output.split("General Commands:")[1].split(" Commands:")[0]
        assert "returnees" in general
