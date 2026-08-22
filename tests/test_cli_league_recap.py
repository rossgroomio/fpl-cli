"""Tests for league-recap history capture: row building, wiring, and warnings."""
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.cli._league_recap_history import (
    HISTORY_WARNING_STANDINGS_TRUNCATED,
    HISTORY_WARNING_STORE_UNREADABLE,
    HISTORY_WARNING_TRANSFER_DETAIL_SHORT,
    HISTORY_WARNING_UNMATCHED_PLAYERS,
    build_history_rows,
    capture_recap_history,
)
from fpl_cli.cli._league_recap_types import (
    LeagueRecapData,
    RecapAwards,
    RecapDraftTransaction,
    RecapManagerEntry,
    RecapManagerPlayer,
    RecapStandingsEntry,
    RecapTransfer,
)
from fpl_cli.models.league_history import CaptureStatus, FidelityTier
from fpl_cli.services.league_history import LeagueHistoryStore

SEASON = "2026-27"
CAPTURED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _player(
    name: str = "Salah",
    code: int | None = 118_748,
    points: int = 12,
    **kwargs: Any,
) -> RecapManagerPlayer:
    return RecapManagerPlayer(
        name=name,
        team=kwargs.get("team", "LIV"),
        position=kwargs.get("position", "MID"),
        code=code,
        points=points,
        is_captain=kwargs.get("is_captain", False),
        is_vice_captain=kwargs.get("is_vice_captain", False),
        contributed=kwargs.get("contributed", True),
        is_bench_boost_player=kwargs.get("is_bench_boost_player", False),
        auto_sub_in=kwargs.get("auto_sub_in", False),
        auto_sub_out=kwargs.get("auto_sub_out", False),
        red_cards=kwargs.get("red_cards", 0),
        unmatched=kwargs.get("unmatched", False),
        had_fixture=kwargs.get("had_fixture", True),
    )


def _manager(
    name: str = "Alice",
    entry_id: int = 1,
    gross_points: int = 60,
    **kwargs: Any,
) -> RecapManagerEntry:
    entry = RecapManagerEntry(
        manager_name=name,
        entry_id=entry_id,
        gw_points=kwargs.get("gw_points", gross_points),
        gross_points=gross_points,
        total_points=kwargs.get("total_points", 300),
        gw_rank=kwargs.get("gw_rank", 1),
        overall_rank=kwargs.get("overall_rank", 1),
        previous_rank=kwargs.get("previous_rank", 2),
        captain=kwargs.get("captain", "Salah"),
        captain_points=kwargs.get("captain_points", 12),
        captain_played=kwargs.get("captain_played", True),
        vice_captain=kwargs.get("vice_captain", "Saka"),
        vice_captain_points=kwargs.get("vice_captain_points", 6),
        active_chip=kwargs.get("active_chip"),
        squad=kwargs.get("squad", [_player(is_captain=True), _player(name="Saka", code=223_340, points=6, is_vice_captain=True)]),
        bench_points=kwargs.get("bench_points", 4),
        transfer_cost=kwargs.get("transfer_cost", 0),
        auto_subs=[],
    )
    for key in (
        "transfers", "transactions", "league_entry_id",
        "squad_value", "bank", "global_rank", "transfers_made",
    ):
        if key in kwargs:
            entry[key] = kwargs[key]  # type: ignore[literal-required]
    if kwargs.get("total_points") is None:
        del entry["total_points"]
    return entry


def _cohort(*entries: tuple[int, str, int | None, int, int]) -> list[RecapStandingsEntry]:
    return [
        RecapStandingsEntry(
            manager_key=key, manager_name=name, entry_id=entry_id,
            gw_points=gw_points, total_points=total,
        )
        for key, name, entry_id, gw_points, total in entries
    ]


def _recap_data(
    managers: list[RecapManagerEntry] | None = None,
    cohort: list[RecapStandingsEntry] | None = None,
    *,
    gameweek: int = 5,
    fpl_format: str = "classic",
    league_id: int = 42,
    **kwargs: Any,
) -> LeagueRecapData:
    if managers is None:
        managers = [_manager()]
    if cohort is None:
        cohort = _cohort((1, "Alice", 1, 60, 300))
    data = LeagueRecapData(
        gameweek=gameweek,
        league_name="Test League",
        fpl_format=fpl_format,
        managers=managers,
        awards=RecapAwards(),
        league_id=league_id,
        standings_cohort=cohort,
        standings_truncated=kwargs.get("standings_truncated", False),
        is_bgw=kwargs.get("is_bgw", False),
        is_dgw=kwargs.get("is_dgw", False),
    )
    for key in ("fines", "league_size", "league_start_event"):
        if key in kwargs:
            data[key] = kwargs[key]  # type: ignore[literal-required]
    return data


def _store(fpl_format: str = "classic", league_id: int = 42) -> LeagueHistoryStore:
    return LeagueHistoryStore(SEASON, fpl_format, league_id)  # type: ignore[arg-type]


def _stderr(capsys: pytest.CaptureFixture[str]) -> str:
    """Captured stderr with rich's soft wrapping undone.

    A long path is broken mid-word at the console width, so a containment
    assertion has to see the unwrapped text.
    """
    return capsys.readouterr().err.replace("\n", "")


