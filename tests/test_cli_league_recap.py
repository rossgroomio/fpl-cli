"""Tests for league-recap history capture: row building, wiring, and warnings."""
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

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
