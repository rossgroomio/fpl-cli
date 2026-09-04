"""Tests for league-recap history capture: row building, wiring, and warnings."""
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.cli._league_recap_history import (
    _pair_squads,
    HISTORY_WARNING_BACKFILL_MANAGER_UNREACHABLE,
    HISTORY_WARNING_BACKFILL_REPLAY_FAILED,
    DETAIL_FLAG,
    HISTORY_WARNING_COVERAGE,
    HISTORY_WARNING_IDENTITY_CARRIED,
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
from fpl_cli.models.league_history import (
    CaptureStatus,
    FidelityTier,
    LedgerCaptaincy,
    LedgerPlayer,
    LedgerTransaction,
)
from fpl_cli.season import CHIP_SPLIT_GW, TOTAL_GAMEWEEKS, season_label
from fpl_cli.services.league_history import LeagueHistoryStore
from fpl_cli.services.league_history_notes import (
    GameweekWindow,
    NoteKind,
    NotesPack,
    NotesPackEntry,
    NoteSurface,
    SeasonPhase,
    build_notes_pack,
)
from tests.conftest import make_history_row

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
        "team_value", "bank", "global_rank", "global_gw_rank", "transfers_made",
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
            team_value=1013, bank=7, global_rank=400_000, global_gw_rank=12_345, transfers_made=1,
            transfers=[RecapTransfer(
                player_in="In", player_in_team="ARS", player_in_points=8,
                player_out="Out", player_out_team="LIV", player_out_points=2,
                net=6, cost=0,
            )],
        )])
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert (
            row.team_value, row.bank, row.global_rank, row.global_gw_rank, row.transfers_made,
        ) == (1013, 7, 400_000, 12_345, 1)
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

    def test_a_draft_row_omits_the_five_classic_only_fields(self):
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
        assert (
            row.team_value, row.bank, row.global_rank, row.global_gw_rank, row.transfers_made,
        ) == (None, None, None, None, None)
        assert row.transfer_detail_shortfall is None
        assert [t.player_in for t in row.transactions] == ["In"]

    def test_the_first_gameweek_records_no_previous_position(self):
        """Issue #147: GW1 has no previous table, and a row claiming the
        current position as the previous one is indistinguishable from a
        manager who genuinely held their place."""
        data = _recap_data(gameweek=1, managers=[_manager(overall_rank=3, previous_rank=3)])
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert row.league_position == 3
        assert row.previous_league_position is None

    def test_a_late_starting_league_records_none_on_its_own_first_gameweek(self):
        """A league created at GW12 has no table before GW12 either, so the
        row asks the same helper the collectors gate their derivations with
        rather than assuming the season's first gameweek is the league's."""
        data = _recap_data(
            gameweek=12, league_start_event=12,
            managers=[_manager(overall_rank=3, previous_rank=3)],
        )
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert row.previous_league_position is None

    def test_a_late_starting_league_records_movement_from_its_second_gameweek(self):
        data = _recap_data(
            gameweek=13, league_start_event=12,
            managers=[_manager(overall_rank=3, previous_rank=5)],
        )
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert row.previous_league_position == 5

    def test_a_later_gameweek_still_records_the_previous_position(self):
        data = _recap_data(gameweek=2, managers=[_manager(overall_rank=3, previous_rank=5)])
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert row.previous_league_position == 5

    def test_a_draft_row_records_no_transfer_cost(self):
        """Issue #147: draft charges nothing for a squad change, so a zero
        would be a measurement of a mechanic the format does not have."""
        data = _recap_data(
            fpl_format="draft",
            managers=[_manager(name="Alice", entry_id=1, league_entry_id=10, transfer_cost=0)],
            cohort=_cohort((10, "Alice", 1, 60, 300)),
        )
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert row.transfer_cost is None

    def test_a_classic_row_records_the_hit_it_took(self):
        data = _recap_data(managers=[_manager(transfer_cost=4)])
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert row.transfer_cost == 4

    def test_team_value_is_the_bank_inclusive_figure_the_api_reports(self):
        """Issue #147: `value` counts the bank, so the row stores it under a
        name that does not claim otherwise -- the squad alone is the
        difference."""
        data = _recap_data(managers=[_manager(team_value=1000, bank=5)])
        row = build_history_rows(data, season=SEASON, captured_at=CAPTURED_AT)[0]
        assert (row.team_value, row.bank) == (1000, 5)

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
        first = await capture_recap_history(data, season=SEASON)
        before = _store().gameweek_file(5).read_bytes()
        first_captured_at = {r.manager_key: r.captured_at for r in first.rows}

        result = await capture_recap_history(data, season=SEASON)

        assert result.written == []
        assert _store().gameweek_file(5).read_bytes() == before
        # Nothing was written this run, so the rows handed back (the JSON
        # payload's source) must carry the gameweek's original capture time,
        # not this call's fresh one -- a re-read must not be mislabelled as a
        # new capture (issue #237).
        assert {r.manager_key: r.captured_at for r in result.rows} == first_captured_at

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

    async def test_an_empty_cohort_over_a_corrupt_store_still_never_raises(self, capsys):
        """R4: a corrupt gameweek file must never escape as a raised error.

        `append_rows` short-circuits on an empty `rows` without ever parsing
        the file, so an empty cohort used to skip the existing corrupt-store
        guard entirely and reach the unconditional `resolved_gameweek` re-stamp
        call added for issue #237 -- which raises `LeagueHistoryError` on the
        same corrupt file, uncaught (issue #239 review).
        """
        path = _store().gameweek_file(5)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{\n", encoding="utf-8")

        data = _recap_data(managers=[], cohort=[])
        result = await capture_recap_history(data, season=SEASON)

        assert result.rows == []

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

    async def test_a_missing_league_id_leaves_previous_rank_at_its_derived_value(self):
        """Finding #11's second fail-open degrade path: with no league id,
        `capture_recap_history` returns before the store is ever constructed,
        so the R13 correction never runs -- `previous_rank` must survive
        untouched rather than raise or silently vanish."""
        data = _recap_data(managers=[_manager(name="Alice", entry_id=1, previous_rank=99)])
        del data["league_id"]

        result = await capture_recap_history(data, season=SEASON)

        assert result.store_readable is False
        assert data["managers"][0]["previous_rank"] == 99

    async def test_the_recorded_previous_position_correction_reaches_the_built_row(self):
        """Finding #1: before this fix, `_apply_recorded_previous_positions`
        ran after `capture_result.rows` was already built and persisted, so
        the correction reached `collected_data` (console/report) but not the
        ledger row or the `--format json` payload built from `rows`. It must
        now reach both."""
        store = _store()
        store.append_rows(4, [make_history_row(
            season=SEASON, fpl_format="classic", league_id=42, gameweek=4,
            manager_key=1, manager_name="Alice", league_position=3,
        )])
        data = _recap_data(
            gameweek=5,
            managers=[_manager(name="Alice", entry_id=1, previous_rank=99)],
        )

        result = await capture_recap_history(data, season=SEASON)

        # Reached the row this run built and returned...
        assert result.rows[0].previous_league_position == 3
        # ...and the row actually persisted to the ledger.
        persisted = store.resolved_gameweek(5)[1]
        assert persisted.previous_league_position == 3
        # `collected_data` still gets it too, for console/report.
        assert data["managers"][0]["previous_rank"] == 3


# ---------------------------------------------------------------------------
# U6: CLI wiring
# ---------------------------------------------------------------------------


def _fpl_client(gw: int = 5) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_players = AsyncMock(return_value=[])
    client.get_teams = AsyncMock(return_value=[])
    client.get_gameweek_live = AsyncMock(return_value={"elements": []})
    client.get_fixtures = AsyncMock(return_value=[])
    # `gw` is the latest finished gameweek, so a run resolving that gameweek
    # sees it as live -- the same relationship the real API reports.
    client.get_gameweeks = AsyncMock(return_value=[{"id": gw, "finished": True}])
    return client


def _invoke_recap(
    collected: LeagueRecapData, args: list[str] | None = None, *,
    client: MagicMock | None = None,
    settings: dict[str, Any] | None = None,
    replays: list[LeagueRecapData] | None = None,
    gw: int = 5,
):
    """Run the command with the collector stubbed.

    `replays` are handed back, in order, to the calls a detailed backfill
    makes after the live collection -- the collector is the same one the
    replay path goes through, so a backfill test cannot stub it separately.
    """
    client = client or _fpl_client()
    collector = (
        AsyncMock(side_effect=[collected, *replays])
        if replays else AsyncMock(return_value=collected)
    )
    with (
        patch(
            "fpl_cli.cli.league_recap.get_settings",
            return_value=settings or {"fpl": {"classic_league_id": 42}},
        ),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.cli.review._review_resolve_gw", AsyncMock(return_value={"gw": gw})),
        patch("fpl_cli.cli._league_recap_data.collect_classic_recap_data", collector),
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


class TestSummariseWithoutAKey:
    """An unusable synthesis provider must not take the recap down with it (#144).

    The editorial is opt-in garnish. The ledger capture underneath it is
    append-only and, for draft, unreconstructable once the season moves on --
    so a missing API key used to cost a gameweek of history, silently, with
    exit 0 and the reason printed to stdout.
    """

    def _no_key(self):
        from fpl_cli.api.providers import ProviderError

        return patch(
            "fpl_cli.api.providers.get_llm_provider",
            side_effect=ProviderError("ANTHROPIC_API_KEY not set"),
        )

    def test_the_recap_still_runs_and_captures_the_ledger(self):
        with self._no_key():
            result = _invoke_recap(_recap_data(), ["--summarise"])

        assert result.exit_code == 0, result.output
        assert _store().captured_gameweeks() == [5], "the ledger capture was skipped"
        assert "Test League" in result.output, "the recap itself was skipped"

    def test_the_skipped_editorial_is_reported_on_stderr(self):
        with self._no_key():
            result = _invoke_recap(_recap_data(), ["--summarise"])

        assert "ANTHROPIC_API_KEY not set" in result.stderr
        assert "ANTHROPIC_API_KEY" not in result.stdout

    def test_json_keeps_stdout_parseable_and_names_the_skip(self):
        with self._no_key():
            result = _invoke_recap(_recap_data(), ["--summarise", "--format", "json"])

        assert result.exit_code == 0, result.stderr
        envelope = json.loads(result.stdout)
        assert envelope["metadata"]["synthesis_summary"] is None
        codes = [w["code"] for w in envelope["metadata"]["warnings"]]
        assert "synthesis_provider_unavailable" in codes, (
            "a null synthesis_summary alone cannot say whether one was asked for"
        )

    def test_a_run_that_never_asked_for_an_editorial_carries_no_warning(self):
        result = _invoke_recap(_recap_data(), ["--format", "json"])

        envelope = json.loads(result.stdout)
        codes = [w["code"] for w in envelope["metadata"]["warnings"]]
        assert "synthesis_provider_unavailable" not in codes


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
# Fines capture (issue #136)
# ---------------------------------------------------------------------------


_LAST_PLACE_ONLY = {
    "fpl": {"classic_league_id": 42},
    "fines": {"classic": [{"type": "last-place", "penalty": "Pint on video"}]},
}
_ALL_THREE_RULES = {
    "fpl": {"classic_league_id": 42},
    "fines": {"classic": [
        {"type": "last-place", "penalty": "Pint"},
        {"type": "below-threshold", "threshold": 40, "penalty": "Pint"},
        {"type": "red-card", "penalty": "Round"},
    ]},
}


class TestFinesAreRecordedNotJustRendered:
    def test_a_live_capture_records_what_was_ruled_even_when_nothing_triggered(self):
        """`fines == []` says three different things at once without this; the
        row has to record which rules were actually checked."""
        result = _invoke_recap(_recap_data(), settings=_LAST_PLACE_ONLY)

        assert result.exit_code == 0, result.output
        row = _store().resolved_gameweek(5)[1]
        assert row.fine_rules_evaluated == ["last-place"]

    def test_no_configured_rules_records_an_empty_ruling_not_silence(self):
        result = _invoke_recap(_recap_data())

        assert result.exit_code == 0, result.output
        assert _store().resolved_gameweek(5)[1].fine_rules_evaluated == []

    def test_a_triggered_fine_reaches_the_stored_row(self):
        data = _recap_data(
            managers=[
                _manager(name="Alice", entry_id=1, gross_points=60),
                _manager(name="Bob", entry_id=2, gross_points=10, gw_rank=2, overall_rank=2),
            ],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 10, 200)),
        )

        result = _invoke_recap(data, settings=_LAST_PLACE_ONLY)

        assert result.exit_code == 0, result.output
        resolved = _store().resolved_gameweek(5)
        assert [f.rule_type for f in resolved[2].fines] == ["last-place"]
        assert resolved[1].fines == []

    def test_a_replayed_gameweek_records_its_fines_rather_than_landing_empty(self):
        """The bug this fixes: `--backfill-detail` repaired every other field
        of a past gameweek to the detailed tier while its fines stayed empty,
        so missing a week un-fined it permanently."""
        gw5 = _recap_data(gameweek=5)
        gw4 = _recap_data(
            gameweek=4,
            managers=[
                _manager(name="Alice", entry_id=1, gross_points=60),
                _manager(name="Bob", entry_id=2, gross_points=5, gw_rank=2, overall_rank=2),
            ],
            cohort=_cohort((1, "Alice", 1, 60, 240), (2, "Bob", 2, 5, 180)),
        )
        client = _fpl_client()
        client.get_gameweeks = AsyncMock(return_value=[
            {"id": 4, "finished": True}, {"id": 5, "finished": True},
        ])
        client.get_manager_history = AsyncMock(return_value={"current": []})

        result = _invoke_recap(
            gw5, ["--backfill-detail"], client=client,
            settings=_LAST_PLACE_ONLY, replays=[gw4],
        )

        assert result.exit_code == 0, result.output
        replayed = _store().resolved_gameweek(4)
        assert [f.rule_type for f in replayed[2].fines] == ["last-place"]
        assert replayed[2].fine_rules_evaluated == ["last-place"]