# ---------------------------------------------------------------------------
# U6: row building
# ---------------------------------------------------------------------------


class TestBuildHistoryRows:
    def test_every_cohort_member_gets_a_row(self):
        data = _recap_data(
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280)),
        )
        rows = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)
        assert sorted(r.manager_key for r in rows) == [1, 2]

    def test_ae7_a_manager_who_could_not_be_fetched_is_recorded_unknown(self):
        data = _recap_data(
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280)),
        )
        by_key = {r.manager_key: r for r in build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)}
        assert by_key[1].capture_status is CaptureStatus.OK
        assert by_key[2].capture_status is CaptureStatus.UNKNOWN
        assert by_key[2].manager_name == "Bob"
        assert by_key[2].captain is None
        assert by_key[2].squad == []

    def test_an_unknown_row_on_a_replay_records_no_standings_numbers(self):
        """Standings describe the current gameweek, not the replayed one."""
        data = _recap_data(managers=[], cohort=_cohort((2, "Bob", 2, 40, 280)))
        rows = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT, is_live_gw=False)
        assert rows[0].gross_points is None
        assert rows[0].total_points is None

    def test_an_unknown_row_on_a_live_capture_keeps_what_standings_knew(self):
        data = _recap_data(managers=[], cohort=_cohort((2, "Bob", 2, 40, 280)))
        rows = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT, is_live_gw=True)
        assert rows[0].gross_points == 40
        assert rows[0].total_points == 280

    def test_the_row_carries_the_key_tuple_from_the_collected_data(self):
        data = _recap_data(gameweek=7, league_id=99)
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert (row.season, row.fpl_format, row.league_id, row.gameweek) == (SEASON, "classic", 99, 7)

    def test_captain_and_vice_carry_codes_and_the_fixture_flag(self):
        squad = [
            _player(name="Salah", code=118_748, points=2, is_captain=True, had_fixture=False),
            _player(name="Saka", code=223_340, points=6, is_vice_captain=True),
        ]
        data = _recap_data(managers=[_manager(squad=squad, captain="Salah", captain_points=2)])
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert row.captain is not None
        assert row.captain.code == 118_748
        assert row.captain.points == 2
        assert row.captain.had_fixture is False
        assert row.vice_captain is not None
        assert row.vice_captain.code == 223_340

    def test_the_raw_chip_name_is_stored_not_the_display_abbreviation(self):
        data = _recap_data(managers=[_manager(active_chip="BB")])
        assert build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0].active_chip == "bboost"

    def test_no_chip_stays_none(self):
        assert build_history_rows(_recap_data(), season=SEASON, captured_at=CAPTURED_AT)[0].active_chip is None

    def test_the_gameweek_shape_is_recorded_on_every_row(self):
        data = _recap_data(
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280)),
            is_bgw=True, is_dgw=False,
        )
        rows = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)
        assert all(r.gameweek_blank is True and r.gameweek_double is False for r in rows)

    def test_fines_reach_the_row_of_the_manager_they_were_ruled_against(self):
        data = _recap_data(
            managers=[_manager(name="Same", entry_id=1), _manager(name="Same", entry_id=2, gw_rank=2)],
            cohort=_cohort((1, "Same", 1, 60, 300), (2, "Same", 2, 40, 280)),
            fines=[{"manager_name": "Same", "manager_key": 2, "rule_type": "last-place", "message": "Pint"}],
        )
        by_key = {r.manager_key: r for r in build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)}
        assert by_key[1].fines == []
        assert [f.rule_type for f in by_key[2].fines] == ["last-place"]

    def test_a_fine_with_no_key_is_matched_by_name_as_a_last_resort(self):
        data = _recap_data(
            fines=[{"manager_name": "Alice", "rule_type": "last-place", "message": "Pint"}],
        )
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert [f.rule_type for f in row.fines] == ["last-place"]

    def test_the_classic_only_fields_are_carried_through(self):
        data = _recap_data(managers=[_manager(
            squad_value=1013, bank=7, global_rank=400_000, transfers_made=1,
            transfers=[RecapTransfer(
                player_in="In", player_in_team="ARS", player_in_points=8,
                player_out="Out", player_out_team="LIV", player_out_points=2,
                net=6, cost=0,
            )],
        )])
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert (row.squad_value, row.bank, row.global_rank, row.transfers_made) == (1013, 7, 400_000, 1)
        assert row.transfer_detail_shortfall == 0
        assert [t.player_in for t in row.transfers] == ["In"]

    def test_a_short_transfer_list_records_the_shortfall(self):
        data = _recap_data(managers=[_manager(
            transfers_made=3,
            transfers=[RecapTransfer(
                player_in="In", player_in_team="ARS", player_in_points=8,
                player_out="Out", player_out_team="LIV", player_out_points=2,
                net=6, cost=0,
            )],
        )])
        assert build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0].transfer_detail_shortfall == 2

    def test_a_manager_who_made_no_transfers_has_no_shortfall(self):
        data = _recap_data(managers=[_manager(transfers_made=0, transfers=[])])
        assert build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0].transfer_detail_shortfall == 0

    def test_a_draft_row_omits_the_four_classic_only_fields(self):
        data = _recap_data(
            fpl_format="draft",
            managers=[_manager(name="Alice", entry_id=1, league_entry_id=10, transactions=[
                RecapDraftTransaction(
                    player_in="In", player_in_team="ARS", player_in_points=8,
                    player_out="Out", player_out_team="LIV", player_out_points=2,
                    net=6, kind="w",
                ),
            ])],
            cohort=_cohort((10, "Alice", 1, 60, 300)),
        )
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert row.manager_key == 10
        assert row.entry_id == 1
        assert (row.squad_value, row.bank, row.global_rank, row.transfers_made) == (None, None, None, None)
        assert row.transfer_detail_shortfall is None
        assert [t.player_in for t in row.transactions] == ["In"]

    def test_rows_default_to_the_detailed_tier(self):
        assert build_history_rows(_recap_data(), season=SEASON, captured_at=CAPTURED_AT)[0].tier is FidelityTier.DETAILED


