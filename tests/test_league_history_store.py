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
            squad_value=1013, bank=7, global_rank=400_000, transfers_made=2,
        )
        assert (row.squad_value, row.bank, row.global_rank, row.transfers_made) == (1013, 7, 400_000, 2)

    def test_draft_row_omits_all_four_and_still_validates(self):
        row = make_history_row(fpl_format="draft", manager_key=10, gross_points=51)
        assert row.squad_value is None
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