class TestCoarseTierFinesArePartialAndSaySo:
    async def test_the_cohort_only_rules_are_ruled_from_headline_points(self):
        from fpl_cli.cli._fines_config import parse_fines_config

        data = _recap_data(
            gameweek=2,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 5, 200)),
        )
        client = _HistoryClient({
            1: [_history_row(gw, 60, 60 * gw) for gw in (1, 2)],
            2: [_history_row(gw, 5, 5 * gw) for gw in (1, 2)],
        })

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=[1, 2],
            fines_config=parse_fines_config(_ALL_THREE_RULES),
        )

        coarse = _store().resolved_gameweek(1)
        assert coarse[2].tier is FidelityTier.COARSE
        assert sorted(f.rule_type for f in coarse[2].fines) == ["below-threshold", "last-place"]
        assert coarse[1].fines == []

    async def test_red_card_is_recorded_as_unruled_rather_than_acquitted(self):
        """The manager-history endpoint carries no squad, so a red-card
        handler run there would answer "no red card fine" -- a false
        acquittal, not an abstention."""
        from fpl_cli.cli._fines_config import parse_fines_config

        data = _recap_data(
            gameweek=2,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        client = _HistoryClient({1: [_history_row(gw, 60, 60 * gw) for gw in (1, 2)]})

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=[1, 2],
            fines_config=parse_fines_config(_ALL_THREE_RULES),
        )

        assert _store().resolved_gameweek(1)[1].fine_rules_evaluated == [
            "last-place", "below-threshold",
        ]

    async def test_an_unreached_manager_records_no_ruling_at_all(self):
        from fpl_cli.cli._fines_config import parse_fines_config

        data = _recap_data(
            gameweek=2,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 5, 200)),
        )
        client = _HistoryClient(
            {1: [_history_row(gw, 60, 60 * gw) for gw in (1, 2)]}, fail={2},
        )

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=[1, 2],
            fines_config=parse_fines_config(_ALL_THREE_RULES),
        )

        bob = _store().resolved_gameweek(1)[2]
        assert bob.capture_status is CaptureStatus.UNKNOWN
        assert bob.fine_rules_evaluated is None

    async def test_no_fines_config_still_records_an_empty_coarse_ruling(self):
        data = _recap_data(
            gameweek=2,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        client = _HistoryClient({1: [_history_row(gw, 60, 60 * gw) for gw in (1, 2)]})

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=[1, 2],
        )

        assert _store().resolved_gameweek(1)[1].fine_rules_evaluated == []


class TestRulingsAreFrozenAtCapture:
    """A repair re-fetches the whole cohort and re-rules it. Left alone that
    re-rules managers whose data never changed, under whatever config is
    current at repair time -- so editing a threshold in March would silently
    rewrite every already-ruled gameweek a later repair touches (#165
    review)."""

    _LENIENT = {
        "fpl": {"classic_league_id": 42},
        "fines": {"classic": [{"type": "below-threshold", "threshold": 40, "penalty": "Pint"}]},
    }
    _HARSH = {
        "fpl": {"classic_league_id": 42},
        "fines": {"classic": [{"type": "below-threshold", "threshold": 100, "penalty": "Pint"}]},
    }

    @staticmethod
    def _config(settings: dict[str, Any]):
        from fpl_cli.cli._fines_config import parse_fines_config

        return parse_fines_config(settings)

    def _data(self):
        return _recap_data(
            gameweek=2,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 5, 200)),
        )

    async def _capture(self, settings, *, reachable: set[int]):
        client = _HistoryClient(
            {entry: [_history_row(gw, 60 if entry == 1 else 5, 60 * gw) for gw in (1, 2)]
             for entry in reachable},
            fail={2} - reachable,
        )
        return await capture_recap_history(
            self._data(), season=SEASON, history_client=client, finished_gameweeks=[1, 2],
            fines_config=self._config(settings),
        )

    async def test_a_config_change_does_not_rewrite_an_already_ruled_gameweek(self):
        await self._capture(self._LENIENT, reachable={1})
        assert _store().resolved_gameweek(1)[1].fines == []

        # Bob is still unreachable, so GW1 is still "incomplete" and gets
        # repaired again -- this time under a threshold that would fine Alice.
        await self._capture(self._HARSH, reachable={1})

        alice = _store().resolved_gameweek(1)[1]
        assert alice.fines == [], "a ruling already recorded is history, not a re-derivation"
        assert alice.fine_rules_evaluated == ["below-threshold"]

    async def test_a_repair_that_actually_fills_a_gap_rules_the_cohort_together(self):
        """Config drift must not re-rule, but a real repair must: leaving the
        already-ruled half frozen while the newly-filled half is ruled could
        record two managers as last place in one gameweek."""
        await self._capture(self._LENIENT, reachable={1})
        assert _store().resolved_gameweek(1)[2].capture_status is CaptureStatus.UNKNOWN

        await self._capture(self._LENIENT, reachable={1, 2})

        resolved = _store().resolved_gameweek(1)
        assert resolved[2].capture_status is CaptureStatus.OK
        assert [f.rule_type for f in resolved[2].fines] == ["below-threshold"]
        assert resolved[2].fine_rules_evaluated == ["below-threshold"]

    async def test_a_detailed_upgrade_still_rules_what_the_coarse_tier_could_not(self):
        """Freezing must not block a tier upgrade: the detailed replay can
        rule `red-card`, which the coarse capture recorded as unruled."""
        await capture_recap_history(
            self._data(), season=SEASON,
            history_client=_HistoryClient(
                {1: [_history_row(gw, 60, 60 * gw) for gw in (1, 2)],
                 2: [_history_row(gw, 5, 5 * gw) for gw in (1, 2)]},
            ),
            finished_gameweeks=[1, 2],
            fines_config=self._config(_ALL_THREE_RULES),
        )
        assert _store().resolved_gameweek(1)[1].fine_rules_evaluated == [
            "last-place", "below-threshold",
        ]

        replayed = _recap_data(
            gameweek=1,
            managers=[_manager(name="Alice", entry_id=1, gross_points=60)],
            cohort=_cohort((1, "Alice", 1, 60, 60)),
        )
        replayed["fine_rules_evaluated"] = ["last-place", "below-threshold", "red-card"]

        async def _replay(gameweek: int):
            return replayed if gameweek == 1 else None

        await capture_recap_history(
            self._data(), season=SEASON, finished_gameweeks=[1, 2],
            replay_gameweek=_replay, backfill_detail=True,
            fines_config=self._config(_ALL_THREE_RULES),
        )

        assert _store().resolved_gameweek(1)[1].fine_rules_evaluated == [
            "last-place", "below-threshold", "red-card",
        ]


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
        "rank": 12_345,
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

    async def test_the_coarse_tier_captures_the_per_gameweek_world_rank_too(self):
        """Issue #148: `/entry/{id}/history`'s `rank` (this gameweek alone) is
        a different field from `overall_rank` (season-cumulative), and both
        must be captured rather than the world rank collapsing onto one."""
        data = _recap_data(
            gameweek=2,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        client = _HistoryClient({
            1: [_history_row(gw, 50, 50 * gw, rank=9_999, overall_rank=400_000) for gw in range(1, 3)],
        })

        await capture_recap_history(
            data, season=SEASON, history_client=client, finished_gameweeks=range(1, 3),
        )

        row = _store().resolved_gameweek(1)[1]
        assert row.global_gw_rank == 9_999
        assert row.global_rank == 400_000

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

    async def test_a_backfill_detail_run_retrofits_the_per_gameweek_world_rank(self):
        """Issue #148: `--backfill-detail` is the suggested retrofit path for
        a gameweek already captured only at the coarse tier -- the
        superseding detailed row it writes must carry `global_gw_rank`, not
        just the fields the coarse tier already had."""
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        coarse = _HistoryClient({1: [_history_row(gw, 40, 40 * gw, rank=9_999) for gw in range(1, 4)]})
        await capture_recap_history(
            data, season=SEASON, history_client=coarse, finished_gameweeks=range(1, 4),
        )
        assert _store().resolved_gameweek(1)[1].global_gw_rank == 9_999

        async def _replay_with_gw_rank(gameweek: int) -> LeagueRecapData | None:
            return _recap_data(
                gameweek=gameweek,
                managers=[_manager(name="Alice", entry_id=1, global_gw_rank=1_234)],
                cohort=_cohort((1, "Alice", 1, gameweek, 0)),
            )

        await capture_recap_history(
            data, season=SEASON, finished_gameweeks=range(1, 4),
            replay_gameweek=_replay_with_gw_rank, backfill_detail=True,
        )

        resolved = _store().resolved_gameweek(1)
        assert resolved[1].tier is FidelityTier.DETAILED
        assert resolved[1].global_gw_rank == 1_234

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

    async def test_a_failed_replay_reaches_capture_result_warnings(self):
        """Finding #10: `_detailed_backfill`'s replay-failure diagnostic used
        to go straight to `error_console.print()`, bypassing `_warn()` -- so
        it never reached `capture_result.warnings`, and `--format json`
        mode's warning-prose suppression (`error_console.capture()` with no
        `as` binding) dropped it with no replacement. It must now go through
        `_warn()` like every other diagnostic in this module."""
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )

        result = await capture_recap_history(
            data, season=SEASON, finished_gameweeks=range(1, 4),
            replay_gameweek=self._replay([], {"now": 0, "peak": 0}, fail={2}),
            backfill_detail=True,
        )

        matching = [w for w in result.warnings if w["code"] == HISTORY_WARNING_BACKFILL_REPLAY_FAILED]
        assert len(matching) == 1
        assert "GW2" in matching[0]["message"]
        assert "boom" in matching[0]["message"]