# ---------------------------------------------------------------------------
# U6: capture
# ---------------------------------------------------------------------------


class TestCaptureRecapHistory:
    async def test_ae7_the_gameweek_file_holds_a_row_for_every_cohort_member(self):
        data = _recap_data(
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280)),
        )
        result = await capture_recap_history(data, season=SEASON)

        assert result.store_readable is True
        rows = _store().load_gameweek(5)
        assert sorted(r.manager_key for r in rows) == [1, 2]
        assert {r.manager_key: r.capture_status for r in rows}[2] is CaptureStatus.UNKNOWN

    async def test_a_second_run_over_the_same_gameweek_leaves_the_file_byte_identical(self):
        data = _recap_data()
        await capture_recap_history(data, season=SEASON)
        before = _store().gameweek_file(5).read_bytes()

        result = await capture_recap_history(data, season=SEASON)

        assert result.written == []
        assert _store().gameweek_file(5).read_bytes() == before

    async def test_ae4_a_corrupt_store_warns_once_and_keeps_the_rows(self, capsys):
        path = _store().gameweek_file(5)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{\n", encoding="utf-8")
        before = path.read_bytes()

        result = await capture_recap_history(_recap_data(), season=SEASON)

        assert result.store_readable is False
        assert len(result.rows) == 1
        assert [w["code"] for w in result.warnings] == [HISTORY_WARNING_STORE_UNREADABLE]
        err = _stderr(capsys)
        assert str(path) in err
        assert path.read_bytes() == before

    async def test_the_store_path_is_announced_only_when_a_season_is_first_created(self, capsys):
        await capture_recap_history(_recap_data(), season=SEASON)
        first = _stderr(capsys)
        assert str(_store().partition_dir()) in first

        await capture_recap_history(_recap_data(gameweek=6), season=SEASON)
        assert str(_store().partition_dir()) not in _stderr(capsys)

    async def test_a_draft_capture_with_unmatched_players_warns_naming_the_count(self, capsys):
        data = _recap_data(
            fpl_format="draft",
            managers=[_manager(
                name="Alice", entry_id=1, league_entry_id=10,
                squad=[_player(name="Ghost", code=None, unmatched=True), _player()],
            )],
            cohort=_cohort((10, "Alice", 1, 60, 300)),
        )
        result = await capture_recap_history(data, season=SEASON)

        codes = [w["code"] for w in result.warnings]
        assert HISTORY_WARNING_UNMATCHED_PLAYERS in codes
        assert "Ghost" in _stderr(capsys)

    async def test_a_clean_draft_capture_is_silent_about_matching(self, capsys):
        data = _recap_data(
            fpl_format="draft",
            managers=[_manager(name="Alice", entry_id=1, league_entry_id=10)],
            cohort=_cohort((10, "Alice", 1, 60, 300)),
        )
        result = await capture_recap_history(data, season=SEASON)

        assert HISTORY_WARNING_UNMATCHED_PLAYERS not in [w["code"] for w in result.warnings]
        assert "unmatched" not in _stderr(capsys).lower()

    async def test_a_short_transfer_list_warns_naming_both_counts(self, capsys):
        data = _recap_data(managers=[_manager(transfers_made=3, transfers=[])])
        result = await capture_recap_history(data, season=SEASON)

        assert HISTORY_WARNING_TRANSFER_DETAIL_SHORT in [w["code"] for w in result.warnings]
        err = _stderr(capsys)
        assert "Alice: 3 made, 0 captured" in err

    async def test_a_truncated_standings_response_warns_naming_the_shortfall(self, capsys):
        data = _recap_data(standings_truncated=True, league_size=60)
        result = await capture_recap_history(data, season=SEASON)

        assert HISTORY_WARNING_STANDINGS_TRUNCATED in [w["code"] for w in result.warnings]
        err = _stderr(capsys)
        assert "1 of 60 members" in err

    async def test_a_complete_league_does_not_warn_about_truncation(self, capsys):
        result = await capture_recap_history(_recap_data(), season=SEASON)
        assert HISTORY_WARNING_STANDINGS_TRUNCATED not in [w["code"] for w in result.warnings]
        assert "truncat" not in _stderr(capsys).lower()

    async def test_membership_churn_neither_backfills_nor_deletes(self):
        """A departed manager keeps their row; a joiner starts where they joined."""
        gw5 = _recap_data(
            gameweek=5,
            managers=[_manager(name="Alice", entry_id=1), _manager(name="Bob", entry_id=2, gw_rank=2)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280)),
        )
        gw6 = _recap_data(
            gameweek=6,
            managers=[_manager(name="Alice", entry_id=1), _manager(name="Cara", entry_id=3, gw_rank=2)],
            cohort=_cohort((1, "Alice", 1, 60, 360), (3, "Cara", 3, 50, 250)),
        )
        await capture_recap_history(gw5, season=SEASON)
        await capture_recap_history(gw6, season=SEASON)

        store = _store()
        assert sorted(store.resolved_gameweek(5)) == [1, 2]
        assert sorted(store.resolved_gameweek(6)) == [1, 3]

    async def test_a_draft_replay_sums_its_cumulative_total_from_captured_gameweeks(self):
        for gw, points in ((1, 40), (2, 45)):
            await capture_recap_history(
                _recap_data(
                    gameweek=gw, fpl_format="draft",
                    managers=[_manager(name="Alice", entry_id=1, league_entry_id=10, gross_points=points)],
                    cohort=_cohort((10, "Alice", 1, points, 0)),
                ),
                season=SEASON,
            )
        replay = _recap_data(
            gameweek=3, fpl_format="draft",
            managers=[_manager(
                name="Alice", entry_id=1, league_entry_id=10, gross_points=50, total_points=None,
            )],
            cohort=_cohort((10, "Alice", 1, 50, 0)),
        )
        result = await capture_recap_history(replay, season=SEASON, is_live_gw=False)
        assert result.rows[0].total_points == 135

    async def test_a_draft_replay_with_a_hole_leaves_the_total_unset(self):
        await capture_recap_history(
            _recap_data(
                gameweek=1, fpl_format="draft",
                managers=[_manager(name="Alice", entry_id=1, league_entry_id=10, gross_points=40)],
                cohort=_cohort((10, "Alice", 1, 40, 0)),
            ),
            season=SEASON,
        )
        replay = _recap_data(
            gameweek=3, fpl_format="draft",
            managers=[_manager(
                name="Alice", entry_id=1, league_entry_id=10, gross_points=50, total_points=None,
            )],
            cohort=_cohort((10, "Alice", 1, 50, 0)),
        )
        result = await capture_recap_history(replay, season=SEASON, is_live_gw=False)
        assert result.rows[0].total_points is None

    async def test_a_live_draft_capture_keeps_the_standings_total(self):
        data = _recap_data(
            fpl_format="draft", gameweek=3,
            managers=[_manager(name="Alice", entry_id=1, league_entry_id=10, total_points=500)],
            cohort=_cohort((10, "Alice", 1, 60, 500)),
        )
        result = await capture_recap_history(data, season=SEASON, is_live_gw=True)
        assert result.rows[0].total_points == 500

    async def test_capture_without_a_league_id_is_skipped_rather_than_misfiled(self):
        data = _recap_data()
        del data["league_id"]
        result = await capture_recap_history(data, season=SEASON)
        assert result.store_readable is False
        assert result.written == []


