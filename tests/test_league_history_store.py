"""Tests for the league-history ledger row model and its durable store."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from fpl_cli.models.league_history import (
    LEAGUE_HISTORY_VERSION,
    CaptureStatus,
    FidelityTier,
    LeagueHistoryRow,
    LedgerCaptaincy,
    LedgerFine,
    LedgerPlayer,
    resolve_rows,
)
from tests.conftest import make_history_row

# ---------------------------------------------------------------------------
# U4: the row model
# ---------------------------------------------------------------------------


class TestLeagueHistoryRowModel:
    def test_row_round_trips_through_serialisation_unchanged(self):
        row = make_history_row(
            gross_points=64,
            transfer_cost=4,
            total_points=310,
            gw_rank=2,
            league_position=3,
            previous_league_position=5,
            captain=LedgerCaptaincy(name="Salah", code=118_748, points=12, played=True, had_fixture=True),
            vice_captain=LedgerCaptaincy(name="Saka", code=223_340, points=6),
            active_chip="bboost",
            bench_points=9,
            squad=[LedgerPlayer(name="Salah", team="LIV", position="MID", code=118_748, points=12)],
            fines=[LedgerFine(manager_key=1, rule_type="last-place", message="Pint on video")],
            gameweek_blank=False,
            gameweek_double=True,
        )
        restored = LeagueHistoryRow.model_validate_json(row.model_dump_json())
        assert restored == row

    def test_missing_key_field_raises_rather_than_defaulting_to_zero(self):
        payload = make_history_row().model_dump(mode="json")
        del payload["manager_key"]
        with pytest.raises(ValidationError):
            LeagueHistoryRow.model_validate(payload)

    def test_unknown_extra_field_is_rejected_not_silently_accepted(self):
        payload = make_history_row().model_dump(mode="json")
        payload["storyline_theme"] = "a field from a future version"
        with pytest.raises(ValidationError):
            LeagueHistoryRow.model_validate(payload)

    def test_unknown_status_row_validates_with_no_detail_at_all(self):
        row = make_history_row(capture_status="unknown", manager_name="Bob")
        assert row.capture_status is CaptureStatus.UNKNOWN
        assert row.gross_points is None
        assert row.captain is None
        assert row.squad == []
        # Still round-trips, so an unknown row survives a store rewrite.
        assert LeagueHistoryRow.model_validate_json(row.model_dump_json()) == row

    def test_unrecognised_format_is_rejected(self):
        with pytest.raises(ValidationError):
            make_history_row(fpl_format="h2h")

    def test_global_rank_and_league_position_are_separate_fields(self):
        row = make_history_row(global_rank=400_000, league_position=1)
        assert row.global_rank == 400_000
        assert row.league_position == 1

    def test_classic_row_carries_the_four_rollover_only_fields(self):
        row = make_history_row(
            team_value=1013, bank=7, global_rank=400_000, transfers_made=2,
        )
        assert (row.team_value, row.bank, row.global_rank, row.transfers_made) == (1013, 7, 400_000, 2)

    def test_draft_row_omits_all_four_and_still_validates(self):
        row = make_history_row(fpl_format="draft", manager_key=10, gross_points=51)
        assert row.team_value is None
        assert row.bank is None
        assert row.global_rank is None
        assert row.transfers_made is None
        assert row.gross_points == 51

    def test_transfer_shortfall_is_recorded_rather_than_implied(self):
        short = make_history_row(transfers_made=3, transfer_detail_shortfall=2)
        complete = make_history_row(transfers_made=1, transfer_detail_shortfall=0)
        none_made = make_history_row(transfers_made=0, transfer_detail_shortfall=0)
        assert short.transfer_detail_shortfall == 2
        assert complete.transfer_detail_shortfall == 0
        assert none_made.transfer_detail_shortfall == 0

    def test_rows_are_stamped_with_the_current_schema_version(self):
        assert make_history_row().version == LEAGUE_HISTORY_VERSION

    def test_content_ignores_the_capture_timestamp(self):
        early = make_history_row(captured_at=datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc))
        late = make_history_row(captured_at=datetime(2026, 8, 22, 18, 0, tzinfo=timezone.utc))
        assert early.content() == late.content()
        assert early != late

    def test_content_notices_a_changed_value(self):
        assert make_history_row(gross_points=50).content() != make_history_row(gross_points=51).content()


class TestRowResolution:
    """R3: highest fidelity tier, then latest capture; unknown ranks below all."""

    def _at(self, hour: int) -> datetime:
        return datetime(2026, 8, 22, hour, tzinfo=timezone.utc)

    def test_detailed_beats_coarse_even_when_written_earlier(self):
        coarse = make_history_row(tier="coarse", captured_at=self._at(18), gross_points=40)
        detailed = make_history_row(tier="detailed", captured_at=self._at(9), gross_points=41)
        assert resolve_rows([coarse, detailed])[1] is detailed

    def test_latest_capture_wins_within_one_tier(self):
        first = make_history_row(captured_at=self._at(9), gross_points=40)
        second = make_history_row(captured_at=self._at(18), gross_points=44)
        assert resolve_rows([first, second])[1] is second

    def test_any_tier_supersedes_an_unknown_row_written_later(self):
        unknown = make_history_row(capture_status="unknown", captured_at=self._at(18))
        coarse = make_history_row(tier="coarse", captured_at=self._at(9), gross_points=40)
        assert resolve_rows([unknown, coarse])[1] is coarse

    def test_each_manager_resolves_independently(self):
        alice = make_history_row(manager_key=1, gross_points=40)
        bob_unknown = make_history_row(manager_key=2, capture_status="unknown")
        bob_known = make_history_row(manager_key=2, captured_at=self._at(20), gross_points=33)
        winners = resolve_rows([alice, bob_unknown, bob_known])
        assert winners[1] is alice
        assert winners[2] is bob_known

    def test_an_unknown_row_still_records_the_tier_it_attempted(self):
        row = make_history_row(capture_status="unknown", tier="coarse")
        assert row.tier is FidelityTier.COARSE


# ---------------------------------------------------------------------------
# U5: the store
# ---------------------------------------------------------------------------


class TestStoreFailsClosed:
    """The inverse of `tests/test_cli_chips.py::test_load_corrupt_file_returns_empty`.

    A chip plan resets on a corrupt file because it can be rebuilt from the
    API. A ledger gameweek cannot -- the API destroys per-gameweek granularity
    at the rollover -- so it refuses to guess (R4).
    """

    def test_ae4_malformed_line_raises_and_leaves_the_file_untouched(self):
        from fpl_cli.services.league_history import LeagueHistoryError, LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, gross_points=50)])
        path = store.gameweek_file(5)
        path.write_text(path.read_text(encoding="utf-8") + "not json{{{\n", encoding="utf-8")
        before = path.read_bytes()

        with pytest.raises(LeagueHistoryError) as exc:
            store.load_gameweek(5)

        assert str(path) in str(exc.value)
        assert "move" in str(exc.value).lower()
        assert path.read_bytes() == before

    def test_a_corrupt_gameweek_does_not_take_the_partition_with_it(self):
        from fpl_cli.services.league_history import LeagueHistoryError, LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, gross_points=50)])
        store.append_rows(6, [make_history_row(gameweek=6, gross_points=60)])
        store.gameweek_file(5).write_text("not json{{{\n", encoding="utf-8")

        assert store.load_gameweek(6)[0].gross_points == 60
        with pytest.raises(LeagueHistoryError):
            store.load_gameweek(5)

        coverage = {c.gameweek: c for c in store.coverage()}
        assert coverage[5].readable is False
        assert coverage[6].readable is True

    def test_a_write_onto_a_corrupt_gameweek_refuses_rather_than_overwriting(self):
        from fpl_cli.services.league_history import LeagueHistoryError, LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        path = store.gameweek_file(5)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{\n", encoding="utf-8")
        before = path.read_bytes()

        with pytest.raises(LeagueHistoryError):
            store.append_rows(5, [make_history_row(gameweek=5, gross_points=50)])
        assert path.read_bytes() == before


class TestStoreAppendAndSupersession:
    def test_rows_read_back_as_written(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        rows = [
            make_history_row(gameweek=5, manager_key=1, gross_points=50),
            make_history_row(gameweek=5, manager_key=2, gross_points=44),
        ]
        store.append_rows(5, rows)
        assert store.load_gameweek(5) == rows

    def test_an_identical_row_writes_nothing(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, gross_points=50)])
        before = store.gameweek_file(5).read_bytes()

        written = store.append_rows(
            5,
            [make_history_row(
                gameweek=5, gross_points=50,
                captured_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
            )],
        )

        assert written == []
        assert store.gameweek_file(5).read_bytes() == before

    def test_a_differing_row_appends_a_superseding_line(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, gross_points=50)])
        later = make_history_row(
            gameweek=5, gross_points=53,
            captured_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        )
        store.append_rows(5, [later])

        assert len(store.load_gameweek(5)) == 2
        assert store.resolved_gameweek(5)[1].gross_points == 53

    def test_a_detailed_row_supersedes_a_coarse_one_without_removing_it(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, tier="coarse", gross_points=50)])
        store.append_rows(5, [make_history_row(gameweek=5, tier="detailed", gross_points=50)])

        rows = store.load_gameweek(5)
        assert [r.tier.value for r in rows] == ["coarse", "detailed"]
        assert store.resolved_gameweek(5)[1].tier.value == "detailed"

    def test_a_coarse_repair_row_never_downgrades_an_already_detailed_manager(self):
        """A repair pass that revisits the whole cohort -- because some *other*
        manager in the gameweek is still unknown -- must not re-append a
        lower-tier duplicate for a manager who is already captured at a higher
        tier, even though the repair row's content differs (R3). Otherwise the
        file grows on every single run for the lifetime of the other manager's
        failure."""
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, tier="detailed", gross_points=50)])
        before = store.gameweek_file(5).read_bytes()

        written = store.append_rows(5, [
            make_history_row(gameweek=5, tier="coarse", gross_points=99),
        ])

        assert written == []
        assert store.gameweek_file(5).read_bytes() == before
        assert store.resolved_gameweek(5)[1].gross_points == 50

    def test_any_capture_supersedes_an_unknown_row(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, capture_status="unknown")])
        store.append_rows(5, [make_history_row(gameweek=5, tier="coarse", gross_points=50)])

        assert store.resolved_gameweek(5)[1].gross_points == 50

    def test_a_multi_manager_multi_gameweek_write_keeps_every_row(self):
        """Two iterations in both loops: the shape that has bitten this area before."""
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (5, 6):
            store.append_rows(gw, [
                make_history_row(gameweek=gw, manager_key=key, gross_points=gw * 10 + key)
                for key in (1, 2)
            ])

        assert sorted(store.captured_gameweeks()) == [5, 6]
        for gw in (5, 6):
            resolved = store.resolved_gameweek(gw)
            assert sorted(resolved) == [1, 2]
            assert [resolved[k].gross_points for k in (1, 2)] == [gw * 10 + 1, gw * 10 + 2]

    def test_a_batch_carrying_two_rows_for_one_manager_keeps_both(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [
            make_history_row(gameweek=5, tier="coarse", gross_points=50),
            make_history_row(
                gameweek=5, tier="detailed", gross_points=50,
                captured_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
            ),
        ])
        assert len(store.load_gameweek(5)) == 2
        assert store.resolved_gameweek(5)[1].tier.value == "detailed"

    def test_a_missing_gameweek_reads_as_empty_rather_than_raising(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        assert LeagueHistoryStore("2026-27", "classic", 1).load_gameweek(9) == []


class TestResolvedGameweekMemoization:
    """`resolved_gameweek` memoizes per instance, invalidated by
    `append_rows`: several callers sharing one store (the counters
    projection's own read of a gameweek, and a later raw-row read of that
    same gameweek) must not re-parse the same file more than once per call
    chain, and a write must always be visible on the very next read."""

    def test_two_reads_of_the_same_gameweek_parse_the_file_once(self, monkeypatch):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, manager_key=1, gross_points=50)])

        calls: list[int] = []
        original_load_gameweek = store.load_gameweek

        def _counting_load_gameweek(gameweek):
            calls.append(gameweek)
            return original_load_gameweek(gameweek)

        monkeypatch.setattr(store, "load_gameweek", _counting_load_gameweek)

        first = store.resolved_gameweek(5)
        second = store.resolved_gameweek(5)

        assert calls == [5]  # parsed once, not twice
        assert first[1].gross_points == 50
        assert second[1].gross_points == 50

    def test_reads_of_different_gameweeks_each_parse_independently(self, monkeypatch):
        """Two-plus iterations: memoization is keyed per gameweek, not a
        single blanket cache that would collapse distinct gameweeks."""
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gw in (5, 6, 7):
            store.append_rows(gw, [make_history_row(gameweek=gw, manager_key=1, gross_points=gw)])

        calls: list[int] = []
        original_load_gameweek = store.load_gameweek

        def _counting_load_gameweek(gameweek):
            calls.append(gameweek)
            return original_load_gameweek(gameweek)

        monkeypatch.setattr(store, "load_gameweek", _counting_load_gameweek)

        for gw in (5, 6, 7):
            store.resolved_gameweek(gw)
            store.resolved_gameweek(gw)  # second read of the same gameweek

        assert sorted(calls) == [5, 6, 7]

    def test_append_rows_invalidates_the_cache_for_its_own_gameweek(self):
        """A write to a gameweek already read must be reflected on the next
        read off the same store instance, not served stale from the memo
        cache populated before the write."""
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, gross_points=50)])
        assert store.resolved_gameweek(5)[1].gross_points == 50  # populates the cache

        store.append_rows(5, [make_history_row(
            gameweek=5, gross_points=53,
            captured_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        )])

        assert store.resolved_gameweek(5)[1].gross_points == 53  # not served stale

    def test_a_write_that_changes_nothing_leaves_the_cache_untouched(self):
        """An identical re-run writes nothing (R3) -- the already-cached read
        for that gameweek must still be valid afterwards, not merely happen
        to still be correct."""
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, gross_points=50)])
        first = store.resolved_gameweek(5)

        written = store.append_rows(5, [make_history_row(
            gameweek=5, gross_points=50,
            captured_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )])

        assert written == []
        assert store.resolved_gameweek(5)[1].gross_points == first[1].gross_points == 50


class TestStoreVersioning:
    def test_a_line_below_the_readable_floor_raises(self, monkeypatch):
        from fpl_cli.services import league_history as svc

        store = svc.LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, gross_points=50)])
        monkeypatch.setattr(
            svc, "MIN_READABLE_LEAGUE_HISTORY_VERSION", LEAGUE_HISTORY_VERSION + 1,
        )

        with pytest.raises(svc.LeagueHistoryError):
            store.load_gameweek(5)

    def test_an_older_but_readable_line_loads_through_the_upgrade_hook(self, monkeypatch):
        from fpl_cli.services import league_history as svc

        store = svc.LeagueHistoryStore("2026-27", "classic", 1)
        payload = make_history_row(gameweek=5, gross_points=50).model_dump(mode="json")
        payload["version"] = 0
        path = store.gameweek_file(5)
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        monkeypatch.setattr(svc, "MIN_READABLE_LEAGUE_HISTORY_VERSION", 0)

        rows = store.load_gameweek(5)
        assert [r.gross_points for r in rows] == [50]

    def test_a_version_1_row_reads_its_squad_value_back_as_team_value(self):
        """Issue #147: v1 stored the API's bank-inclusive `value` under
        `squad_value`. Same number, renamed on the way in -- a season of
        already-written rows must stay readable, and `extra="forbid"` would
        otherwise reject every one of them."""
        import json

        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        payload = make_history_row(
            gameweek=5, gross_points=50, team_value=1000, bank=5,
        ).model_dump(mode="json")
        payload["version"] = 1
        payload["squad_value"] = payload.pop("team_value")
        path = store.gameweek_file(5)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        row = store.load_gameweek(5)[0]
        assert (row.team_value, row.bank) == (1000, 5)
        assert row.version == 1

    def test_a_version_1_row_without_a_squad_value_still_reads(self):
        """A draft row, or a classic one whose response omitted the figure,
        carries no `squad_value` key at all -- the migration must not invent
        a `team_value` for it."""
        import json

        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "draft", 1)
        payload = make_history_row(
            fpl_format="draft", gameweek=5, gross_points=50,
        ).model_dump(mode="json")
        payload["version"] = 1
        del payload["team_value"]
        path = store.gameweek_file(5)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        assert store.load_gameweek(5)[0].team_value is None

    def test_a_future_version_line_is_skipped_with_a_warning_and_survives(self, caplog):
        import json
        import logging

        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        future = make_history_row(gameweek=5, manager_key=2, gross_points=99).model_dump(mode="json")
        future["version"] = LEAGUE_HISTORY_VERSION + 1
        path = store.gameweek_file(5)
        path.parent.mkdir(parents=True, exist_ok=True)
        future_line = json.dumps(future)
        path.write_text(future_line + "\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            assert store.load_gameweek(5) == []
        assert str(path) in caplog.text

        store.append_rows(5, [make_history_row(gameweek=5, manager_key=1, gross_points=50)])
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == future_line
        assert len(lines) == 2

    def test_existing_lines_are_preserved_byte_for_byte_on_rewrite(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [make_history_row(gameweek=5, manager_key=1, gross_points=50)])
        first_line = store.gameweek_file(5).read_text(encoding="utf-8").splitlines()[0]

        store.append_rows(5, [make_history_row(gameweek=5, manager_key=2, gross_points=40)])
        assert store.gameweek_file(5).read_text(encoding="utf-8").splitlines()[0] == first_line


class TestStoreSeasonPartitioning:
    def test_ae5_a_previous_season_stays_readable_after_the_label_advances(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        old = LeagueHistoryStore("2026-27", "classic", 1)
        old.append_rows(5, [make_history_row(season="2026-27", gameweek=5, gross_points=50)])

        new = LeagueHistoryStore("2027-28", "classic", 1)
        new.append_rows(5, [make_history_row(season="2027-28", gameweek=5, gross_points=60)])

        assert old.load_gameweek(5)[0].gross_points == 50
        assert new.load_gameweek(5)[0].gross_points == 60
        assert old.gameweek_file(5) != new.gameweek_file(5)

    def test_each_format_and_league_gets_its_own_partition(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        paths = {
            LeagueHistoryStore("2026-27", "classic", 1).gameweek_file(5),
            LeagueHistoryStore("2026-27", "draft", 1).gameweek_file(5),
            LeagueHistoryStore("2026-27", "classic", 2).gameweek_file(5),
        }
        assert len(paths) == 3

    def test_the_partition_reports_whether_it_exists_yet(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        assert store.partition_exists() is False
        store.append_rows(5, [make_history_row(gameweek=5, gross_points=50)])
        assert store.partition_exists() is True

    def test_the_store_honours_the_data_dir_override_at_point_of_use(self, tmp_path, monkeypatch):
        from fpl_cli.paths import user_data_dir
        from fpl_cli.services.league_history import LeagueHistoryStore

        redirected = tmp_path / "elsewhere"
        user_data_dir.cache_clear()
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(redirected))
        try:
            path = LeagueHistoryStore("2026-27", "classic", 1).gameweek_file(5)
        finally:
            user_data_dir.cache_clear()
        assert redirected in path.parents


class TestStoreCoverage:
    def test_coverage_reports_tier_and_status_counts_per_gameweek(self):
        from fpl_cli.models.league_history import FidelityTier
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(4, [
            make_history_row(gameweek=4, manager_key=1, tier="coarse", gross_points=40),
            make_history_row(gameweek=4, manager_key=2, tier="coarse", gross_points=41),
        ])
        store.append_rows(5, [
            make_history_row(gameweek=5, manager_key=1, tier="detailed", gross_points=50),
            make_history_row(gameweek=5, manager_key=2, capture_status="unknown"),
        ])

        coverage = {c.gameweek: c for c in store.coverage()}
        assert coverage[4].tier_counts == {FidelityTier.COARSE: 2}
        assert coverage[4].unknown_count == 0
        assert coverage[4].lowest_tier is FidelityTier.COARSE
        assert coverage[5].tier_counts == {FidelityTier.DETAILED: 1}
        assert coverage[5].unknown_count == 1
        assert coverage[5].manager_count == 2
        assert coverage[5].is_complete is False
        assert coverage[4].is_complete is True

    def test_coverage_is_empty_for_a_partition_with_no_rows(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        assert LeagueHistoryStore("2026-27", "classic", 1).coverage() == []

    def test_unknown_manager_keys_are_reported_so_a_gap_can_be_repaired(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        store.append_rows(5, [
            make_history_row(gameweek=5, manager_key=1, gross_points=50),
            make_history_row(gameweek=5, manager_key=2, capture_status="unknown"),
            make_history_row(gameweek=5, manager_key=3, capture_status="unknown"),
        ])
        assert store.coverage()[0].unknown_manager_keys == [2, 3]


class TestStoreMultiIterationLoops:
    """Both store loops that build a list, exercised with two-plus iterations."""

    def test_three_lines_parse_back_in_order(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        rows = [make_history_row(gameweek=5, manager_key=k, gross_points=40 + k) for k in (1, 2, 3)]
        store.append_rows(5, rows)
        assert [r.manager_key for r in store.load_gameweek(5)] == [1, 2, 3]
        assert [r.gross_points for r in store.load_gameweek(5)] == [41, 42, 43]

    def test_coverage_walks_three_gameweeks_independently(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        store = LeagueHistoryStore("2026-27", "classic", 1)
        for gameweek, tier in ((4, "coarse"), (5, "detailed"), (6, "coarse")):
            store.append_rows(gameweek, [
                make_history_row(gameweek=gameweek, tier=tier, gross_points=gameweek),
            ])
        assert [(c.gameweek, c.lowest_tier.value) for c in store.coverage() if c.lowest_tier] == [
            (4, "coarse"), (5, "detailed"), (6, "coarse"),
        ]