class TestBackfillCountersInvalidation:
    async def test_a_coarse_repair_of_an_earlier_unknown_gameweek_is_reflected_in_the_next_counters(
        self,
    ):
        """The counters cache advances through GW1 while that manager's row is
        still unknown (`weeks_on_top` holds, per R19). GW1 is then repaired --
        the coarse tier's automatic, `--backfill-detail`-free part of
        `_backfill` -- in the very same run that also captures GW2, which is
        exactly `stamp + 1`. Without `invalidate_if_repaired` wired through
        `_backfill`'s returned gameweeks, `compute_counters_through`'s fast
        path would fold GW2 onto the run state cached back when GW1 was still
        unknown -- `weeks_on_top` length=1, start_gameweek=2 -- instead of
        recognising the run actually started at GW1."""
        from fpl_cli.models.league_history import LeagueHistoryCountersProjection
        from fpl_cli.services.league_history_counters import (
            counters_file,
            manager_condition_views,
            rebuild_counters_through,
        )

        gw1 = _recap_data(gameweek=1, managers=[], cohort=_cohort((1, "Alice", 1, 60, 300)))
        await capture_recap_history(gw1, season=SEASON)
        assert _store().resolved_gameweek(1)[1].capture_status is CaptureStatus.UNKNOWN

        gw2 = _recap_data(
            gameweek=2,
            managers=[_manager(name="Alice", entry_id=1, overall_rank=1, gw_rank=1)],
            cohort=_cohort((1, "Alice", 1, 60, 600)),
        )
        client = _HistoryClient({1: [_history_row(1, 60, 300, overall_rank=1)]})
        await capture_recap_history(
            gw2, season=SEASON, history_client=client, finished_gameweeks=[1, 2],
        )

        # The repair really happened: GW1's line count grew (append-only; the
        # unknown line is kept, not replaced) and its winner is now coarse OK.
        assert len(_store().load_gameweek(1)) == 2
        resolved_gw1 = _store().resolved_gameweek(1)
        assert resolved_gw1[1].capture_status is CaptureStatus.OK
        assert resolved_gw1[1].tier is FidelityTier.COARSE

        cached = LeagueHistoryCountersProjection.model_validate_json(
            counters_file(SEASON, "classic", 42).read_text(encoding="utf-8"),
        )
        expected = rebuild_counters_through(_store(), 2)
        assert cached.runs == expected.runs

        view = manager_condition_views(cached, 1)["weeks_on_top"]
        assert (view.length, view.start_gameweek) == (2, 1)


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

    async def test_an_unreadable_gameweek_is_a_store_problem_not_a_coverage_gap(self, capsys):
        """Issue #224: the read path used to report a corrupt file as two
        `league_history_coverage` lines -- one of them the "no recorded rows,
        re-run with --backfill-detail" hint, for a file that holds rows and
        that `_gaps` refuses to hand backfill anyway -- while the documented
        `league_history_store_unreadable` came only from the write path, and
        the path and `mv` remedy reached no machine-readable surface at all."""
        data = _recap_data(
            gameweek=2,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        await capture_recap_history(data, season=SEASON, finished_gameweeks=[1, 2])
        path = _store().gameweek_file(1)
        path.write_text("not json{{{\n", encoding="utf-8")
        capsys.readouterr()

        result = await capture_recap_history(data, season=SEASON, finished_gameweeks=[1, 2])

        unreadable = [w for w in result.warnings if w["code"] == HISTORY_WARNING_STORE_UNREADABLE]
        assert len(unreadable) == 1
        assert "GW1" in unreadable[0]["message"]
        assert str(path) in unreadable[0]["message"]
        assert f"mv '{path}'" in unreadable[0]["message"]
        # No coverage line at all: GW1 is not a gap, and GW2 is fully detailed.
        assert [w for w in result.warnings if w["code"] == HISTORY_WARNING_COVERAGE] == []
        assert DETAIL_FLAG not in _stderr(capsys)

    async def test_an_unreadable_gameweek_outside_the_target_window_is_still_reported(self):
        """A file that will not parse is a store problem whatever window the
        coverage report spans, and `coverage()` no longer logs it -- so a
        gameweek the window excludes (a league whose start moved later, say)
        would otherwise be reported nowhere at all (issue #224 review)."""
        data = _recap_data(
            gameweek=3,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
            league_start_event=3,
        )
        await capture_recap_history(data, season=SEASON, finished_gameweeks=[1, 2, 3])
        path = _store().gameweek_file(1)
        path.write_text("not json{{{\n", encoding="utf-8")

        result = await capture_recap_history(data, season=SEASON, finished_gameweeks=[1, 2, 3])

        unreadable = [w for w in result.warnings if w["code"] == HISTORY_WARNING_STORE_UNREADABLE]
        assert len(unreadable) == 1
        assert "GW1" in unreadable[0]["message"]
        assert str(path) in unreadable[0]["message"]
        # The window still scopes the coverage report itself: GW1 and GW2 are
        # before the league started, so neither is a gap to fill.
        assert [w for w in result.warnings if w["code"] == HISTORY_WARNING_COVERAGE] == []

    async def test_an_unreadable_gameweek_is_reported_once_not_once_per_reader(
        self, capsys, caplog,
    ):
        """Issue #224: every consumer of one recap reads the same gameweek --
        the coverage pass, the counters rebuild, the notes pack, the
        earliest-row scan, the fines tally -- and each logged the whole
        path-and-`mv` message, so one truncated line printed five times.

        Also pins the flag `capture_recap_history` sets on the store: the
        capture reports every unreadable gameweek itself, so *no* reader may
        log one loudly, whatever order they run in."""
        data = _recap_data(
            gameweek=2,
            managers=[_manager(name="Alice", entry_id=1)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )
        await capture_recap_history(data, season=SEASON, finished_gameweeks=[1, 2])
        _store().gameweek_file(1).write_text("not json{{{\n", encoding="utf-8")
        capsys.readouterr()

        with caplog.at_level(logging.DEBUG, logger="fpl_cli.services.league_history"):
            await capture_recap_history(data, season=SEASON, finished_gameweeks=[1, 2])

        # Counted on a single word: rich soft-wraps the long path mid-message,
        # and `_stderr` rejoins the lines without the space it wrapped on, so
        # a multi-word phrase is not reliably countable. "aside" appears once
        # per copy of the remedy.
        assert _stderr(capsys).count("aside") == 1
        # The readers that swallow the reason still leave their own context
        # behind, one level down -- deduped, not dropped.
        trail = [r for r in caplog.records if "Move the file aside" in r.getMessage()]
        assert len(trail) > 1
        assert [r for r in trail if r.levelno > logging.DEBUG] == []


class TestGaps:
    """_gaps() classification that targets U7's backfill (missing/incomplete/coarse)."""

    def test_an_unreadable_gameweek_is_reported_not_targeted_for_backfill(self):
        """An unreadable gameweek must land in none of the three buckets: it is
        reported, not overwritten, so a repair attempt never replays or
        refetches a file that will just fail to parse again."""
        from fpl_cli.cli._league_recap_history import _gaps
        from fpl_cli.services.league_history import GameweekCoverage

        gaps = _gaps([GameweekCoverage(gameweek=1, readable=False)], [1])

        assert gaps.missing == []
        assert gaps.incomplete == []
        assert gaps.coarse == []

    def test_a_gameweek_never_captured_at_all_is_missing(self):
        from fpl_cli.cli._league_recap_history import _gaps

        gaps = _gaps([], [1])

        assert gaps.missing == [1]


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


# ---------------------------------------------------------------------------
# U10: console and report rendering
# ---------------------------------------------------------------------------


def _stdout(capsys: pytest.CaptureFixture[str]) -> str:
    """Captured stdout with rich's soft wrapping undone (see `_stderr`)."""
    return capsys.readouterr().out.replace("\n", "")


def _streak_entry(
    text: str = "Alice: 3 gameweeks on top of the league in a row (GW1-GW3).",
    *,
    surfaces: frozenset[NoteSurface] = frozenset({NoteSurface.CONSOLE, NoteSurface.REPORT, NoteSurface.PROMPT}),
    excess: int = 1,
    manager_name: str = "Alice",
    condition_key: str = "weeks_on_top",
) -> NotesPackEntry:
    return NotesPackEntry(
        kind=NoteKind.STREAK, text=text, surfaces=surfaces, excess=excess,
        manager_name=manager_name, condition_key=condition_key,
        window=GameweekWindow(start_gameweek=1, end_gameweek=3), length=3,
    )


def _notes_pack(
    entries: list[NotesPackEntry] | None = None,
    coverage_entries: list[NotesPackEntry] | None = None,
    *,
    phase: SeasonPhase = SeasonPhase.MIDPOINT,
    phase_text: str = "GW20 is the season midpoint.",
    gameweek: int = 20,
) -> NotesPack:
    return NotesPack(
        season=SEASON, fpl_format="classic", league_id=42, gameweek=gameweek, phase=phase,
        league_start_gameweek=1,
        season_phase_entry=NotesPackEntry(
            kind=NoteKind.SEASON_PHASE, text=phase_text, surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
        ),
        entries=entries or [],
        coverage_entries=coverage_entries if coverage_entries is not None else [
            NotesPackEntry(
                kind=NoteKind.COVERAGE, text="Recorded history is complete from its start (GW1) through GW20.",
                surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
            ),
        ],
    )


class TestConsoleStreaks:
    def test_only_the_capped_leaders_render_on_console(self, capsys: pytest.CaptureFixture[str]):
        from fpl_cli.cli.league_recap import _render_console_highlights

        # Pre-sorted descending by excess, matching the real invariant
        # `_streak_entries` (U9) already produces -- the 5 leaders are
        # Manager5..Manager1, and Manager0 (the lowest excess) is capped.
        entries = [
            _streak_entry(text=f"Manager{i}: streak {i}", excess=i, manager_name=f"Manager{i}")
            for i in (5, 4, 3, 2, 1, 0)
        ]
        _render_console_highlights(
            _recap_data(managers=[_manager(total_points=300)]), _notes_pack(entries=entries),
        )

        out = _stdout(capsys)
        assert "Manager5: streak 5" in out
        assert "Manager1: streak 1" in out
        assert "Manager0: streak 0" not in out

    def test_an_empty_pack_renders_no_streaks_heading(self, capsys: pytest.CaptureFixture[str]):
        from fpl_cli.cli.league_recap import _render_console_highlights

        _render_console_highlights(_recap_data(managers=[_manager()]), _notes_pack(entries=[]))

        assert "Streaks:" not in _stdout(capsys)

    def test_no_pack_at_all_renders_no_streaks_heading(self, capsys: pytest.CaptureFixture[str]):
        from fpl_cli.cli.league_recap import _render_console_highlights

        _render_console_highlights(_recap_data(managers=[_manager()]), None)

        assert "Streaks:" not in _stdout(capsys)

    def test_a_below_minimum_entry_with_no_surfaces_does_not_render(self, capsys: pytest.CaptureFixture[str]):
        from fpl_cli.cli.league_recap import _render_console_highlights

        entry = _streak_entry(text="Alice: single blank", surfaces=frozenset())
        _render_console_highlights(_recap_data(managers=[_manager()]), _notes_pack(entries=[entry]))

        assert "single blank" not in _stdout(capsys)


class TestConsoleUnavailable:
    def test_ae8_a_manager_with_no_derivable_total_is_named_unavailable(self, capsys: pytest.CaptureFixture[str]):
        from fpl_cli.cli.league_recap import _render_console_highlights

        data = _recap_data(managers=[_manager(name="Alice", total_points=None, overall_rank=None)])
        _render_console_highlights(data, None)

        out = _stdout(capsys)
        assert "Alice" in out
        assert "position unavailable" in out
        assert "total unavailable" in out

    def test_two_managers_each_missing_a_different_field_are_both_named(self, capsys: pytest.CaptureFixture[str]):
        data = _recap_data(managers=[
            _manager(name="Alice", entry_id=1, total_points=None, overall_rank=1, previous_rank=1),
            _manager(name="Bob", entry_id=2, total_points=200, overall_rank=None),
        ])
        from fpl_cli.cli.league_recap import _render_console_highlights

        _render_console_highlights(data, None)

        out = _stdout(capsys)
        assert "Alice: total unavailable" in out
        assert "Bob: position unavailable" in out

    def test_a_fully_resolved_manager_is_not_listed_as_unavailable(self, capsys: pytest.CaptureFixture[str]):
        from fpl_cli.cli.league_recap import _render_console_highlights

        _render_console_highlights(_recap_data(managers=[_manager(total_points=300)]), None)

        assert "Unavailable:" not in _stdout(capsys)


class TestApplyRecordedPreviousPositions:
    """`_apply_recorded_previous_positions` now lives in `_league_recap_history`
    and takes a `LeagueHistoryStore` instance directly -- it runs inside
    `capture_recap_history`, on the same store that function already
    constructs, rather than as a CLI-layer post-hoc step with its own store
    (finding #1/#2/#14/#18's coordinated fix). See `TestCaptureRecapHistory`
    for coverage that the correction reaches `capture_result.rows`, not just
    `data` -- the exact gap finding #1 identified."""

    def test_gw1_is_a_no_op(self):
        from fpl_cli.cli._league_recap_history import _apply_recorded_previous_positions

        data = _recap_data(managers=[_manager(name="Alice", previous_rank=99)])
        _apply_recorded_previous_positions(data, _store(), 1)

        assert data["managers"][0]["previous_rank"] == 99

    def test_a_recorded_prior_row_overrides_the_derived_previous_rank(self):
        from fpl_cli.cli._league_recap_history import _apply_recorded_previous_positions

        store = _store()
        store.append_rows(4, [make_history_row(
            season=SEASON, fpl_format="classic", league_id=42, gameweek=4,
            manager_key=1, manager_name="Alice", league_position=3,
        )])
        data = _recap_data(managers=[_manager(name="Alice", entry_id=1, previous_rank=99)])

        _apply_recorded_previous_positions(data, store, 5)

        assert data["managers"][0]["previous_rank"] == 3

    def test_no_prior_row_falls_back_unchanged(self):
        from fpl_cli.cli._league_recap_history import _apply_recorded_previous_positions

        data = _recap_data(managers=[_manager(name="Alice", entry_id=1, previous_rank=99)])
        _apply_recorded_previous_positions(data, _store(), 5)

        assert data["managers"][0]["previous_rank"] == 99

    def test_a_manager_absent_from_the_prior_row_falls_back_unchanged(self):
        from fpl_cli.cli._league_recap_history import _apply_recorded_previous_positions

        store = _store()
        store.append_rows(4, [make_history_row(
            season=SEASON, fpl_format="classic", league_id=42, gameweek=4,
            manager_key=1, manager_name="Alice", league_position=3,
        )])
        data = _recap_data(managers=[
            _manager(name="Alice", entry_id=1, previous_rank=99),
            _manager(name="Bob", entry_id=2, previous_rank=88),
        ])

        _apply_recorded_previous_positions(data, store, 5)

        assert data["managers"][0]["previous_rank"] == 3
        assert data["managers"][1]["previous_rank"] == 88

    def test_draft_keys_on_league_entry_id_not_entry_id(self):
        from fpl_cli.cli._league_recap_history import _apply_recorded_previous_positions

        store = LeagueHistoryStore(SEASON, "draft", 42)  # type: ignore[arg-type]
        store.append_rows(4, [make_history_row(
            season=SEASON, fpl_format="draft", league_id=42, gameweek=4,
            manager_key=10, manager_name="Alice", league_position=2,
        )])
        data = _recap_data(
            managers=[_manager(name="Alice", entry_id=1, league_entry_id=10, previous_rank=99)],
            fpl_format="draft",
        )

        _apply_recorded_previous_positions(data, store, 5)

        assert data["managers"][0]["previous_rank"] == 2

    def test_an_unreadable_prior_gameweek_falls_back_unchanged(self):
        """Finding #11: the fail-open degrade path for a corrupt/unreadable
        GW-1 ledger file -- must not raise, and `previous_rank` stays at its
        derived value rather than being overridden from a file that can't
        be trusted."""
        from fpl_cli.cli._league_recap_history import _apply_recorded_previous_positions

        store = _store()
        path = store.gameweek_file(4)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{\n", encoding="utf-8")
        data = _recap_data(managers=[_manager(name="Alice", entry_id=1, previous_rank=99)])

        _apply_recorded_previous_positions(data, store, 5)

        assert data["managers"][0]["previous_rank"] == 99


class TestLeagueHistoryReportSection:
    async def test_streaks_and_coverage_render_under_one_heading(self, tmp_path: Path):
        from fpl_cli.agents.orchestration.report import ReportAgent

        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = dict(_recap_data(managers=[_manager(name="Alice", overall_rank=1, previous_rank=1)]))
        data["league_history_phase_text"] = "GW20 is the season midpoint."
        data["league_history_streak_lines"] = ["Alice: 3 gameweeks on top of the league in a row (GW1-GW3)."]
        data["league_history_coverage_lines"] = ["Recorded history is complete from its start (GW1) through GW20."]

        result = await agent.run(context={"report_type": "league-recap", "gameweek": 20, "data": data})

        content = Path(result.data["report_path"]).read_text(encoding="utf-8")
        assert "# League History" in content
        assert "## Streaks" in content
        assert "Alice: 3 gameweeks on top of the league in a row (GW1-GW3)." in content
        assert "Recorded history is complete from its start (GW1) through GW20." in content
        assert "GW20 is the season midpoint." in content

    async def test_season_counts_render_under_their_own_subheading(self, tmp_path: Path):
        from fpl_cli.agents.orchestration.report import ReportAgent

        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = dict(_recap_data(managers=[_manager(name="Alice", overall_rank=1, previous_rank=1)]))
        data["league_history_phase_text"] = "GW20 is the season midpoint."
        data["league_history_streak_lines"] = []
        data["league_history_season_count_lines"] = [
            "Alice: 4 gameweek wins this season (GW1-GW20), the latest this gameweek.",
        ]
        data["league_history_coverage_lines"] = []

        result = await agent.run(context={"report_type": "league-recap", "gameweek": 20, "data": data})

        content = Path(result.data["report_path"]).read_text(encoding="utf-8")
        assert "# League History" in content
        assert "## Season Counts" in content
        assert "## Streaks" not in content
        assert "Alice: 4 gameweek wins this season (GW1-GW20), the latest this gameweek." in content

    async def test_no_streaks_heading_when_the_pack_has_no_open_runs(self, tmp_path: Path):
        from fpl_cli.agents.orchestration.report import ReportAgent

        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = dict(_recap_data(managers=[_manager(name="Alice", overall_rank=1, previous_rank=1)]))
        data["league_history_phase_text"] = "GW1 is the season opener."
        data["league_history_streak_lines"] = []
        data["league_history_coverage_lines"] = ["No league history has been recorded before GW1."]

        result = await agent.run(context={"report_type": "league-recap", "gameweek": 1, "data": data})

        content = Path(result.data["report_path"]).read_text(encoding="utf-8")
        assert "## Streaks" not in content
        assert "# League History" in content  # coverage still has something to say
        assert "No league history has been recorded before GW1." in content

    async def test_no_league_history_section_at_all_without_a_pack(self, tmp_path: Path):
        from fpl_cli.agents.orchestration.report import ReportAgent

        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = dict(_recap_data(managers=[_manager(name="Alice", overall_rank=1, previous_rank=1)]))

        result = await agent.run(context={"report_type": "league-recap", "gameweek": 1, "data": data})

        content = Path(result.data["report_path"]).read_text(encoding="utf-8")
        assert "# League History" not in content

    async def test_ae6_the_finale_wording_renders(self, tmp_path: Path):
        from fpl_cli.agents.orchestration.report import ReportAgent

        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = dict(_recap_data(managers=[_manager(name="Alice", overall_rank=1, previous_rank=1)]))
        data["league_history_phase_text"] = "GW38 is the season finale."

        result = await agent.run(context={"report_type": "league-recap", "gameweek": 38, "data": data})

        content = Path(result.data["report_path"]).read_text(encoding="utf-8")
        assert "GW38 is the season finale." in content


class TestSeasonPhaseTextIsPromptOnly:
    """Issue #187: the season-phase marker (`_season_phase_entry`) is
    scene-setting context for the editorial writer, not a fact worth
    printing in the report body -- through the full command, not just the
    `NotesPack` construction it's derived from."""

    def test_the_saved_report_does_not_print_the_season_phase_note(self, tmp_path: Path):
        result = _invoke_recap(_recap_data(), ["--save", "--output", str(tmp_path)])

        assert result.exit_code == 0, result.output
        content = (tmp_path / season_label() / "gw5-league-recap.md").read_text(encoding="utf-8")
        assert "chip-availability boundary" not in content


class TestFormatGatingInPack:
    def test_draft_pack_has_no_captain_blank_entry_classic_pack_has_no_waiver_entry(self):
        """Delegated entirely to U8/U9's format filtering -- confirmed here at
        the pack-consumption boundary so a regression there is caught by this
        unit's own tests too."""
        store_classic = _store(fpl_format="classic", league_id=1)
        store_draft = LeagueHistoryStore(SEASON, "draft", 2)  # type: ignore[arg-type]
        for gw in (1, 2):
            store_classic.append_rows(gw, [make_history_row(
                season=SEASON, fpl_format="classic", league_id=1, gameweek=gw,
                manager_key=1, manager_name="Alice",
                captain=LedgerCaptaincy(name="Cap", points=1, played=True, had_fixture=True),
            )])
            store_draft.append_rows(gw, [make_history_row(
                season=SEASON, fpl_format="draft", league_id=2, gameweek=gw,
                manager_key=1, manager_name="Bob",
                transactions=[LedgerTransaction(
                    player_in="X", player_in_team="AAA", player_in_points=0,
                    player_out="Y", player_out_team="BBB", player_out_points=0, net=5,
                )],
            )])

        classic_pack = build_notes_pack(store_classic, 2)
        draft_pack = build_notes_pack(store_draft, 2)

        assert not any(e.condition_key == "waiver_win_run" for e in classic_pack.entries)
        assert not any(e.condition_key == "captain_blank_run" for e in draft_pack.entries)


class TestEndToEndStreakThroughTheFullCommand:
    """U10's own verification criterion: a recap against a seeded
    multi-gameweek partition shows a streak line a single-gameweek run
    could not have produced, through the real CLI path -- store seeding,
    live capture, counters, notes pack, and console rendering all wired
    together, not just each piece in isolation."""

    def test_a_seeded_run_of_top_table_finishes_surfaces_a_streak_on_console(self):
        store = _store(fpl_format="classic", league_id=42)
        for gw in (1, 2, 3, 4):
            store.append_rows(gw, [make_history_row(
                season=SEASON, fpl_format="classic", league_id=42, gameweek=gw,
                manager_key=1, manager_name="Alice", league_position=1, gw_rank=1,
            )])

        result = _invoke_recap(_recap_data(
            gameweek=5,
            managers=[_manager(name="Alice", entry_id=1, overall_rank=1, previous_rank=1, total_points=300)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        ))

        assert result.exit_code == 0, result.output
        assert "Streaks:" in result.output
        assert "Alice" in result.output
        assert "gameweeks on top of the league" in result.output

    def test_a_single_gameweek_run_shows_no_streak_yet(self):
        """The same manager's first-ever captured gameweek: nothing to
        stream yet, since `weeks_on_top` needs a minimum run of 2."""
        result = _invoke_recap(_recap_data(
            gameweek=1,
            managers=[_manager(name="Alice", entry_id=1, overall_rank=1, previous_rank=None, total_points=60)],
            cohort=_cohort((1, "Alice", 1, 60, 60)),
        ))

        assert result.exit_code == 0, result.output
        assert "Streaks:" not in result.output


# ---------------------------------------------------------------------------
# U11: league-recap --format json
# ---------------------------------------------------------------------------


def _invoke_recap_unresolved_gameweek(args: list[str] | None = None):
    """Like `_invoke_recap`, but the gameweek cannot be resolved at all --
    no collector is ever reached."""
    client = _fpl_client()
    with (
        patch("fpl_cli.cli.league_recap.get_settings", return_value={"fpl": {"classic_league_id": 42}}),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        patch("fpl_cli.cli.review._review_resolve_gw", AsyncMock(return_value=None)),
    ):
        from fpl_cli.cli.league_recap import league_recap_command

        return CliRunner().invoke(league_recap_command, args or [])


class TestLeagueRecapJsonEnvelope:
    def test_the_envelope_carries_command_metadata_and_data(self):
        result = _invoke_recap(_recap_data(), ["--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["command"] == "league-recap"
        assert isinstance(payload["metadata"], dict)
        assert isinstance(payload["data"], list)

    def test_no_rich_output_is_mixed_into_stdout(self):
        result = _invoke_recap(_recap_data(), ["--format", "json"])

        assert result.exit_code == 0, result.output
        # A clean parse is itself the proof: any stray Rich markup or panel
        # text ahead of/after the envelope would break json.loads outright.
        json.loads(result.stdout)
        assert "Gameweek 5 League Recap" not in result.stdout
        # The header was genuinely printed (redirected to stderr, not
        # skipped outright) -- proof this is real suppression, not an
        # accidentally-silent run with nothing to leak in the first place.
        assert "Gameweek 5 League Recap" in result.stderr

    def test_a_per_manager_payload_matches_the_stored_row_field_names(self):
        result = _invoke_recap(
            _recap_data(managers=[_manager(name="Alice", entry_id=1)]), ["--format", "json"],
        )

        payload = json.loads(result.stdout)
        assert len(payload["data"]) == 1
        row = payload["data"][0]
        assert row["manager_name"] == "Alice"
        assert row["season"] == SEASON
        assert row["fpl_format"] == "classic"
        assert row["gameweek"] == 5
        assert row["capture_status"] == "ok"

    def test_the_recorded_previous_position_correction_reaches_the_payload(self):
        """Finding #1, through the full command: the ledger's recorded GW4
        position -- not the raw, uncorrected `previous_rank` the collected
        data started with -- is what the `--format json` payload shows,
        because the correction now runs inside `capture_recap_history`
        before `capture_result.rows` (the payload's source) is built."""
        store = _store(fpl_format="classic", league_id=42)
        store.append_rows(4, [make_history_row(
            season=SEASON, fpl_format="classic", league_id=42, gameweek=4,
            manager_key=1, manager_name="Alice", league_position=3,
        )])

        result = _invoke_recap(
            _recap_data(
                gameweek=5,
                managers=[_manager(name="Alice", entry_id=1, previous_rank=99)],
            ),
            ["--format", "json"],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["data"][0]["previous_league_position"] == 3

    def test_the_warnings_key_is_present_and_empty_on_a_clean_run(self):
        result = _invoke_recap(_recap_data(), ["--format", "json"])

        payload = json.loads(result.stdout)
        assert payload["metadata"]["warnings"] == []

    def test_the_first_capture_notice_survives_warning_suppression_in_json_mode(self):
        """The first-capture-of-a-partition stderr notice is not a `_warn()`
        code, so JSON mode's warning-prose suppression must not swallow it
        with no replacement -- it has to reach the payload some other way."""
        assert not _store().partition_exists()

        result = _invoke_recap(_recap_data(), ["--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        path = payload["metadata"]["first_capture_store_path"]
        assert path is not None
        assert path == str(_store().partition_dir())

    def test_a_second_run_against_an_existing_partition_has_no_first_capture_notice(self):
        result = _invoke_recap(_recap_data(), ["--format", "json"])
        assert result.exit_code == 0, result.output

        second = _invoke_recap(_recap_data(gameweek=6), ["--format", "json"])

        payload = json.loads(second.stdout)
        assert payload["metadata"]["first_capture_store_path"] is None

    def test_a_corrupted_store_produces_a_warning_code_full_data_and_exit_zero(self):
        path = _store().gameweek_file(5)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{\n", encoding="utf-8")

        result = _invoke_recap(
            _recap_data(managers=[_manager(name="Alice", entry_id=1)]), ["--format", "json"],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert len(payload["data"]) == 1
        codes = [w["code"] for w in payload["metadata"]["warnings"]]
        assert HISTORY_WARNING_STORE_UNREADABLE in codes

    def test_an_unreadable_prior_gameweek_reaches_metadata_warnings_with_its_remedy(self):
        """Issue #224: a consumer scripting on the documented
        `league_history_store_unreadable` never saw it for a gameweek that
        failed to *read*, and the two `league_history_coverage` lines it got
        instead named neither the file nor anything it could act on."""
        path = _store().gameweek_file(1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{\n", encoding="utf-8")
        client = _fpl_client()
        client.get_gameweeks = AsyncMock(return_value=[
            {"id": 1, "finished": True}, {"id": 5, "finished": True},
        ])

        result = _invoke_recap(
            _recap_data(managers=[_manager(name="Alice", entry_id=1)]),
            ["--format", "json"],
            client=client,
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        matching = [
            w for w in payload["metadata"]["warnings"]
            if w["code"] == HISTORY_WARNING_STORE_UNREADABLE
        ]
        assert len(matching) == 1
        assert str(path) in matching[0]["message"]
        assert f"mv '{path}'" in matching[0]["message"]
        assert [c["readable"] for c in payload["metadata"]["coverage"] if c["gameweek"] == 1] == [False]
        # The gameweek holds rows that simply would not parse, so the gap
        # hint that would send the user at `--backfill-detail` is wrong: it
        # skips an unreadable gameweek rather than repairing one.
        assert not [
            w for w in payload["metadata"]["warnings"]
            if w["code"] == HISTORY_WARNING_COVERAGE and DETAIL_FLAG in w["message"]
        ]

    def test_a_backfill_manager_fetch_failure_reaches_metadata_warnings(self):
        """Finding #10: `_coarse_backfill`'s manager-history-fetch-failure
        diagnostic used to go straight to `error_console.print()`, bypassing
        `_warn()` -- so under `--format json`, where the whole capture call
        is wrapped in `error_console.capture()` with no `as` binding, that
        detail was silently discarded with no replacement in the payload.
        It must now reach `metadata.warnings` like every other diagnostic."""
        client = _fpl_client()
        client.get_gameweeks = AsyncMock(return_value=[
            {"id": 4, "finished": True}, {"id": 5, "finished": True},
        ])
        client.get_manager_history = AsyncMock(side_effect=RuntimeError("boom"))

        result = _invoke_recap(
            _recap_data(managers=[_manager(name="Alice", entry_id=1)]),
            ["--format", "json"],
            client=client,
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        matching = [
            w for w in payload["metadata"]["warnings"]
            if w["code"] == HISTORY_WARNING_BACKFILL_MANAGER_UNREACHABLE
        ]
        assert len(matching) == 1
        assert "Alice" in matching[0]["message"]

    def test_metadata_coverage_reflects_the_tiers_actually_present(self):
        result = _invoke_recap(
            _recap_data(managers=[_manager(name="Alice", entry_id=1)]), ["--format", "json"],
        )

        payload = json.loads(result.stdout)
        coverage = payload["metadata"]["coverage"]
        assert any(c["gameweek"] == 5 and c["tier_counts"].get("detailed") == 1 for c in coverage)

    def test_multiple_managers_and_gameweeks_all_serialize_into_the_payload(self):
        """Exercises the serialization loops (`_serialize_coverage`,
        `_serialize_notes_pack`, the per-manager payload list) over more
        than one item each, not just the single-manager/single-gameweek
        happy path the other tests use."""
        store = _store(fpl_format="classic", league_id=42)
        for gw in (1, 2, 3, 4):
            store.append_rows(gw, [
                make_history_row(
                    season=SEASON, fpl_format="classic", league_id=42, gameweek=gw,
                    manager_key=1, manager_name="Alice", league_position=1, gw_rank=1,
                ),
                make_history_row(
                    season=SEASON, fpl_format="classic", league_id=42, gameweek=gw,
                    manager_key=2, manager_name="Bob", league_position=2, gw_rank=2,
                ),
            ])

        result = _invoke_recap(_recap_data(
            gameweek=5,
            managers=[
                _manager(name="Alice", entry_id=1, overall_rank=1, previous_rank=1, total_points=300),
                _manager(name="Bob", entry_id=2, overall_rank=2, previous_rank=2, total_points=280),
            ],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 50, 280)),
        ), ["--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)

        manager_names = {row["manager_name"] for row in payload["data"]}
        assert manager_names == {"Alice", "Bob"}

        coverage_gameweeks = {c["gameweek"] for c in payload["metadata"]["coverage"]}
        assert coverage_gameweeks == {1, 2, 3, 4, 5}

        pack = payload["metadata"]["notes_pack"]
        streak_managers = {entry["manager_name"] for entry in pack["entries"]}
        assert "Alice" in streak_managers

        # Season counts ride the same pack (issue #164): Alice topped GW1-4
        # and again in the recapped GW5, so her count is 5 -- exactly the
        # step weeks-on-top fires on, so it carries rendering surfaces this
        # gameweek. Bob's own counts are below their rules' thresholds and
        # reach the payload with none, which is what KTD8 asks of `--format
        # json`: the whole set, surfaced or not.
        alice_top = next(
            entry for entry in pack["season_count_entries"]
            if entry["manager_name"] == "Alice" and entry["condition_key"] == "weeks_on_top"
        )
        assert alice_top["occurrences"] == 5
        assert alice_top["surfaces"] == ["prompt", "report"]

        assert any(
            entry["manager_name"] == "Bob" and entry["surfaces"] == []
            for entry in pack["season_count_entries"]
        )

    def test_a_partial_coverage_run_reports_tiers_and_unknowns_per_gameweek(self):
        """U11's own Definition of Done row: the payload parses cleanly on a
        partial-coverage run too, with manager data still present -- a mix
        of coarse and unknown-status gameweeks alongside detailed ones."""
        store = _store(fpl_format="classic", league_id=42)
        store.append_rows(1, [make_history_row(
            season=SEASON, fpl_format="classic", league_id=42, gameweek=1,
            manager_key=1, manager_name="Alice", tier="coarse",
        )])
        store.append_rows(2, [make_history_row(
            season=SEASON, fpl_format="classic", league_id=42, gameweek=2,
            manager_key=1, manager_name="Alice", capture_status="unknown",
        )])

        result = _invoke_recap(
            _recap_data(managers=[_manager(name="Alice", entry_id=1)]), ["--format", "json"],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert {row["manager_name"] for row in payload["data"]} == {"Alice"}

        by_gw = {c["gameweek"]: c for c in payload["metadata"]["coverage"]}
        assert by_gw[1]["tier_counts"] == {"coarse": 1}
        assert by_gw[2]["unknown_count"] == 1
        assert by_gw[5]["tier_counts"] == {"detailed": 1}

    def test_dry_run_json_includes_the_editorial(self):
        result = _invoke_recap(_recap_data(), ["--format", "json", "--dry-run"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["metadata"]["synthesis_summary"]

    def test_save_writes_the_report_and_still_emits_the_payload(self, tmp_path: Path):
        result = _invoke_recap(
            _recap_data(), ["--format", "json", "--save", "--output", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert isinstance(payload["data"], list)
        # An explicit --output is season-partitioned too (#85), so a new
        # season's GW21 recap cannot land on the previous season's file.
        assert list((tmp_path / season_label()).glob("*.md"))
        assert not list(tmp_path.glob("*.md"))

    def test_an_unresolved_gameweek_emits_the_json_error_path_and_exits_one(self):
        result = _invoke_recap_unresolved_gameweek(["--format", "json"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["command"] == "league-recap"
        assert "error" in payload
        # #141: stdout carries the envelope and only the envelope; the prose
        # explaining the failure stays on stderr.
        assert "{" not in result.stderr

    def test_an_unresolved_gameweek_in_table_mode_exits_zero(self):
        """The deliberate divergence R9/R10 name: `emit_json_error` is the
        shared JSON contract (always exit 1), but the table path keeps its
        own softer exit-0 message-and-return behaviour."""
        result = _invoke_recap_unresolved_gameweek()

        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# U12: League History prompt section, through the full command
# ---------------------------------------------------------------------------


class TestSeasonFinesSurfaces:
    """The tally reaches console, report, prompt and `--format json`
    together, or the recap can only ever talk about this week (issue #136)."""

    def _fined_data(self, gameweek: int = 5) -> LeagueRecapData:
        return _recap_data(
            gameweek=gameweek,
            managers=[
                _manager(name="Alice", entry_id=1, gross_points=60),
                _manager(name="Bob", entry_id=2, gross_points=10, gw_rank=2, overall_rank=2),
            ],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 10, 200)),
        )

    def _run(self, gameweek: int, args: list[str] | None = None):
        return _invoke_recap(
            self._fined_data(gameweek), args, client=_fpl_client(gameweek),
            settings=_LAST_PLACE_ONLY, gw=gameweek,
        )

    def test_the_console_prints_season_totals_at_the_halfway_boundary(self):
        result = self._run(CHIP_SPLIT_GW)

        assert result.exit_code == 0, result.output
        output = result.output.replace("\n", "")
        assert "Season Fines" in output
        assert "Bob: 1 (1 last-place)" in output

    def test_manager_names_in_the_season_block_are_not_read_as_markup(self):
        """Both the per-manager lines and the qualifiers beneath them carry
        names, which are user-supplied (#165 review)."""
        data = _recap_data(
            gameweek=CHIP_SPLIT_GW,
            managers=[
                _manager(name="Alice", entry_id=1, gross_points=60),
                _manager(name="[b]B[/b]", entry_id=2, gross_points=10, gw_rank=2, overall_rank=2),
            ],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "[b]B[/b]", 2, 10, 200)),
        )

        result = _invoke_recap(
            data, client=_fpl_client(CHIP_SPLIT_GW), settings=_LAST_PLACE_ONLY,
            gw=CHIP_SPLIT_GW,
        )

        assert result.exit_code == 0, result.output
        assert "[b]B[/b]: 1 (1 last-place)" in result.output.replace("\n", "")

    def test_the_finale_prints_them_too(self):
        result = self._run(TOTAL_GAMEWEEKS)

        assert result.exit_code == 0, result.output
        assert "Season Fines" in result.output.replace("\n", "")

    def test_an_ordinary_gameweek_prints_no_season_table(self):
        """The printed table is a set-piece: showing it all 38 weeks would
        turn it into wallpaper, and `fpl league-fines` answers the season
        question on demand in between. The editorial still gets the totals --
        see `TestEndToEndPromptThroughTheFullCommand`."""
        result = self._run(5)

        assert result.exit_code == 0, result.output
        output = result.output.replace("\n", "")
        assert "Season Fines" not in output
        assert "Fines:" in output, "this gameweek's own fines still print"

    def test_a_league_with_no_fine_rules_gets_no_season_section(self):
        result = _invoke_recap(
            _recap_data(gameweek=CHIP_SPLIT_GW), client=_fpl_client(CHIP_SPLIT_GW),
            gw=CHIP_SPLIT_GW,
        )

        assert result.exit_code == 0, result.output
        assert "Season Fines" not in result.output

    def test_the_json_metadata_carries_the_tally_on_an_ordinary_gameweek_too(self):
        """A consumer polling `--format json` weekly must not have the season
        table appear and disappear on a calendar it cannot see (KTD8)."""
        result = self._run(5, ["--format", "json"])

        assert result.exit_code == 0, result.output
        tally = json.loads(result.stdout)["metadata"]["season_fines"]
        assert tally["rule_types"] == ["last-place"]
        assert tally["total_fines"] == 1
        assert tally["through_gameweek"] == 5
        bob = next(m for m in tally["managers"] if m["manager_name"] == "Bob")
        assert bob["counts"] == {"last-place": 1}
        assert bob["fined_gameweeks"] == [5]
        assert tally["qualifiers"]

    def test_a_saved_report_carries_the_table_only_at_a_milestone(self, tmp_path: Path):
        """The report is the surface that gets shared, so its gate matters
        most: a full season table every week is exactly the wallpaper the
        milestone rule exists to avoid."""
        milestone = _invoke_recap(
            self._fined_data(CHIP_SPLIT_GW), ["--save", "--output", str(tmp_path)],
            client=_fpl_client(CHIP_SPLIT_GW), settings=_LAST_PLACE_ONLY, gw=CHIP_SPLIT_GW,
        )
        assert milestone.exit_code == 0, milestone.output
        at_milestone = (
            tmp_path / season_label() / f"gw{CHIP_SPLIT_GW}-league-recap.md"
        ).read_text(encoding="utf-8")

        ordinary = _invoke_recap(
            self._fined_data(5), ["--save", "--output", str(tmp_path)],
            settings=_LAST_PLACE_ONLY,
        )
        assert ordinary.exit_code == 0, ordinary.output
        at_ordinary = (
            tmp_path / season_label() / "gw5-league-recap.md"
        ).read_text(encoding="utf-8")

        assert "# Season Fines" in at_milestone
        assert "Bob: 1 (1 last-place)" in at_milestone
        assert "# Season Fines" not in at_ordinary
        assert "# Fines" in at_ordinary, "this gameweek's own fines still render"

    def test_the_prompt_section_reads_as_a_season_table(self):
        from fpl_cli.prompts.league_recap import format_recap_season_fines_context
        from fpl_cli.services.league_history_fines import build_season_fines_tally

        self._run(CHIP_SPLIT_GW)
        tally = build_season_fines_tally(_store(), CHIP_SPLIT_GW, rule_types=["last-place"])

        text = format_recap_season_fines_context(tally)

        assert f"Season fine totals, GW{CHIP_SPLIT_GW} through GW{CHIP_SPLIT_GW}" in text
        assert f"- Bob: 1 (1 last-place; fined in GW{CHIP_SPLIT_GW})" in text
        assert "Not fined so far: Alice" in text
        assert "Coverage:" in text

    def test_the_prompt_section_is_empty_without_configured_rules(self):
        from fpl_cli.prompts.league_recap import format_recap_season_fines_context
        from fpl_cli.services.league_history_fines import build_season_fines_tally

        _invoke_recap(_recap_data())

        assert format_recap_season_fines_context(
            build_season_fines_tally(_store(), 5, rule_types=[]),
        ) == ""

    async def test_the_report_renders_a_season_fines_section(self, tmp_path: Path):
        from fpl_cli.agents.orchestration.report import ReportAgent

        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = dict(_recap_data(managers=[_manager(name="Alice", overall_rank=1, previous_rank=1)]))
        data["season_fines_span"] = "GW1-GW5"
        data["season_fines_lines"] = ["Bob: 2 (2 last-place)", "Alice: none"]
        data["season_fines_coverage_lines"] = ["GW3 was never captured, so no fine was ruled there."]

        result = await agent.run(context={"report_type": "league-recap", "gameweek": 5, "data": data})

        content = Path(result.data["report_path"]).read_text(encoding="utf-8")
        assert "# Season Fines" in content
        assert "GW1-GW5" in content
        assert "- Bob: 2 (2 last-place)" in content
        assert "- Alice: none" in content
        assert "GW3 was never captured" in content

    async def test_no_season_fines_section_when_the_tally_has_nothing_to_show(self, tmp_path: Path):
        from fpl_cli.agents.orchestration.report import ReportAgent

        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = dict(_recap_data(managers=[_manager(name="Alice", overall_rank=1, previous_rank=1)]))

        result = await agent.run(context={"report_type": "league-recap", "gameweek": 5, "data": data})

        content = Path(result.data["report_path"]).read_text(encoding="utf-8")
        assert "# Season Fines" not in content


class TestEndToEndPromptThroughTheFullCommand:
    """U12's own verification criterion: a dry run against a seeded
    multi-gameweek partition writes a prompt containing the League History
    section, its rules, and the season-phase instruction."""

    def test_a_dry_run_writes_a_prompt_with_league_history(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)  # debug prompts land in ./data/debug (cwd-relative, pre-existing)

        store = _store(fpl_format="classic", league_id=42)
        for gw in (1, 2, 3, 4):
            store.append_rows(gw, [make_history_row(
                season=SEASON, fpl_format="classic", league_id=42, gameweek=gw,
                manager_key=1, manager_name="Alice", league_position=1, gw_rank=1,
            )])

        result = _invoke_recap(_recap_data(
            gameweek=5,
            managers=[_manager(name="Alice", entry_id=1, overall_rank=1, previous_rank=1, total_points=300)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        ), ["--dry-run"])

        assert result.exit_code == 0, result.output
        system_prompt = (tmp_path / "data" / "debug" / "recap_system.txt").read_text(encoding="utf-8")
        user_prompt = (tmp_path / "data" / "debug" / "recap_prompt.txt").read_text(encoding="utf-8")

        assert "## League History" in user_prompt
        assert "Alice" in user_prompt
        assert "gameweeks on top of the league" in user_prompt
        assert "Season phase:" in user_prompt
        assert "Stick to what happened this gameweek, with one exception" in system_prompt
        assert "season phase" in system_prompt.lower()

    def _fined_at(self, gameweek: int) -> LeagueRecapData:
        return _recap_data(
            gameweek=gameweek,
            managers=[
                _manager(name="Alice", entry_id=1, gross_points=60),
                _manager(name="Bob", entry_id=2, gross_points=10, gw_rank=2, overall_rank=2),
            ],
            cohort=_cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 10, 200)),
        )

    def test_a_milestone_dry_run_writes_the_season_fines_section_and_its_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """The narrative can only reference season totals it was handed, and
        only in the section's own wording (issue #136)."""
        monkeypatch.chdir(tmp_path)

        result = _invoke_recap(
            self._fined_at(CHIP_SPLIT_GW), ["--dry-run"],
            client=_fpl_client(CHIP_SPLIT_GW), settings=_LAST_PLACE_ONLY, gw=CHIP_SPLIT_GW,
        )

        assert result.exit_code == 0, result.output
        system_prompt = (tmp_path / "data" / "debug" / "recap_system.txt").read_text(encoding="utf-8")
        user_prompt = (tmp_path / "data" / "debug" / "recap_prompt.txt").read_text(encoding="utf-8")

        assert "## Season Fines" in user_prompt
        assert f"Bob: 1 (1 last-place; fined in GW{CHIP_SPLIT_GW})" in user_prompt
        assert "NEVER add up fines yourself" in system_prompt
        # This fixture's ledger holds only the gameweek under recap, so the
        # ordinal the model kept inventing cannot be asserted -- and the
        # section refuses it outright rather than guessing (issue #233).
        assert "Do not number this fine." in user_prompt

    def test_an_ordinary_gameweek_still_hands_the_model_season_totals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """The milestone gate covers the printed table, not the prompt: a
        table every week is wallpaper, but a sentence every week is what
        makes a recap feel like it has a memory -- and the model can only
        write one for totals it was actually handed."""
        monkeypatch.chdir(tmp_path)

        result = _invoke_recap(
            self._fined_at(5), ["--dry-run"], settings=_LAST_PLACE_ONLY,
        )

        assert result.exit_code == 0, result.output
        system_prompt = (tmp_path / "data" / "debug" / "recap_system.txt").read_text(encoding="utf-8")
        user_prompt = (tmp_path / "data" / "debug" / "recap_prompt.txt").read_text(encoding="utf-8")

        assert "## Season Fines" in user_prompt
        assert "Bob: 1 (1 last-place; fined in GW5)" in user_prompt
        assert "the ledger records 1 last-place fine against Bob" in user_prompt
        assert "Do not number this fine." in user_prompt
        # ...and it is offered, never demanded.
        assert "optional colour, not a required beat" in system_prompt

    def test_the_ordinary_gameweek_prompt_is_not_matched_by_the_printed_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """The two surfaces genuinely diverge on an ordinary gameweek: the
        model is told, the console is not."""
        monkeypatch.chdir(tmp_path)

        result = _invoke_recap(
            self._fined_at(5), ["--dry-run"], settings=_LAST_PLACE_ONLY,
        )

        user_prompt = (tmp_path / "data" / "debug" / "recap_prompt.txt").read_text(encoding="utf-8")
        assert "## Season Fines" in user_prompt
        assert "Season Fines" not in result.output.replace("\n", "")

    def test_no_season_fines_section_without_configured_rules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """The one case that still omits the section from the prompt: a
        league with no fine rules configured and none ever ruled."""
        monkeypatch.chdir(tmp_path)

        result = _invoke_recap(
            _recap_data(gameweek=CHIP_SPLIT_GW), ["--dry-run"],
            client=_fpl_client(CHIP_SPLIT_GW), gw=CHIP_SPLIT_GW,
        )

        assert result.exit_code == 0, result.output
        user_prompt = (tmp_path / "data" / "debug" / "recap_prompt.txt").read_text(encoding="utf-8")
        assert "## Season Fines" not in user_prompt


# ---------------------------------------------------------------------------
# issue #169: a replay must not restamp a row with today's identity
# ---------------------------------------------------------------------------


class TestReplayKeepsRecordedIdentity:
    """A backfill resolves every pick against today's bootstrap, so a player
    transferred or renamed since comes back wearing his current club on a row
    describing a gameweek where that was not true."""

    def _cohort(self):
        return _cohort((1, "Alice", 1, 60, 300), (2, "Bob", 2, 40, 280))

    def _data(self, squad, *, gameweek: int = 1, **manager_kwargs):
        return _recap_data(
            gameweek=gameweek,
            managers=[_manager(name="Alice", entry_id=1, squad=squad, **manager_kwargs)],
            cohort=self._cohort(),
        )

    async def _replay(self, squad, *, warnings_out=None, **manager_kwargs):
        """Capture GW1 with `squad`, leaving Bob unknown so GW1 stays
        incomplete and every later run replays the whole cohort."""
        replayed = self._data(squad, **manager_kwargs)

        async def _replay_gw(gameweek: int):
            return replayed if gameweek == 1 else None

        result = await capture_recap_history(
            self._data([_player()]), season=SEASON, finished_gameweeks=[1],
            replay_gameweek=_replay_gw, backfill_detail=False,
        )
        if warnings_out is not None:
            warnings_out.extend(result.warnings)
        return _store().resolved_gameweek(1)

    async def _capture_original(self, squad, **manager_kwargs):
        await capture_recap_history(
            self._data(squad, **manager_kwargs), season=SEASON, finished_gameweeks=[1],
        )

    async def test_a_replay_keeps_the_club_the_gameweek_recorded(self):
        await self._capture_original(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
        )
        resolved = await self._replay(
            [_player(name="Mover", code=510_281, team="TOT", is_captain=True)],
        )
        assert resolved[1].squad[0].team == "MCI"

    async def test_a_replay_keeps_the_name_the_gameweek_recorded(self):
        await self._capture_original(
            [_player(name="Savinho", code=510_281, team="MCI", is_captain=True)],
            captain="Savinho",
        )
        resolved = await self._replay(
            [_player(name="Sávio", code=510_281, team="MCI", is_captain=True)],
            captain="Sávio",
        )
        assert resolved[1].squad[0].name == "Savinho"
        assert resolved[1].captain.name == "Savinho", "captaincy must follow the squad's rename"

    async def test_a_gameweek_an_earlier_replay_already_degraded_is_repaired(self):
        """The property that needs the store read to be every line rather than
        the resolved winner: by now the degraded row is the winner, so carrying
        *that* forward would just re-copy the mistake."""
        await self._capture_original(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
        )
        await self._replay([_player(name="Mover", code=510_281, team="TOT", is_captain=True)])
        assert _store().resolved_gameweek(1)[1].squad[0].team == "MCI"

        # A second replay, still wrong, still repaired.
        resolved = await self._replay(
            [_player(name="Mover", code=510_281, team="TOT", is_captain=True)],
        )
        assert resolved[1].squad[0].team == "MCI"

    async def test_a_replay_never_lowers_a_resolved_code(self):
        await self._capture_original(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
            captain="Mover",
        )
        resolved = await self._replay(
            [_player(name="Mover", code=None, team="MCI", is_captain=True, unmatched=True)],
            captain="Mover",
        )
        recorded = resolved[1].squad[0]
        assert recorded.code == 510_281, "the cross-season join key survives a drifted bootstrap"
        assert recorded.unmatched is True, (
            "restoring a code does not make this replay's zero a real score"
        )
        assert resolved[1].captain.code == 510_281, (
            "a captaincy entry is a value copy, not a reference into the squad"
        )

    async def test_restoring_a_code_is_reported_even_when_nothing_else_moved(self):
        """The repair the issue asks to be warned about: name and club agree, so
        only the recovered join key marks that the bootstrap has drifted."""
        await self._capture_original(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
        )
        warnings: list[dict[str, str]] = []
        await self._replay(
            [_player(name="Mover", code=None, team="MCI", is_captain=True, unmatched=True)],
            warnings_out=warnings,
        )
        assert any(w["code"] == HISTORY_WARNING_IDENTITY_CARRIED for w in warnings)

    async def test_what_the_gameweek_re_derives_is_left_to_the_replay(self):
        """Points, cards and the blank gate come from the gameweek's own data,
        so a replay that improves them must win -- only who a player *was* is
        carried."""
        await self._capture_original([
            _player(name="Mover", code=510_281, team="MCI", points=2,
                    is_captain=True, had_fixture=True, red_cards=0),
        ])
        resolved = await self._replay([
            _player(name="Mover", code=510_281, team="TOT", points=9,
                    is_captain=True, had_fixture=False, red_cards=1),
        ])
        player = resolved[1].squad[0]
        assert (player.points, player.had_fixture, player.red_cards) == (9, False, 1)
        assert player.team == "MCI"

    async def test_a_player_who_has_left_the_game_does_not_strand_the_rest(self):
        """The collectors skip a pick today's bootstrap cannot resolve, so a
        replayed squad can be shorter. Slot alignment is off; the code join
        still carries everyone it can reach."""
        await self._capture_original([
            _player(name="Gone", code=111, team="LIV"),
            _player(name="Mover", code=510_281, team="MCI", is_captain=True),
        ])
        resolved = await self._replay(
            [_player(name="Mover", code=510_281, team="TOT", is_captain=True)],
        )
        assert [(p.name, p.team) for p in resolved[1].squad] == [("Mover", "MCI")]

    async def test_a_transfer_keeps_the_clubs_the_gameweek_recorded(self):
        await self._capture_original(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
            transfers=[RecapTransfer(
                player_in="Mover", player_in_team="MCI", player_in_points=6,
                player_in_code=510_281,
                player_out="Other", player_out_team="LIV", player_out_points=1,
                player_out_code=222,
                net=5, cost=0,
            )],
        )
        resolved = await self._replay(
            [_player(name="Mover", code=510_281, team="TOT", is_captain=True)],
            transfers=[RecapTransfer(
                player_in="Mover", player_in_team="TOT", player_in_points=6,
                player_in_code=510_281,
                player_out="Other", player_out_team="EVE", player_out_points=1,
                player_out_code=222,
                net=5, cost=0,
            )],
        )
        transfer = resolved[1].transfers[0]
        assert (transfer.player_in_team, transfer.player_out_team) == ("MCI", "LIV")

    async def test_carrying_identity_forward_is_reported(self):
        await self._capture_original(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
        )
        warnings: list[dict[str, str]] = []
        await self._replay(
            [_player(name="Mover", code=510_281, team="TOT", is_captain=True)],
            warnings_out=warnings,
        )
        assert any(w["code"] == HISTORY_WARNING_IDENTITY_CARRIED for w in warnings)

    async def test_a_replay_that_agrees_with_the_record_says_nothing(self):
        await self._capture_original(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
        )
        warnings: list[dict[str, str]] = []
        await self._replay(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
            warnings_out=warnings,
        )
        assert not any(w["code"] == HISTORY_WARNING_IDENTITY_CARRIED for w in warnings)

    async def test_a_gameweek_with_nothing_recorded_yet_is_left_alone(self):
        """A first fill and a coarse upgrade have no recorded squad to carry,
        so the replay's own values stand."""
        resolved = await self._replay(
            [_player(name="Mover", code=510_281, team="TOT", is_captain=True)],
        )
        assert resolved[1].squad[0].team == "TOT"

    async def test_a_player_moved_out_is_renamed_too(self):
        """A sold player is by definition absent from the squad, so the squad
        pairing can never reach him -- the move lists have to pair themselves."""
        await self._capture_original(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
            transfers=[RecapTransfer(
                player_in="Mover", player_in_team="MCI", player_in_points=6,
                player_in_code=510_281,
                player_out="Savinho", player_out_team="MCI", player_out_points=1,
                player_out_code=222,
                net=5, cost=0,
            )],
        )
        resolved = await self._replay(
            [_player(name="Mover", code=510_281, team="TOT", is_captain=True)],
            transfers=[RecapTransfer(
                player_in="Mover", player_in_team="TOT", player_in_points=6,
                player_in_code=510_281,
                player_out="Sávio", player_out_team="EVE", player_out_points=1,
                player_out_code=222,
                net=5, cost=0,
            )],
        )
        transfer = resolved[1].transfers[0]
        assert (transfer.player_out, transfer.player_out_team) == ("Savinho", "MCI")

    async def test_a_move_never_lowers_a_resolved_code(self):
        """The same guarantee the squad gets: a move whose code the replay lost
        keeps the cross-season join key the gameweek resolved."""
        await self._capture_original(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
            transfers=[RecapTransfer(
                player_in="Mover", player_in_team="MCI", player_in_points=6,
                player_in_code=510_281,
                player_out="Other", player_out_team="LIV", player_out_points=1,
                player_out_code=222,
                net=5, cost=0,
            )],
        )
        resolved = await self._replay(
            [_player(name="Mover", code=510_281, team="MCI", is_captain=True)],
            transfers=[RecapTransfer(
                player_in="Mover", player_in_team="MCI", player_in_points=6,
                player_in_code=510_281,
                player_out="Other", player_out_team="LIV", player_out_points=1,
                net=5, cost=0,
            )],
        )
        assert resolved[1].transfers[0].player_out_code == 222


# ---------------------------------------------------------------------------
# issue #178: a current-gameweek recapture must not restamp a recorded row
# ---------------------------------------------------------------------------


class TestLiveCaptureKeepsRecordedIdentity:
    """`capture_recap_history`'s direct write path -- no `replay_gameweek`
    involved -- is what the ordinary `league-recap` command runs every time.
    A gameweek stays "current" (and so keeps landing on this path) for the
    whole window between its last fixture and the next deadline, and FPL
    keeps processing transfers throughout that window (issue #178)."""

    def _data(self, squad, *, gameweek: int = 1, **manager_kwargs):
        return _recap_data(
            gameweek=gameweek,
            managers=[_manager(name="Alice", entry_id=1, squad=squad, **manager_kwargs)],
            cohort=_cohort((1, "Alice", 1, 60, 300)),
        )

    async def test_a_recapture_of_a_finished_gameweek_keeps_the_recorded_club(self):
        await capture_recap_history(
            self._data([_player(name="Mover", code=510_281, team="MCI", is_captain=True)]),
            season=SEASON, finished_gameweeks=[1],
        )
        await capture_recap_history(
            self._data([_player(name="Mover", code=510_281, team="TOT", is_captain=True)]),
            season=SEASON, finished_gameweeks=[1],
        )
        resolved = _store().resolved_gameweek(1)
        assert resolved[1].squad[0].team == "MCI"

    async def test_a_recapture_reports_the_carry(self):
        await capture_recap_history(
            self._data([_player(name="Mover", code=510_281, team="MCI", is_captain=True)]),
            season=SEASON, finished_gameweeks=[1],
        )
        result = await capture_recap_history(
            self._data([_player(name="Mover", code=510_281, team="TOT", is_captain=True)]),
            season=SEASON, finished_gameweeks=[1],
        )
        assert any(w["code"] == HISTORY_WARNING_IDENTITY_CARRIED for w in result.warnings)

    async def test_a_gameweek_not_yet_finished_is_not_gated_into_carrying(self):
        """Absent from `finished_gameweeks`, the gameweek is still being
        played, so its squad can legitimately change between captures -- the
        carry must not run."""
        await capture_recap_history(
            self._data([_player(name="Mover", code=510_281, team="MCI", is_captain=True)]),
            season=SEASON, finished_gameweeks=[],
        )
        await capture_recap_history(
            self._data([_player(name="Mover", code=510_281, team="TOT", is_captain=True)]),
            season=SEASON, finished_gameweeks=[],
        )
        resolved = _store().resolved_gameweek(1)
        assert resolved[1].squad[0].team == "TOT"

    async def test_a_first_capture_is_unaffected(self):
        result = await capture_recap_history(
            self._data([_player(name="Mover", code=510_281, team="TOT", is_captain=True)]),
            season=SEASON, finished_gameweeks=[1],
        )
        resolved = _store().resolved_gameweek(1)
        assert resolved[1].squad[0].team == "TOT"
        assert not any(w["code"] == HISTORY_WARNING_IDENTITY_CARRIED for w in result.warnings)


class TestPairSquads:
    """issue #175 review: which recorded entry each replayed one supersedes."""

    def _p(self, name: str, code: int | None) -> LedgerPlayer:
        return LedgerPlayer(name=name, team="MCI", position="MID", code=code)

    def test_slots_align_when_the_codes_they_share_agree(self):
        replayed = [self._p("A", 1), self._p("B", None)]
        recorded = [self._p("X", 1), self._p("Y", 2)]
        assert _pair_squads(replayed, recorded) == [
            (replayed[0], recorded[0]), (replayed[1], recorded[1]),
        ]

    def test_two_squads_sharing_no_code_at_all_do_not_align_on_hope(self):
        """`all()` over no comparisons is vacuously true, so equal length alone
        would otherwise stamp each recorded entry onto whichever slot happened
        to sit opposite it."""
        replayed = [self._p("A", None), self._p("B", None)]
        recorded = [self._p("X", 1), self._p("Y", 2)]
        assert _pair_squads(replayed, recorded) == []

    def test_a_single_unidentified_entry_is_still_reached_off_the_slot_path(self):
        """Codes disagree by slot, so pairing falls back to the codes -- and the
        one entry the replay could not identify has exactly one candidate
        left, which is enough to place it."""
        replayed = [self._p("A", 2), self._p("B", None)]
        recorded = [self._p("X", 1), self._p("Y", 2)]
        assert _pair_squads(replayed, recorded) == [
            (replayed[0], recorded[1]), (replayed[1], recorded[0]),
        ]

    def test_two_unidentified_entries_are_left_alone(self):
        replayed = [self._p("A", 3), self._p("B", None), self._p("C", None)]
        recorded = [self._p("X", 1), self._p("Y", 2), self._p("Z", 3)]
        assert _pair_squads(replayed, recorded) == [(replayed[0], recorded[2])]

    def test_a_player_gone_from_the_game_leaves_the_rest_paired_by_code(self):
        replayed = [self._p("B", 2)]
        recorded = [self._p("X", 1), self._p("Y", 2)]
        assert _pair_squads(replayed, recorded) == [(replayed[0], recorded[1])]