# ---------------------------------------------------------------------------
# U6: CLI wiring
# ---------------------------------------------------------------------------


def _fpl_client() -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_players = AsyncMock(return_value=[])
    client.get_teams = AsyncMock(return_value=[])
    client.get_gameweek_live = AsyncMock(return_value={"elements": []})
    client.get_fixtures = AsyncMock(return_value=[])
    client.get_gameweeks = AsyncMock(return_value=[{"id": 5, "finished": True}])
    return client


def _invoke_recap(collected: LeagueRecapData, args: list[str] | None = None):
    client = _fpl_client()
    with (
        patch("fpl_cli.cli.league_recap.load_settings", return_value={"fpl": {"classic_league_id": 42}}),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.cli.review._review_resolve_gw", AsyncMock(return_value={"gw": 5})),
        patch(
            "fpl_cli.cli._league_recap_data.collect_classic_recap_data",
            AsyncMock(return_value=collected),
        ),
    ):
        from fpl_cli.cli.league_recap import league_recap_command

        return CliRunner().invoke(league_recap_command, args or [])


class TestLeagueRecapCapturesOnEveryRun:
    def test_a_plain_run_captures_the_gameweek_and_exits_zero(self):
        result = _invoke_recap(_recap_data())
        assert result.exit_code == 0, result.output
        assert _store().resolved_gameweek(5)[1].gross_points == 60

    def test_a_dry_run_captures_too(self):
        result = _invoke_recap(_recap_data(), ["--dry-run"])
        assert result.exit_code == 0, result.output
        assert _store().captured_gameweeks() == [5]

    def test_ae4_a_corrupt_store_still_renders_the_recap_and_exits_zero(self):
        path = _store().gameweek_file(5)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{\n", encoding="utf-8")
        before = path.read_bytes()

        result = _invoke_recap(_recap_data())

        assert result.exit_code == 0, result.output
        assert "Test League" in result.output
        assert path.read_bytes() == before

    def test_capture_happens_before_synthesis_so_the_prompt_can_see_it(self):
        seen: dict[str, bool] = {}

        async def _spy(*_args: Any, **_kwargs: Any) -> None:
            seen["captured"] = _store().gameweek_file(5).is_file()

        with patch("fpl_cli.cli.league_recap._recap_llm_summarise", AsyncMock(side_effect=_spy)):
            result = _invoke_recap(_recap_data(), ["--dry-run"])

        assert result.exit_code == 0, result.output
        assert seen == {"captured": True}


@pytest.mark.parametrize("fpl_format", ["classic", "draft"])
def test_the_row_shape_survives_a_store_round_trip(fpl_format: str):
    """The stored row and the in-memory row are the same object (R2, one schema)."""
    managers = [_manager(name="Alice", entry_id=1)]
    cohort = _cohort((1, "Alice", 1, 60, 300))
    if fpl_format == "draft":
        managers[0]["league_entry_id"] = 10
        cohort = _cohort((10, "Alice", 1, 60, 300))
    data = _recap_data(managers=managers, cohort=cohort, fpl_format=fpl_format)

    rows = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)
    store = _store(fpl_format)
    store.append_rows(5, rows)

    assert store.load_gameweek(5) == rows


# ---------------------------------------------------------------------------
# U7: two-tier backfill and coverage
# ---------------------------------------------------------------------------


class _HistoryClient:
    """Fake exposing only `get_manager_history`, the coarse tier's whole surface."""

    def __init__(self, rows_by_entry: dict[int, list[dict[str, Any]]], fail: set[int] | None = None):
        self._rows_by_entry = rows_by_entry
        self._fail = fail or set()
        self.calls: list[int] = []
        self.live = 0
        self.peak = 0

    async def get_manager_history(self, entry_id: int) -> dict[str, Any]:
        self.calls.append(entry_id)
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            if entry_id in self._fail:
                raise RuntimeError("boom")
            return {"current": self._rows_by_entry.get(entry_id, [])}
        finally:
            self.live -= 1


def _history_row(event: int, points: int, total: int, **kwargs: Any) -> dict[str, Any]:
    row = {
        "event": event,
        "points": points,
        "total_points": total,
        "event_transfers": 0,
        "event_transfers_cost": 0,
        "points_on_bench": 3,
        "value": 1000,
        "bank": 5,
        "overall_rank": 400_000,
    }
    row.update(kwargs)
    return row


class TestCoarseBackfill:
    async def test_ae2_a_gap_since_the_last_run_is_filled(self):
        data = _recap_data(
            gameweek=6,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        client = _HistoryClient({1: [_history_row(gw, 40 + gw, 40 * gw) for gw in range(1, 7)]})

        await capture_recap_history(
            data, season=SEASON, history_client=client,
            finished_gameweeks=range(1, 7),
        )

        store = _store()
        assert store.captured_gameweeks() == [1, 2, 3, 4, 5, 6]
        assert store.resolved_gameweek(4)[1].gross_points == 44
        assert store.resolved_gameweek(6)[1].tier is FidelityTier.DETAILED

    async def test_one_history_call_per_manager_covers_the_whole_gap(self):
        data = _recap_data(
            gameweek=6,
            managers=[_manager(name="Alice", entry_id=1), _manager(name="Bob", entry_id=2, gw_rank=2)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280)),
        )
        rows = {
            entry: [_history_row(gw, 40 + gw, 40 * gw) for gw in range(1, 7)]
            for entry in (1, 2)
        }
        client = _HistoryClient(rows)

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=range(1, 7),
        )

        assert sorted(client.calls) == [1, 2]

    async def test_coarse_backfill_holds_the_concurrency_cap(self):
        from fpl_cli.cli._league_recap_data import _PICKS_CONCURRENCY

        entries = list(range(1, 26))
        data = _recap_data(
            gameweek=6,
            managers=[_manager(name=f"M{e}", entry_id=e, gw_rank=e) for e in entries],
            cohort=_cohort(*[(e, f"M{e}", e, 40, 200) for e in entries]),
        )
        client = _HistoryClient({
            e: [_history_row(gw, 40, 40 * gw) for gw in range(1, 7)] for e in entries
        })

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=range(1, 7),
        )

        assert client.peak <= _PICKS_CONCURRENCY

    async def test_league_positions_are_derived_across_the_cohort_not_from_global_rank(self):
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1), _manager(name="Bob", entry_id=2, gw_rank=2)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280)),
        )
        client = _HistoryClient({
            # Alice trails on the global ladder but leads this league.
            1: [_history_row(gw, 50, 50 * gw, overall_rank=400_000) for gw in range(1, 4)],
            2: [_history_row(gw, 30, 30 * gw, overall_rank=12) for gw in range(1, 4)],
        })

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=range(1, 4),
        )

        resolved = _store().resolved_gameweek(2)
        assert resolved[1].global_rank == 400_000
        assert resolved[1].league_position == 1
        assert resolved[2].league_position == 2
        assert resolved[1].gw_rank == 1
        assert resolved[2].gw_rank == 2

    async def test_a_league_that_started_late_reports_no_gap_before_its_start(self):
        data = _recap_data(
            gameweek=13,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
            league_start_event=12,
        )
        client = _HistoryClient({1: [_history_row(gw, 40, 40 * gw) for gw in range(1, 14)]})

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=range(1, 14),
        )

        assert _store().captured_gameweeks() == [12, 13]

    async def test_a_league_that_started_late_rescopes_the_cumulative_total(self):
        data = _recap_data(
            gameweek=13,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
            league_start_event=12,
        )
        client = _HistoryClient({1: [_history_row(gw, 40, 40 * gw) for gw in range(1, 14)]})

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=range(1, 14),
        )

        # Season total at GW12 is 480; the league's baseline (GW11) is 440.
        assert _store().resolved_gameweek(12)[1].total_points == 40

    async def test_one_failing_manager_becomes_unknown_rows_while_the_rest_complete(self, capsys):
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1), _manager(name="Bob", entry_id=2, gw_rank=2)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280)),
        )
        client = _HistoryClient(
            {1: [_history_row(gw, 50, 50 * gw) for gw in range(1, 4)]}, fail={2},
        )

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=range(1, 4),
        )

        resolved = _store().resolved_gameweek(2)
        assert resolved[1].capture_status is CaptureStatus.OK
        assert resolved[2].capture_status is CaptureStatus.UNKNOWN
        assert "Bob" in _stderr(capsys)

    async def test_a_gameweek_holding_an_unknown_row_is_repaired_on_the_next_run(self):
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1), _manager(name="Bob", entry_id=2, gw_rank=2)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280)),
        )
        rows = {e: [_history_row(gw, 50, 50 * gw) for gw in range(1, 4)] for e in (1, 2)}
        failing = _HistoryClient({1: rows[1]}, fail={2})
        await capture_recap_history(
            data, season=SEASON, history_client=failing, finished_gameweeks=range(1, 4),
        )
        assert _store().resolved_gameweek(2)[2].capture_status is CaptureStatus.UNKNOWN

        healthy = _HistoryClient(rows)
        await capture_recap_history(
            data, season=SEASON, history_client=healthy, finished_gameweeks=range(1, 4),
        )

        assert _store().resolved_gameweek(2)[2].capture_status is CaptureStatus.OK

    async def test_a_fully_captured_season_makes_no_history_calls_at_all(self):
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        client = _HistoryClient({1: [_history_row(gw, 50, 50 * gw) for gw in range(1, 4)]})
        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=range(1, 4),
        )
        client.calls.clear()

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=range(1, 4),
        )

        assert client.calls == []

    async def test_an_unfinished_gameweek_is_never_backfilled(self):
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        client = _HistoryClient({1: [_history_row(gw, 50, 50 * gw) for gw in range(1, 6)]})

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=[1, 2, 3],
        )

        assert _store().captured_gameweeks() == [1, 2, 3]


class TestDetailedBackfill:
    def _replay(self, calls: list[int], live: dict[str, int], fail: set[int] | None = None):
        fail = fail or set()

        async def _run(gameweek: int) -> LeagueRecapData | None:
            calls.append(gameweek)
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            try:
                for _ in range(3):
                    await asyncio.sleep(0)
                if gameweek in fail:
                    raise RuntimeError("boom")
                return _recap_data(
                    gameweek=gameweek,
                    managers=[_manager(name="Alice", entry_id=1, gross_points=gameweek)],
                    cohort=_cohort((1, "Alice", 1, gameweek, 0)),
                )
            finally:
                live["now"] -= 1

        return _run

    async def test_the_detailed_tier_makes_no_calls_without_its_flag(self):
        calls: list[int] = []
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )

        await capture_recap_history(
            data, season=SEASON, finished_gameweeks=range(1, 4),
            replay_gameweek=self._replay(calls, {"now": 0, "peak": 0}),
        )

        assert calls == []

    async def test_the_detailed_tier_supersedes_coarse_rows_when_asked(self):
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        coarse = _HistoryClient({1: [_history_row(gw, 40, 40 * gw) for gw in range(1, 4)]})
        await capture_recap_history(
            data, season=SEASON, history_client=coarse, finished_gameweeks=range(1, 4),
        )
        assert _store().resolved_gameweek(1)[1].tier is FidelityTier.COARSE

        calls: list[int] = []
        await capture_recap_history(
            data, season=SEASON, finished_gameweeks=range(1, 4),
            replay_gameweek=self._replay(calls, {"now": 0, "peak": 0}),
            backfill_detail=True,
        )

        assert calls == [1, 2]
        resolved = _store().resolved_gameweek(1)
        assert resolved[1].tier is FidelityTier.DETAILED
        assert resolved[1].gross_points == 1
        # The coarse line is superseded, never removed.
        assert len(_store().load_gameweek(1)) == 2

    async def test_gameweeks_are_replayed_one_at_a_time(self):
        live = {"now": 0, "peak": 0}
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )

        await capture_recap_history(
            data, season=SEASON, finished_gameweeks=range(1, 4),
            replay_gameweek=self._replay([], live), backfill_detail=True,
        )

        assert live["peak"] == 1

    async def test_an_interrupted_backfill_keeps_the_gameweeks_already_committed(self):
        calls: list[int] = []
        data = _recap_data(
            gameweek=6,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )

        await capture_recap_history(
            data, season=SEASON, finished_gameweeks=range(1, 7),
            replay_gameweek=self._replay(calls, {"now": 0, "peak": 0}, fail={3}),
            backfill_detail=True,
        )

        captured = _store().captured_gameweeks()
        assert 1 in captured and 2 in captured
        assert 3 not in captured

    async def test_a_draft_gameweek_holding_unknown_rows_is_repaired_without_the_flag(self):
        first = _recap_data(
            gameweek=2, fpl_format="draft",
            managers=[_manager(name="Alice", entry_id=1, league_entry_id=10)],
            cohort=_cohort((10, "Alice", 1, 60, 300), (11, "Bob", 2, 40, 280)),
        )
        await capture_recap_history(first, season=SEASON, finished_gameweeks=[1, 2])
        assert _store("draft").resolved_gameweek(2)[11].capture_status is CaptureStatus.UNKNOWN

        async def _replay(gameweek: int) -> LeagueRecapData:
            return _recap_data(
                gameweek=gameweek, fpl_format="draft",
                managers=[
                    _manager(name="Alice", entry_id=1, league_entry_id=10),
                    _manager(name="Bob", entry_id=2, league_entry_id=11, gw_rank=2),
                ],
                cohort=_cohort((10, "Alice", 1, 60, 300), (11, "Bob", 2, 40, 280)),
            )

        await capture_recap_history(
            first, season=SEASON, finished_gameweeks=[1, 2], replay_gameweek=_replay,
        )

        assert _store("draft").resolved_gameweek(2)[11].capture_status is CaptureStatus.OK


class TestCoverageReport:
    async def test_a_coarse_gap_names_the_remedy_and_what_it_holds_back(self, capsys):
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        client = _HistoryClient({1: [_history_row(gw, 40, 40 * gw) for gw in range(1, 4)]})

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=range(1, 4),
        )

        err = _stderr(capsys)
        assert "coarse" in err.lower()
        assert "--backfill-detail" in err
        assert "captain" in err.lower()

    async def test_a_fully_detailed_season_prints_nothing(self, capsys):
        data = _recap_data(
            gameweek=1,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        await capture_recap_history(data, season=SEASON, finished_gameweeks=[1])
        capsys.readouterr()

        await capture_recap_history(data, season=SEASON, finished_gameweeks=[1])

        assert "coarse" not in _stderr(capsys).lower()

    async def test_an_uncaptured_draft_gameweek_names_its_unavailable_fields(self, capsys):
        data = _recap_data(
            gameweek=3, fpl_format="draft",
            managers=[_manager(name="Alice", entry_id=1, league_entry_id=10)],
            cohort=_cohort((10, "Alice", 1, 60, 300)),
        )

        result = await capture_recap_history(
            data, season=SEASON, finished_gameweeks=[1, 2, 3],
        )

        err = _stderr(capsys)
        assert "GW1" in err or "GW1-2" in err or "1, 2" in err
        assert "cumulative total" in err.lower()
        assert "position" in err.lower()
        assert "rollover" in err.lower()
        # The gameweek that was captured still holds everything it recovered.
        assert result.rows[0].gross_points == 60

    async def test_an_unreadable_gameweek_is_named_in_the_report(self, capsys):
        data = _recap_data(
            gameweek=2,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        await capture_recap_history(data, season=SEASON, finished_gameweeks=[1, 2])
        capsys.readouterr()
        _store().gameweek_file(1).write_text("not json{{{\n", encoding="utf-8")

        await capture_recap_history(data, season=SEASON, finished_gameweeks=[1, 2])

        assert "GW1" in _stderr(capsys)


class TestBackfillCliWiring:
    def test_the_flag_is_off_by_default_and_available_on_the_command(self):
        from fpl_cli.cli.league_recap import league_recap_command

        option = next(p for p in league_recap_command.params if p.name == "backfill_detail")
        assert option.default is False
        assert "--backfill-detail" in option.opts


class TestCohortRanks:
    """KTD12: every rank on a row is a position inside the league, over everyone."""

    def test_gameweek_rank_is_derived_over_the_whole_cohort(self):
        """A failed fetch must not hand someone else the week's worst score."""
        data = _recap_data(
            managers=[
                _manager(name="Alice", entry_id=1, gross_points=60, gw_rank=1),
                _manager(name="Cara", entry_id=3, gross_points=30, gw_rank=2),
            ],
            cohort=_cohort(
                (1, "Alice", 1, 60, 300), (2, "Bob", 2, 10, 280), (3, "Cara", 3, 30, 260),
            ),
        )
        by_key = {r.manager_key: r for r in build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)}
        # Bob's standings score is in the pool, so Cara is second, not last.
        assert by_key[1].gw_rank == 1
        assert by_key[3].gw_rank == 2
        assert by_key[2].gw_rank == 3

    def test_gameweek_rank_is_net_of_the_hit_whatever_the_display_setting(self):
        data = _recap_data(
            managers=[
                _manager(name="Alice", entry_id=1, gross_points=60, transfer_cost=8),
                _manager(name="Bob", entry_id=2, gross_points=55, transfer_cost=0, gw_rank=2),
            ],
            cohort=_cohort((1, "Alice", 1, 52, 300), (2, "Bob", 2, 55, 280)),
        )
        by_key = {r.manager_key: r for r in build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)}
        assert by_key[2].gw_rank == 1
        assert by_key[1].gw_rank == 2

    def test_a_position_the_collector_supplied_is_never_overwritten(self):
        """Draft's own standings rank breaks h2h ties a total cannot."""
        data = _recap_data(
            fpl_format="draft",
            managers=[
                _manager(name="Alice", entry_id=1, league_entry_id=10, total_points=100, overall_rank=2),
                _manager(name="Bob", entry_id=2, league_entry_id=11, total_points=90, overall_rank=1, gw_rank=2),
            ],
            cohort=_cohort((10, "Alice", 1, 60, 100), (11, "Bob", 2, 40, 90)),
        )
        by_key = {r.manager_key: r for r in build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)}
        assert by_key[10].league_position == 2
        assert by_key[11].league_position == 1


class TestMultiIterationLoops:
    """Each list-building loop exercised with at least two iterations.

    `_league_recap_data.py` has a documented history of a second iteration
    corrupting the first (docs/solutions/logic-errors/walrus-operator-shadowing-
    loop-variable.md), and every loop here appends.
    """

    def test_two_transfers_both_reach_the_row_intact(self):
        def _tr(name_in: str, name_out: str, net: int) -> RecapTransfer:
            return RecapTransfer(
                player_in=name_in, player_in_team="ARS", player_in_points=net + 2,
                player_out=name_out, player_out_team="LIV", player_out_points=2,
                net=net, cost=4,
            )

        data = _recap_data(managers=[_manager(
            transfers_made=2, transfers=[_tr("InA", "OutA", 6), _tr("InB", "OutB", -3)],
        )])
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert [(t.player_in, t.player_out, t.net) for t in row.transfers] == [
            ("InA", "OutA", 6), ("InB", "OutB", -3),
        ]
        assert row.transfer_detail_shortfall == 0

    def test_two_transactions_both_reach_the_row_intact(self):
        def _txn(name_in: str, net: int) -> RecapDraftTransaction:
            return RecapDraftTransaction(
                player_in=name_in, player_in_team="ARS", player_in_points=net + 1,
                player_out="Out", player_out_team="LIV", player_out_points=1,
                net=net, kind="w",
            )

        data = _recap_data(
            fpl_format="draft",
            managers=[_manager(
                name="Alice", entry_id=1, league_entry_id=10,
                transactions=[_txn("InA", 5), _txn("InB", -2)],
            )],
            cohort=_cohort((10, "Alice", 1, 60, 300)),
        )
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert [(t.player_in, t.net) for t in row.transactions] == [("InA", 5), ("InB", -2)]

    def test_two_fines_against_one_manager_both_land(self):
        data = _recap_data(fines=[
            {"manager_name": "Alice", "manager_key": 1, "rule_type": "last-place", "message": "Pint"},
            {"manager_name": "Alice", "manager_key": 1, "rule_type": "red-card", "message": "Round"},
        ])
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert [f.rule_type for f in row.fines] == ["last-place", "red-card"]

    async def test_two_managers_with_short_transfer_lists_are_both_named(self, capsys):
        data = _recap_data(
            managers=[
                _manager(name="Alice", entry_id=1, transfers_made=2, transfers=[]),
                _manager(name="Bob", entry_id=2, gw_rank=2, transfers_made=3, transfers=[]),
            ],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280)),
        )
        await capture_recap_history(data, season=SEASON)

        err = _stderr(capsys)
        assert "Alice: 2 made, 0 captured" in err
        assert "Bob: 3 made, 0 captured" in err
        assert "2 manager(s)" in err

    async def test_two_managers_with_unmatched_players_are_both_named(self, capsys):
        data = _recap_data(
            fpl_format="draft",
            managers=[
                _manager(
                    name="Alice", entry_id=1, league_entry_id=10,
                    squad=[_player(name="GhostA", code=None, unmatched=True)],
                ),
                _manager(
                    name="Bob", entry_id=2, league_entry_id=11, gw_rank=2,
                    squad=[_player(name="GhostB", code=None, unmatched=True)],
                ),
            ],
            cohort=_cohort((10, "Alice", 1, 60, 300), (11, "Bob", 2, 40, 280)),
        )
        await capture_recap_history(data, season=SEASON)

        err = _stderr(capsys)
        assert "GhostA" in err and "GhostB" in err
        assert "Captured 2 draft player(s)" in err


class TestFormatGameweeks:
    def test_contiguous_runs_collapse_and_gaps_split(self):
        from fpl_cli.cli._league_recap_history import _format_gameweeks

        assert _format_gameweeks([1, 2, 3, 7, 9, 10]) == "GW1-3, GW7, GW9-10"

    def test_a_single_gameweek_renders_alone(self):
        from fpl_cli.cli._league_recap_history import _format_gameweeks

        assert _format_gameweeks([4]) == "GW4"

    def test_an_empty_list_renders_as_nothing(self):
        from fpl_cli.cli._league_recap_history import _format_gameweeks

        assert _format_gameweeks([]) == ""

    def test_unsorted_input_is_ordered_first(self):
        from fpl_cli.cli._league_recap_history import _format_gameweeks

        assert _format_gameweeks([3, 1, 2]) == "GW1-3"
