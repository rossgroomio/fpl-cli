"""Tests for league recap data collection and awards."""
# pyright: reportTypedDictNotRequiredAccess=false

from __future__ import annotations

import asyncio
import logging
import re
from itertools import combinations
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from fpl_cli.agents.orchestration.report import ReportAgent, _format_standings_block
from fpl_cli.cli._league_recap_data import (
    _PICKS_CONCURRENCY,
    MOST_CONTESTED_MIN_CLAIMANTS,
    RecapReconciliationError,
    _apply_league_start_offset,
    _assign_point_in_time_positions,
    _bucket_draft_txns_by_league_entry,
    _classic_pick_flags,
    _compute_most_contested_award,
    _compute_shared_awards,
    _compute_standings_movement,
    _compute_transfer_awards,
    _compute_waiver_awards,
    _contract_draft_txn_chains,
    _fetch_all_manager_data,
    _has_previous_gameweek,
    _reconcile_classic_headline_numbers,
    best_xi_selection,
    collect_classic_recap_data,
    collect_draft_recap_data,
    collect_prior_seasons,
    configured_fine_rule_types,
    derive_point_in_time_positions,
    evaluate_league_fines,
    fetch_manager_histories,
    find_standout_players,
    is_league_opening_gameweek,
    recap_fine_player_names,
    restate_recap_fine_players,
    shape_prior_seasons,
    standout_blanks_order,
)
from fpl_cli.cli._league_recap_types import (
    STANDOUT_PPG_FROM_GW,
    PriorSeasonsSummary,
    RecapAcquisition,
    RecapAwardEntry,
    RecapAwards,
    RecapDraftLostClaim,
    RecapDraftTransaction,
    RecapFineResult,
    RecapManagerEntry,
    RecapManagerPlayer,
    RecapPriorSeason,
    RecapSeasonRecord,
    RecapTransfer,
    competition_rank,
    contested_draft_claims,
    draft_transaction_kind_counts,
    format_contested_claim,
    format_move_counts,
    format_prior_seasons_line,
    last_place_escape,
    recap_fine_lines,
    standout_blanks_heading,
    standout_players_lines,
    summarise_prior_seasons,
)
from fpl_cli.models.player import PlayerPosition
from fpl_cli.prompts.league_recap import (
    collect_player_clubs,
    format_recap_awards_context,
    format_recap_captains_context,
    format_recap_chips_context,
    format_recap_fines_context,
    format_recap_league_history_context,
    format_recap_player_clubs_context,
    format_recap_prior_seasons_context,
    format_recap_season_fines_context,
    format_recap_standings_context,
    format_recap_standout_players_context,
    format_recap_transfers_context,
    format_recap_waivers_context,
    get_recap_synthesis_prompt,
)
from fpl_cli.prompts.league_recap_checks import (
    EDITORIAL_CHECKS,
    RECAP_WARNING_CONTESTED_MISATTRIBUTION,
    RECAP_WARNING_CONTESTED_ROLL_CALL,
    RECAP_WARNING_ESCAPE_MISATTRIBUTION,
    RECAP_WARNING_KIND_NET_MISATTRIBUTION,
    RECAP_WARNING_NET_MISATTRIBUTION,
    RECAP_WARNING_PENALTY_REPEATED,
    RECAP_WARNING_UNSUPPORTED_TIE,
    RECAP_WARNING_WRONG_PLACING,
    RecapNames,
    check_contested_attributions,
    check_contested_roll_calls,
    check_escape_claims,
    check_gameweek_placings,
    check_kind_net_scopes,
    check_net_attributions,
    check_penalty_repetitions,
    check_recap_editorial,
    check_tie_claims,
    editorial_warning_message,
    get_recap_editorial_retry_prompt,
    recap_editorial_flaw,
    restates_forfeit,
    split_recap_problems,
)
from fpl_cli.services.league_history_fines import ManagerFineTally, SeasonFinesTally
from fpl_cli.services.league_history_notes import (
    GameweekWindow,
    NoteKind,
    NotesPack,
    NotesPackEntry,
    NoteSurface,
    SeasonPhase,
)
from tests.conftest import (
    make_draft_player,
    make_draft_team,
    make_gameweek_status,
    make_player,
    make_team,
    team_code_for,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The draft game's club numbering for the draft collector's club tests,
# deliberately the main game's reversed (#423): draft club 1 is main-game
# club 2 and draft club 2 is main-game club 1, joined by club code. A draft
# id read as a main-game one then names the other club rather than missing.
_SWAPPED_DRAFT_TEAMS = [
    make_draft_team(id=1, code=team_code_for(2)),
    make_draft_team(id=2, code=team_code_for(1)),
]


def _make_manager(
    name: str = "Manager A",
    entry_id: int = 1,
    gw_points: int = 50,
    gross_points: int | None = None,
    total_points: int | None = 500,
    gw_rank: int = 1,
    overall_rank: int | None = 1,
    previous_rank: int | None = 1,
    captain: str = "Salah",
    captain_points: int = 10,
    captain_played: bool = True,
    vice_captain: str = "Saka",
    vice_captain_points: int = 0,
    bench_points: int = 5,
    best_xi_gain: int | None = None,
    transfer_cost: int = 0,
    active_chip: str | None = None,
    squad: list[RecapManagerPlayer] | None = None,
    transfers: list[RecapTransfer] | None = None,
    transfers_made: int | None = None,
) -> RecapManagerEntry:
    """Factory for RecapManagerEntry with sensible defaults.

    `gross_points` defaults to `gw_points` -- the common case where a test
    doesn't care about the gross/net distinction. Pass `total_points`,
    `overall_rank`, or `previous_rank` as None to build an entry with that
    key absent, e.g. an unreconstructable draft replay (U2/U3).
    `best_xi_gain` is derived from `squad` the way the collectors derive it,
    so a fixture's award number can never disagree with its squad.
    """
    if best_xi_gain is None:
        best_xi_gain = best_xi_selection(squad or [], captain_played=captain_played).gain
    result = RecapManagerEntry(
        manager_name=name,
        entry_id=entry_id,
        gw_points=gw_points,
        gross_points=gross_points if gross_points is not None else gw_points,
        total_points=total_points if total_points is not None else 0,
        gw_rank=gw_rank,
        overall_rank=overall_rank if overall_rank is not None else 0,
        previous_rank=previous_rank if previous_rank is not None else 0,
        captain=captain,
        captain_points=captain_points,
        captain_played=captain_played,
        vice_captain=vice_captain,
        vice_captain_points=vice_captain_points,
        active_chip=active_chip,
        squad=squad or [],
        bench_points=bench_points,
        best_xi_gain=best_xi_gain,
        transfer_cost=transfer_cost,
        auto_subs=[],
    )
    if total_points is None:
        del result["total_points"]
    if overall_rank is None:
        del result["overall_rank"]
    if previous_rank is None:
        del result["previous_rank"]
    if transfers is not None:
        result["transfers"] = transfers
    if transfers_made is not None:
        result["transfers_made"] = transfers_made
    return result


def _make_transfer(
    player_in: str,
    player_out: str,
    in_points: int,
    out_points: int,
    cost: int = 0,
) -> RecapTransfer:
    return RecapTransfer(
        player_in=player_in, player_in_team="ARS", player_in_points=in_points,
        player_out=player_out, player_out_team="LIV", player_out_points=out_points,
        net=in_points - out_points, cost=cost,
    )


def _make_squad_player(
    name: str = "Player",
    points: int = 5,
    contributed: bool = True,
    auto_sub_out: bool = False,
    **kwargs,
) -> RecapManagerPlayer:
    return RecapManagerPlayer(
        name=name,
        team="ARS",
        position=kwargs.get("position", "MID"),
        code=kwargs.get("code"),
        points=points,
        is_captain=kwargs.get("is_captain", False),
        is_vice_captain=kwargs.get("is_vice_captain", False),
        contributed=contributed,
        is_bench_boost_player=kwargs.get("is_bench_boost_player", False),
        auto_sub_in=kwargs.get("auto_sub_in", False),
        auto_sub_out=auto_sub_out,
        red_cards=kwargs.get("red_cards", 0),
        unmatched=kwargs.get("unmatched", False),
    )


# A (position, points) pair, or (position, points, name) to name the player.
_Slot = tuple[str, int] | tuple[str, int, str]


def _squad(
    xi: list[_Slot],
    bench: list[_Slot] = [],  # noqa: B006 -- never mutated
    *,
    captain: str | None = None,
    vice: str | None = None,
) -> list[RecapManagerPlayer]:
    """A recorded 15 from (position, points) slots, the XI first, the bench after.

    An unnamed slot is called after its position and index ("MID3"), so a
    test names only the players its assertions look for. `captain` and
    `vice` name the armband holders, XI or bench alike.
    """
    squad: list[RecapManagerPlayer] = []
    for contributed, slots in ((True, xi), (False, bench)):
        for i, slot in enumerate(slots, start=1):
            position, points = slot[0], slot[1]
            name = slot[2] if len(slot) == 3 else f"{position}{i}{'' if contributed else 'b'}"
            squad.append(_make_squad_player(
                name=name, points=points, contributed=contributed, position=position,
                is_captain=name == captain, is_vice_captain=name == vice,
            ))
    return squad


# A 4-4-2 in which every starter scored `points`.
def _flat_xi(points: int) -> list[_Slot]:
    return [("GK", points)] + [("DEF", points)] * 4 + [("MID", points)] * 4 + [("FWD", points)] * 2


def _overstated_bench() -> list[RecapManagerPlayer]:
    """The shape of the squad #350 was raised on: 31 on the bench, of which
    the keeper's 3 were never available (the one who played scored 5) and the
    two outfielders replace a 3 and a 2 -- 23 gained, not 31."""
    return _squad(
        [("GK", 5)] + [("DEF", 4)] * 3 + [("DEF", 2, "Colwill")]
        + [("MID", 6)] * 3 + [("MID", 3, "Rice"), ("FWD", 8), ("FWD", 7)],
        [("GK", 3, "Trafford"), ("MID", 17, "Groß"), ("DEF", 11, "Gvardiol"), ("FWD", 0)],
        captain="FWD1",
    )


class TestRecapFineMessagePlayers:
    """#176: the ledger restates a stored ruling's names through these two, so
    the shapes they cannot read are the shapes they must not rewrite either."""

    PENALTY = "Buy the round"

    def _message(self, names: str) -> str:
        return f"Red card in starting XI ({names}). {self.PENALTY}"

    def test_the_names_read_back_in_the_order_the_message_lists_them(self):
        assert recap_fine_player_names(self._message("Sávio, Hothead")) == [
            "Sávio", "Hothead",
        ]

    def test_a_name_carrying_a_bare_comma_reads_back_whole(self):
        """Split on the separator the message was written with, not on any
        comma in it (#290 review)."""
        assert recap_fine_player_names(self._message("Smith,Jr")) == ["Smith,Jr"]

    def test_a_name_carrying_the_separator_itself_reads_as_two(self):
        """The limit of reading names out of prose, asserted rather than left
        implicit: the caller sees a count the squad cannot corroborate and
        declines the repair, which is the safe direction to fail in."""
        assert recap_fine_player_names(self._message("Smith, Jr")) == ["Smith", "Jr"]

    def test_a_message_naming_nobody_reads_back_empty(self):
        assert recap_fine_player_names("Finished last in the gameweek. Pint") == []

    def test_a_penalty_carrying_brackets_is_not_mistaken_for_the_player_list(self):
        message = "Red card in starting XI (Sávio). Pay the pot (cash)"
        assert recap_fine_player_names(message) == ["Sávio"]
        assert restate_recap_fine_players(message, ["Sávio"], ["Savinho"]) == (
            "Red card in starting XI (Savinho). Pay the pot (cash)"
        )

    def test_what_reads_back_out_restates_back_in(self):
        message = self._message("Sávio, Hothead")
        names = recap_fine_player_names(message)
        assert restate_recap_fine_players(message, names, names) == message

    def test_a_message_that_does_not_spell_the_list_out_is_returned_untouched(self):
        message = self._message("Sávio")
        assert restate_recap_fine_players(message, ["Someone Else"], ["Savinho"]) == message


# ---------------------------------------------------------------------------
# Awards: clear winners
# ---------------------------------------------------------------------------


class TestAwardsClearWinner:
    def test_gw_winner_highest_points(self):
        managers = [
            _make_manager(name="Alice", gw_points=80),
            _make_manager(name="Bob", gw_points=60),
            _make_manager(name="Charlie", gw_points=40),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["gw_winner"]["manager_name"] == "Alice"
        assert awards["gw_winner"]["value"] == 80

    def test_gw_loser_lowest_points(self):
        managers = [
            _make_manager(name="Alice", gw_points=80),
            _make_manager(name="Bob", gw_points=20),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["gw_loser"]["manager_name"] == "Bob"
        assert awards["gw_loser"]["value"] == 20

    def test_biggest_bench_haul(self):
        # Benchman (15) for a 0-point midfielder is the whole swap: 15 more.
        alice = _squad(
            [("GK", 2)] + [("DEF", 2)] * 4 + [("MID", 2)] * 3 + [("MID", 0, "Passenger"), ("FWD", 2), ("FWD", 2)],
            [("GK", 0), ("MID", 15, "Benchman"), ("DEF", 0), ("FWD", 0)],
        )
        bob = _squad(_flat_xi(2), [("GK", 0), ("MID", 5), ("DEF", 0), ("FWD", 0)])
        managers = [
            _make_manager(name="Alice", gw_points=29, squad=alice),
            _make_manager(name="Bob", squad=bob),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["biggest_bench_haul"]["manager_name"] == "Alice"
        assert awards["biggest_bench_haul"]["value"] == 15
        detail = awards["biggest_bench_haul"]["detail"]
        assert detail == (
            "Alice could have scored 15 more with their best XI (team scored 29 pts):"
            " Benchman (15) benched while Passenger (0) played"
        )

    def test_bench_award_ranks_on_the_best_xi_gain_not_the_raw_bench_sum(self):
        """#350: the award went to the manager whose bench summed highest,
        when another manager lost more by picking the wrong XI."""
        # Alice: 31 on the bench, 23 gained.
        alice = _overstated_bench()
        # Bob: 30 on the bench, all of it outfield and all of it replacing
        # a 1, a 1 and a 2 -- 26 gained.
        bob = _squad(
            [("GK", 5)] + [("DEF", 4)] * 3 + [("DEF", 1)]
            + [("MID", 6)] * 3 + [("MID", 1), ("FWD", 8), ("FWD", 2)],
            [("GK", 0), ("DEF", 12), ("MID", 10), ("FWD", 8)],
            captain="FWD1",
        )
        managers = [
            _make_manager(name="Alice", gw_points=82, bench_points=31, squad=alice),
            _make_manager(name="Bob", gw_points=91, bench_points=30, squad=bob),
        ]
        assert managers[0]["best_xi_gain"] == 23
        assert managers[1]["best_xi_gain"] == 26
        awards = _compute_shared_awards(managers)
        assert awards["biggest_bench_haul"]["manager_name"] == "Bob"
        assert awards["biggest_bench_haul"]["value"] == 26
        assert "Alice" not in awards["biggest_bench_haul"]["detail"]

    def test_best_captain_by_raw_points(self):
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=15),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["manager_name"] == "Alice"
        assert "Salah" in awards["best_captain"]["detail"]

    def test_worst_captain(self):
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=15),
            _make_manager(name="Bob", captain="Haaland", captain_points=0),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["worst_captain"]["manager_name"] == "Bob"
        assert "Haaland" in awards["worst_captain"]["detail"]


# ---------------------------------------------------------------------------
# Awards: ties
# ---------------------------------------------------------------------------


class TestAwardsTies:
    def test_tied_gw_winner(self):
        managers = [
            _make_manager(name="Alice", gw_points=80),
            _make_manager(name="Bob", gw_points=80),
            _make_manager(name="Charlie", gw_points=40),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["gw_winner"]["value"] == 80
        assert "Alice" in awards["gw_winner"]["detail"]
        assert "Bob" in awards["gw_winner"]["detail"]
        assert "Alice and Bob" == awards["gw_winner"]["manager_name"]

    def test_tied_gw_loser(self):
        managers = [
            _make_manager(name="Alice", gw_points=80),
            _make_manager(name="Bob", gw_points=20),
            _make_manager(name="Charlie", gw_points=20),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["gw_loser"]["value"] == 20
        assert "Bob" in awards["gw_loser"]["detail"]
        assert "Charlie" in awards["gw_loser"]["detail"]
        assert "Bob and Charlie" == awards["gw_loser"]["manager_name"]

    def test_tied_bench_haul(self):
        def _twelve_behind(bench_name: str) -> list[RecapManagerPlayer]:
            return _squad(
                _flat_xi(2)[:-1] + [("FWD", 0)],
                [("GK", 0), ("FWD", 12, bench_name), ("DEF", 0), ("MID", 0)],
            )

        managers = [
            _make_manager(name="Alice", gw_points=40, squad=_twelve_behind("BenchA")),
            _make_manager(name="Bob", gw_points=55, squad=_twelve_behind("BenchB")),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["biggest_bench_haul"]["value"] == 12
        assert "Alice and Bob" == awards["biggest_bench_haul"]["manager_name"]
        assert "BenchA (12)" in awards["biggest_bench_haul"]["detail"]
        assert "BenchB (12)" in awards["biggest_bench_haul"]["detail"]
        assert "team scored 40 pts" in awards["biggest_bench_haul"]["detail"]
        assert "team scored 55 pts" in awards["biggest_bench_haul"]["detail"]

    def test_wide_bench_haul_tie_caps_managers(self):
        """Each bench entry is verbose, so a wide tie is capped like the other awards."""
        managers = [
            _make_manager(
                name=f"M{i:02d}", entry_id=i, gw_points=60 - i,
                squad=_squad(
                    _flat_xi(2)[:-1] + [("FWD", 0)],
                    [("GK", 0), ("FWD", 12, f"Bench{i:02d}"), ("DEF", 0), ("MID", 0)],
                ),
            )
            for i in range(6)
        ]
        awards = _compute_shared_awards(managers)
        detail = awards["biggest_bench_haul"]["detail"]
        assert detail.count("could have scored 12 more") == 3
        assert "3 more managers omitted" in detail
        # The award still records every tied manager, only the prose is trimmed
        assert awards["biggest_bench_haul"]["manager_name"].count(" and ") == 5
        # All tied on bench points, so the ones named are those it cost most:
        # M05 scored least (55), M00 most (60).
        assert [n for n in ("M00", "M01", "M02", "M03", "M04", "M05") if n in detail] == [
            "M03", "M04", "M05",
        ]
        assert detail.index("M05") < detail.index("M04") < detail.index("M03")

    def test_tied_captain_same_captain(self):
        """Two managers captaining the same player - grouped output."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=10),
            _make_manager(name="Bob", captain="Salah", captain_points=10),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["value"] == 10
        assert awards["best_captain"]["detail"] == "Alice and Bob captained Salah (10 pts)"
        assert awards["best_captain"]["manager_name"] == "Alice and Bob"

    def test_tied_captain_different_captains(self):
        """Two managers captaining different players with equal points."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=10),
            _make_manager(name="Bob", captain="Haaland", captain_points=10),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["value"] == 10
        assert "Alice captained Salah (10 pts)" in awards["best_captain"]["detail"]
        assert "Bob captained Haaland (10 pts)" in awards["best_captain"]["detail"]

    def test_worst_captain_tie_same_captain(self):
        """Two managers tied for worst captain on the same player."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=15),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
            _make_manager(name="Charlie", captain="Haaland", captain_points=2),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["worst_captain"]["value"] == 2
        assert awards["worst_captain"]["detail"] == "Bob and Charlie captained Haaland (2 pts) [2 of 3 managers]"
        assert awards["worst_captain"]["manager_name"] == "Bob and Charlie"

    def test_total_managers_uses_full_league_not_just_fetched(self):
        """A failed picks fetch must not shrink the '[n of total]' denominator.

        Passing the full league size (as collect_classic/draft_recap_data do)
        keeps the fraction honest even when some managers dropped out of
        `managers` because their picks fetch failed.
        """
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=15),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
            _make_manager(name="Charlie", captain="Haaland", captain_points=2),
        ]
        awards = _compute_shared_awards(managers, total_managers=20)
        assert awards["worst_captain"]["detail"] == "Bob and Charlie captained Haaland (2 pts) [2 of 20 managers]"

    def test_three_way_captain_tie_same_captain(self):
        """Three managers all captaining the same player - uses 'all captained'."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=10),
            _make_manager(name="Bob", captain="Salah", captain_points=10),
            _make_manager(name="Charlie", captain="Salah", captain_points=10),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["value"] == 10
        assert awards["best_captain"]["detail"] == (
            "Alice, Bob and Charlie all captained Salah (10 pts)"
        )
        assert awards["best_captain"]["manager_name"] == "Alice and Bob and Charlie"

    def test_three_way_captain_tie_mixed_captains(self):
        """Three managers tied - two share a captain, one different."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=10),
            _make_manager(name="Bob", captain="Salah", captain_points=10),
            _make_manager(name="Charlie", captain="Palmer", captain_points=10),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["value"] == 10
        assert "Alice and Bob captained Salah (10 pts)" in awards["best_captain"]["detail"]
        assert "Charlie captained Palmer (10 pts)" in awards["best_captain"]["detail"]

    def test_worst_captain_vc_rescue_not_penalised(self):
        """Captain dnp but VC covered (high VC pts): should not win worst captain."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0, captain_played=False, vice_captain_points=15),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
            _make_manager(name="Charlie", captain="Palmer", captain_points=8),
        ]
        awards = _compute_shared_awards(managers)
        # Alice's effective captain pts = 15 (VC rescued); Bob (2) is worst
        assert awards["worst_captain"]["manager_name"] == "Bob"
        assert awards["worst_captain"]["value"] == 2

    def test_worst_captain_vc_also_blanked(self):
        """Captain dnp AND VC scored 0: genuinely worst captain outcome."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0, captain_played=False, vice_captain_points=0),
            _make_manager(name="Bob", captain="Haaland", captain_points=2),
            _make_manager(name="Charlie", captain="Palmer", captain_points=8),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["worst_captain"]["manager_name"] == "Alice"
        assert awards["worst_captain"]["value"] == 0

    def test_worst_captain_all_captains_didnt_play(self):
        """All captains dnp with 0 VC pts: all tied at 0."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0, captain_played=False, vice_captain_points=0),
            _make_manager(name="Bob", captain="Haaland", captain_points=0, captain_played=False, vice_captain_points=0),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["worst_captain"]["value"] == 0

    def test_worst_captain_mixed_tie_reports_effective_points(self):
        """Tie struck on effective pts must not print a tied manager's raw captain_points."""
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0, captain_played=False,
                          vice_captain="Gordon", vice_captain_points=3),
            _make_manager(name="Bob", captain="Haaland", captain_points=3),
            _make_manager(name="Charlie", captain="Palmer", captain_points=10),
        ]
        awards = _compute_shared_awards(managers)
        detail = awards["worst_captain"]["detail"]
        assert awards["worst_captain"]["value"] == 3
        assert "Bob captained Haaland (3 pts)" in detail
        # Salah blanked - the 3 came from Alice's vice, so don't credit it to Salah
        assert "Alice captained Salah (dnp; vice scored 3)" in detail
        assert "(0 pts)" not in detail

    def test_wide_captain_tie_caps_groups(self):
        """A tie spread across many captains is capped, with the omitted count reported."""
        managers = [
            _make_manager(name=f"M{i:02d}", entry_id=i, captain=f"Cap{i}", captain_points=0)
            for i in range(6)
        ] + [_make_manager(name="Top", entry_id=99, captain="Salah", captain_points=12)]
        awards = _compute_shared_awards(managers)
        detail = awards["worst_captain"]["detail"]
        assert detail.count("captained") == 3
        assert "3 more managers omitted" in detail

    def test_wide_captain_tie_caps_managers_within_a_group(self):
        """Most of the league on one captain must not print a roster-length group."""
        managers = [
            _make_manager(name=f"M{i:02d}", entry_id=i, captain="Haaland", captain_points=2)
            for i in range(14)
        ] + [_make_manager(name="Top", entry_id=99, captain="Salah", captain_points=12)]
        awards = _compute_shared_awards(managers, total_managers=15)
        detail = awards["worst_captain"]["detail"]
        assert detail == (
            "M00, M01, M02 and 11 others all captained Haaland (2 pts) [14 of 15 managers]"
        )
        # The award still records every tied manager, only the prose is trimmed
        assert awards["worst_captain"]["manager_name"].count(" and ") == 13

    def test_league_wide_captain_tie_collapses_to_one_line(self):
        """Issue #145: a tie everyone is in names nobody and is awarded once."""
        managers = [
            _make_manager(
                name=f"M{i:02d}",
                entry_id=i,
                captain="Haaland" if i < 14 else "Salah",
                captain_points=2,
            )
            for i in range(19)
        ]
        awards = _compute_shared_awards(managers, total_managers=19)
        assert awards["best_captain"]["detail"] == (
            "Captaincy was a wash — all 19 captains scored 2 pts (Haaland ×14, Salah ×5)"
        )
        assert not any(m["manager_name"] in awards["best_captain"]["detail"] for m in managers)
        # Best and Worst hold the same managers, so the second block is dropped
        assert "worst_captain" not in awards
        # The award still records every tied manager, only the prose is trimmed
        assert awards["best_captain"]["manager_name"].count(" and ") == 18

    def test_league_wide_captain_tie_caps_the_captains_it_names(self):
        """A wash spread over many captains reports the omitted players."""
        managers = [
            _make_manager(name=f"M{i:02d}", entry_id=i, captain=f"Cap{i}", captain_points=3)
            for i in range(5)
        ]
        awards = _compute_shared_awards(managers, total_managers=5)
        detail = awards["best_captain"]["detail"]
        assert detail.startswith("Captaincy was a wash — all 5 captains scored 3 pts (")
        assert detail.count("×1") == 3
        assert detail.endswith("; 2 more players omitted")

    def test_league_wide_captain_tie_flags_captains_that_did_not_play(self):
        """A wash reached through vice takeovers must not credit the captains."""
        managers = [
            _make_manager(
                name=f"M{i:02d}",
                entry_id=i,
                captain="Haaland",
                captain_points=0,
                captain_played=False,
                vice_captain_points=1,
            )
            for i in range(6)
        ]
        awards = _compute_shared_awards(managers, total_managers=6)
        assert "best_captain" not in awards
        assert awards["worst_captain"]["detail"] == (
            "Captaincy was a wash — all 6 captaincies were worth 1 pt (Haaland ×6 (dnp))"
        )

    def test_league_wide_captain_tie_mixing_played_and_vice_routes(self):
        """Half the tie scored it, half got it from a vice: claim neither route."""
        managers = [
            _make_manager(name=f"P{i}", entry_id=i, captain="Haaland", captain_points=2)
            for i in range(5)
        ] + [
            _make_manager(
                name=f"D{i}",
                entry_id=10 + i,
                captain="Salah",
                captain_points=0,
                captain_played=False,
                vice_captain_points=2,
            )
            for i in range(5)
        ]
        awards = _compute_shared_awards(managers, total_managers=10)
        assert awards["worst_captain"]["detail"] == (
            "Captaincy was a wash — all 10 captaincies were worth 2 pts"
            " (Haaland ×5, Salah ×5 (dnp))"
        )

    def test_league_wide_captain_tie_keeps_grouped_prose_in_a_small_league(self):
        """Three managers fit in a capped block, so nothing is collapsed."""
        managers = [
            _make_manager(name=n, entry_id=i, captain="Salah", captain_points=10)
            for i, n in enumerate(("Alice", "Bob", "Charlie"))
        ]
        awards = _compute_shared_awards(managers, total_managers=3)
        assert awards["best_captain"]["detail"] == "Alice, Bob and Charlie all captained Salah (10 pts)"
        assert "worst_captain" not in awards

    def test_captain_awards_not_duplicated_when_one_manager_holds_both(self):
        """Best and Worst on the same manager is one fact under two headings.

        Alice's captain is both the highest raw score and the lowest effective
        one; Bob's captain blanked but his vice beat it, so he is in neither
        set. Nothing about the gameweek is lost by dropping the repeat.
        """
        managers = [
            _make_manager(name="Alice", entry_id=1, captain="Salah", captain_points=10),
            _make_manager(
                name="Bob",
                entry_id=2,
                captain="Haaland",
                captain_points=4,
                captain_played=False,
                vice_captain_points=15,
            ),
        ]
        awards = _compute_shared_awards(managers, total_managers=2)
        assert awards["best_captain"]["detail"] == "Alice captained Salah (10 pts)"
        assert "worst_captain" not in awards

    def test_captain_awards_kept_when_the_sets_differ(self):
        """The common case still gets both awards."""
        managers = [
            _make_manager(name="Alice", entry_id=1, captain="Salah", captain_points=15),
            _make_manager(name="Bob", entry_id=2, captain="Haaland", captain_points=2),
        ]
        awards = _compute_shared_awards(managers, total_managers=2)
        assert awards["best_captain"]["manager_name"] == "Alice"
        assert awards["worst_captain"]["manager_name"] == "Bob"


# ---------------------------------------------------------------------------
# Awards: edge cases
# ---------------------------------------------------------------------------


class TestAwardsEdgeCases:
    def test_empty_managers(self):
        awards = _compute_shared_awards([])
        assert awards == RecapAwards()

    def test_single_manager(self):
        managers = [_make_manager(name="Solo")]
        awards = _compute_shared_awards(managers)
        assert awards["gw_winner"]["manager_name"] == "Solo"
        assert awards["gw_loser"]["manager_name"] == "Solo"

    def test_zero_bench_points_no_award(self):
        managers = [_make_manager(name="Alice", bench_points=0)]
        awards = _compute_shared_awards(managers)
        assert "biggest_bench_haul" not in awards

    def test_bench_points_nobody_could_have_played_earn_no_award(self):
        """#350: five on the bench -- a keeper behind the one who played and a
        defender no starter was below -- is not five points left anywhere."""
        squad = _squad(
            [("GK", 6)] + _flat_xi(2)[1:],
            [("GK", 3), ("DEF", 2), ("MID", 0), ("FWD", 0)],
        )
        managers = [_make_manager(name="Alice", bench_points=5, squad=squad)]
        assert managers[0]["best_xi_gain"] == 0
        awards = _compute_shared_awards(managers)
        assert "biggest_bench_haul" not in awards

    def test_all_captains_zero(self):
        managers = [
            _make_manager(name="Alice", captain="Salah", captain_points=0),
            _make_manager(name="Bob", captain="Haaland", captain_points=0),
        ]
        awards = _compute_shared_awards(managers)
        # best_captain not awarded when all are 0
        assert "best_captain" not in awards
        # worst_captain is always awarded
        assert awards["worst_captain"]["value"] == 0


# ---------------------------------------------------------------------------
# Awards: active chip
# ---------------------------------------------------------------------------


class TestAwardsChips:
    def test_manager_with_triple_captain(self):
        managers = [
            _make_manager(name="Alice", active_chip="3xc", captain_points=15),
            _make_manager(name="Bob", captain_points=5),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["best_captain"]["manager_name"] == "Alice"

    def test_manager_with_bench_boost(self):
        """Bench boost means bench_points=0 (all contribute). No bench award expected.

        Uses the display-form chip ("BB") that _fetch_all_manager_data actually stores
        on RecapManagerEntry — the raw "bboost" never reaches _compute_shared_awards.
        """
        bb_player = _make_squad_player(
            name="AliceBench", points=0, contributed=True, is_bench_boost_player=True,
        )
        bob = _squad(_flat_xi(2), [("GK", 0), ("MID", 12), ("DEF", 0), ("FWD", 0)])
        managers = [
            _make_manager(name="Alice", active_chip="BB", bench_points=0, squad=[bb_player]),
            _make_manager(name="Bob", bench_points=12, squad=bob),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["biggest_bench_haul"]["manager_name"] == "Bob"

    def test_bb_manager_excluded_even_if_bench_points_stale(self):
        # Regression: the defensive filter must not rely on the raw "bboost" string —
        # RecapManagerEntry.active_chip is stored in display form ("BB"). Detection
        # runs off the per-player is_bench_boost_player flag instead.
        bench_bb = _make_squad_player(
            name="BBBench", points=30, contributed=True, is_bench_boost_player=True,
        )
        bob = _squad(_flat_xi(2), [("GK", 0), ("MID", 15, "Benchman"), ("DEF", 0), ("FWD", 0)])
        managers = [
            _make_manager(
                name="Alice", active_chip="BB", gw_points=99,
                bench_points=30, best_xi_gain=30, squad=[bench_bb],
            ),
            _make_manager(name="Bob", gw_points=60, bench_points=15, squad=bob),
        ]
        awards = _compute_shared_awards(managers)
        assert awards["biggest_bench_haul"]["manager_name"] == "Bob"
        assert "Alice" not in awards["biggest_bench_haul"]["detail"]

    def test_all_managers_played_bench_boost(self):
        bb_player = _make_squad_player(
            name="Bench", points=0, contributed=True, is_bench_boost_player=True,
        )
        managers = [
            _make_manager(name="Alice", active_chip="BB", bench_points=0, squad=[bb_player]),
            _make_manager(name="Bob", active_chip="BB", bench_points=0, squad=[bb_player]),
        ]
        awards = _compute_shared_awards(managers)
        assert "biggest_bench_haul" not in awards


# ---------------------------------------------------------------------------
# Transfer awards
# ---------------------------------------------------------------------------


class TestTransferAwards:
    def test_transfer_genius(self):
        transfers = [
            RecapTransfer(
                player_in="Palmer", player_in_team="CHE", player_in_points=15,
                player_out="Wilson", player_out_team="FUL", player_out_points=2,
                net=13, cost=0,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_genius" in awards
        award = awards["transfer_genius"]
        assert award["manager_name"] == "Alice"
        assert award["value"] == 13
        assert "Best: Palmer in for Wilson (+13)" in award["detail"]

    def test_move_phrasing_states_direction_not_just_names(self):
        # Regression for #146: "X for Y" read as X leaving, Y arriving --
        # backwards from the actual move. Asymmetric names so a reversed
        # direction would fail this assertion outright.
        transfers = [
            RecapTransfer(
                player_in="Savinho", player_in_team="MCI", player_in_points=0,
                player_out="Maddison", player_out_team="TOT", player_out_points=1,
                net=-1, cost=0,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        detail = awards["transfer_disaster"]["detail"]
        assert "Savinho in for Maddison (-1)" in detail
        assert "Maddison in for Savinho" not in detail
        assert "Savinho for Maddison" not in detail

    def test_transfer_disaster(self):
        transfers = [
            RecapTransfer(
                player_in="Dud", player_in_team="LEI", player_in_points=0,
                player_out="Star", player_out_team="LIV", player_out_points=12,
                net=-12, cost=4,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_disaster" in awards
        assert awards.get("transfer_disaster", {}).get("manager_name") == "Alice"  # type: ignore[union-attr]

    def test_no_transfers_no_awards(self):
        managers = [_make_manager(name="Alice"), _make_manager(name="Bob")]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_genius" not in awards
        assert "transfer_disaster" not in awards

    def test_positive_net_only_no_disaster(self):
        transfers = [
            RecapTransfer(
                player_in="Good", player_in_team="ARS", player_in_points=10,
                player_out="OK", player_out_team="TOT", player_out_points=5,
                net=5, cost=0,
            ),
        ]
        managers = [_make_manager(name="Alice", transfers=transfers)]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_genius" in awards
        assert "transfer_disaster" not in awards

    def test_disaster_fires_from_hit_alone(self):
        # +3 raw aggregate, -4 hit -> true_net -1, should fire disaster.
        # Pre-fix this case was suppressed (raw aggregate ignored hit).
        transfers = [
            RecapTransfer(
                player_in="Mid", player_in_team="ARS", player_in_points=4,
                player_out="Out", player_out_team="TOT", player_out_points=1,
                net=3, cost=4,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers, transfer_cost=4),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_disaster" in awards
        assert awards["transfer_disaster"]["manager_name"] == "Alice"
        assert awards["transfer_disaster"]["value"] == -1
        detail = awards["transfer_disaster"]["detail"]
        assert "lost 1 net pts overall" in detail
        assert "+3 raw" in detail
        assert "-4 hit" in detail

    def test_genius_value_is_post_hit_true_net(self):
        # Raw aggregate +13, -4 hit -> true_net +9. Genius value should be 9.
        transfers = [
            RecapTransfer(
                player_in="A", player_in_team="ARS", player_in_points=10,
                player_out="B", player_out_team="TOT", player_out_points=2,
                net=8, cost=4,
            ),
            RecapTransfer(
                player_in="C", player_in_team="CHE", player_in_points=7,
                player_out="D", player_out_team="WHU", player_out_points=2,
                net=5, cost=4,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers, transfer_cost=4),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        assert "transfer_genius" in awards
        assert awards["transfer_genius"]["value"] == 9
        detail = awards["transfer_genius"]["detail"]
        assert "gained 9 net pts overall" in detail
        assert "+13 raw across 2 transfers" in detail
        assert "-4 hit" in detail
        # Top move first, second move via "also"
        assert "Best: A in for B (+8)" in detail
        assert "also C in for D (+5)" in detail

    def test_detail_caps_at_three_with_omitted_count(self):
        # 5 transfers, cap=3, expect "2 more omitted" tail.
        transfers = [
            RecapTransfer(
                player_in=f"In{i}", player_in_team="ARS", player_in_points=10 - i,
                player_out=f"Out{i}", player_out_team="TOT", player_out_points=0,
                net=10 - i, cost=0,
            )
            for i in range(5)
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        detail = awards["transfer_genius"]["detail"]
        # Top 3 by net (descending): In0 (+10), In1 (+9), In2 (+8)
        assert "Best: In0 in for Out0 (+10)" in detail
        assert "In1 in for Out1 (+9)" in detail
        assert "In2 in for Out2 (+8)" in detail
        assert "In3" not in detail
        assert "In4" not in detail
        assert "2 more omitted" in detail

    def test_disaster_side_caps_at_three(self):
        # 5 negative-net transfers, no hit. Disaster sorts ascending; top 3
        # worst should appear, the two least-bad should be omitted.
        transfers = [
            RecapTransfer(
                player_in=f"Bust{i}", player_in_team="ARS", player_in_points=0,
                player_out=f"Star{i}", player_out_team="TOT", player_out_points=2 + i,
                net=-(2 + i), cost=0,
            )
            for i in range(5)
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        detail = awards["transfer_disaster"]["detail"]
        # Top 3 worst by net (ascending): Bust4 (-6), Bust3 (-5), Bust2 (-4)
        assert "Worst: Bust4 in for Star4 (-6)" in detail
        assert "Bust3 in for Star3 (-5)" in detail
        assert "Bust2 in for Star2 (-4)" in detail
        assert "Bust0" not in detail
        assert "Bust1" not in detail
        assert "2 more omitted" in detail

    def test_disaster_from_hit_alone_uses_neutral_framing(self):
        # +3 raw, -4 hit -> true_net -1. No individual move is negative.
        # Detail must not label a positive move as "Worst".
        transfers = [
            RecapTransfer(
                player_in="Mid", player_in_team="ARS", player_in_points=4,
                player_out="Out", player_out_team="TOT", player_out_points=1,
                net=3, cost=4,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers, transfer_cost=4),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        detail = awards["transfer_disaster"]["detail"]
        assert "Worst: Mid in for Out (+3)" not in detail
        assert "All swaps were profitable" in detail
        assert "the hit cost produced the loss" in detail
        assert "Mid in for Out (+3)" in detail

    def test_single_transfer_no_hit_compact_format(self):
        # Single transfer, no hit: skip the redundant raw/count parenthetical.
        transfers = [
            RecapTransfer(
                player_in="Solo", player_in_team="ARS", player_in_points=7,
                player_out="Gone", player_out_team="TOT", player_out_points=0,
                net=7, cost=0,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_transfer_awards(managers, awards)
        detail = awards["transfer_genius"]["detail"]
        assert "Alice gained 7 net pts overall." in detail
        assert "Best: Solo in for Gone (+7)." in detail
        assert "raw across" not in detail
        assert "hit" not in detail


# ---------------------------------------------------------------------------
# Standings movement
# ---------------------------------------------------------------------------


class TestStandingsMovement:
    def test_movement_computed_from_point_diff(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=80, total_points=500),
            _make_manager(name="Bob", entry_id=2, gw_points=20, total_points=480),
            _make_manager(name="Charlie", entry_id=3, gw_points=50, total_points=470),
        ]
        _compute_standings_movement(managers)
        # Previous totals: Alice=420, Bob=460, Charlie=420
        # Previous table: Bob(460)=1st, Alice and Charlie level on 420=joint 2nd
        assert managers[1]["previous_rank"] == 1  # Bob was 1st
        assert managers[0]["previous_rank"] == 2  # Alice and Charlie shared 2nd
        assert managers[2]["previous_rank"] == 2

    def test_movement_single_manager(self):
        managers = [_make_manager(name="Solo", entry_id=1)]
        _compute_standings_movement(managers)
        assert managers[0]["previous_rank"] == 1

    def test_movement_no_change(self):
        """When everyone scored the same, previous ranks match current."""
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=50, total_points=500),
            _make_manager(name="Bob", entry_id=2, gw_points=50, total_points=400),
        ]
        _compute_standings_movement(managers)
        # Previous: Alice=450, Bob=350 -> Alice 1st, Bob 2nd (same as current)
        assert managers[0]["previous_rank"] == 1
        assert managers[1]["previous_rank"] == 2

    def test_dropped_manager_does_not_fabricate_movement(self):
        """A failed picks fetch must not renumber the managers below the gap.

        overall_rank comes from the unfiltered standings, so previous_rank has to
        be ranked over the same full league or every manager below the missing
        entry is reported as having dropped a place.
        """
        league_rows = [(i, 500 - i * 10, 50) for i in range(1, 6)]
        managers = [
            _make_manager(name=f"M{i}", entry_id=i, gw_points=50,
                          total_points=500 - i * 10, overall_rank=i)
            for i in (1, 2, 4, 5)  # entry 3 failed to fetch
        ]
        _compute_standings_movement(managers, league_rows)
        assert [m["previous_rank"] for m in managers] == [1, 2, 4, 5]
        assert all(m["previous_rank"] == m["overall_rank"] for m in managers)

    def test_replay_with_no_cumulative_totals_at_all_warns_once_and_accurately(self, caplog):
        """A draft replay carries no `total_points` for anyone, so every entry
        is unplaceable and nobody is ranked. One warning saying exactly that,
        not one per manager claiming the rest of the league is still ranked
        (PR #295 review)."""
        league_rows = [(i, 500 - i * 10, 50) for i in (1, 2, 3)]
        managers = [
            _make_manager(name=f"M{i}", entry_id=i, gw_points=50,
                          total_points=None, overall_rank=None, previous_rank=None)
            for i in (1, 2, 3)
        ]
        with caplog.at_level(logging.WARNING, logger="fpl_cli.cli._league_recap_data"):
            _compute_standings_movement(managers, league_rows, allow_standings_fallback=False)
        assert all("previous_rank" not in m for m in managers)
        warnings = [r.getMessage() for r in caplog.records]
        assert len(warnings) == 1
        assert "no manager had a point-in-time cumulative total" in warnings[0]
        assert "still ranked" not in warnings[0]

    def test_replay_aborting_on_a_later_entry_makes_no_still_ranked_claim(self, caplog):
        """Bob is missing a total and Cara never fetched: the cohort abort
        fires further down the standings, so the partial-manager warning must
        not have already promised the rest of the league was ranked."""
        league_rows = [(1, 500, 50), (2, 490, 50), (3, 480, 50)]
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=50, total_points=500,
                          overall_rank=None, previous_rank=None),
            _make_manager(name="Bob", entry_id=2, gw_points=50, total_points=None,
                          overall_rank=None, previous_rank=None),
        ]  # Cara (entry 3) failed to fetch entirely
        with caplog.at_level(logging.WARNING, logger="fpl_cli.cli._league_recap_data"):
            _compute_standings_movement(managers, league_rows, allow_standings_fallback=False)
        assert all("previous_rank" not in m for m in managers)
        assert not any("still ranked" in r.getMessage() for r in caplog.records)

    def test_tied_previous_totals_all_share_the_first_place(self):
        """Managers level on the previous table shared it, so none of them can
        be reported as having been above the others.

        (This is the shape GW1 would produce -- every previous total zero --
        but the collectors never call this function there: see
        TestFirstGameweekHasNoPreviousPosition.)"""
        league_rows = [(i, 500 + 80 - i, 80 - i) for i in range(1, 20)]
        managers = [
            _make_manager(name=f"M{i:02d}", entry_id=i, gw_points=80 - i,
                          total_points=500 + 80 - i, overall_rank=i)
            for i in range(1, 20)
        ]
        _compute_standings_movement(managers, league_rows)
        assert [m["previous_rank"] for m in managers] == [1] * 19

    def test_tied_previous_totals_survive_a_dropped_manager(self):
        """The all-tied case must survive a manager dropping out mid-table:
        the gap must not shift anyone off the place they shared."""
        league_rows = [(i, 500 + 80 - i, 80 - i) for i in range(1, 20)]
        managers = [
            _make_manager(name=f"M{i:02d}", entry_id=i, gw_points=80 - i,
                          total_points=500 + 80 - i, overall_rank=i)
            for i in range(1, 20) if i != 5
        ]
        _compute_standings_movement(managers, league_rows)
        assert [m["previous_rank"] for m in managers] == [1] * 18

    def test_a_manager_level_on_points_two_weeks_running_has_not_moved(self):
        """Movement is the difference between two tables, so both have to be
        ranked the same way. Ordinal previous ranks against competition
        current ones arrowed a tied manager up or down for a tie they never
        left (issue #164 review)."""
        league_rows = [(1, 500, 50), (2, 500, 50), (3, 400, 50)]
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=50, total_points=500),
            _make_manager(name="Bob", entry_id=2, gw_points=50, total_points=500),
            _make_manager(name="Charlie", entry_id=3, gw_points=50, total_points=400),
        ]
        _assign_point_in_time_positions(
            managers, [(1, 500), (2, 500), (3, 400)], allow_standings_fallback=True,
        )
        _compute_standings_movement(managers, league_rows)
        # Alice and Bob were level on 450 before this gameweek and are level on
        # 500 after it: neither passed the other, so neither may show an arrow.
        assert [m["overall_rank"] for m in managers] == [1, 1, 3]
        assert [m["previous_rank"] for m in managers] == [1, 1, 3]
        assert all(m["previous_rank"] == m["overall_rank"] for m in managers)

    def test_a_league_that_started_late_has_no_movement_on_its_first_gameweek(self):
        """A league created at GW12 has no table before GW12 either, even
        though GW11 exists for the rest of FPL."""
        assert _has_previous_gameweek(12, 12) is False
        assert _has_previous_gameweek(13, 12) is True

    def test_fetched_points_take_precedence_over_standings_row(self):
        """use_net_points adjusts gw_points on the entry; the standings row must not win."""
        league_rows = [(1, 500, 60), (2, 450, 0)]
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=40, total_points=500, overall_rank=1),
            _make_manager(name="Bob", entry_id=2, gw_points=0, total_points=450, overall_rank=2),
        ]
        _compute_standings_movement(managers, league_rows)
        # Alice's own figures give prev 460 (1st); the row's gross 60 would give 440 (2nd)
        assert managers[0]["previous_rank"] == 1
        assert managers[1]["previous_rank"] == 2

    def test_hit_this_gw_is_netted_out_of_previous_total(self):
        """total_points is always net of every hit; gw_points is gross unless
        use_net_points is on. Off, the hit must still be subtracted here or a
        manager who took it has their previous total over-credited."""
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=50, total_points=500, transfer_cost=8),
            _make_manager(name="Bob", entry_id=2, gw_points=10, total_points=460, transfer_cost=0),
        ]
        _compute_standings_movement(managers)
        # True previous totals: Alice 500-42=458, Bob 460-10=450 -> Alice was 1st.
        # The unfixed formula (500-50=450 vs 460-10=450) would report a tie.
        assert managers[0]["previous_rank"] == 1
        assert managers[1]["previous_rank"] == 2

    def test_use_net_points_mode_does_not_double_subtract_the_hit(self):
        """With use_net_points on, gw_points is already net; subtracting
        transfer_cost again would double-count the hit."""
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=42, total_points=500, transfer_cost=8),
            _make_manager(name="Bob", entry_id=2, gw_points=10, total_points=470, transfer_cost=0),
        ]
        _compute_standings_movement(managers, use_net_points=True)
        # True previous: Alice 500-42=458, Bob 470-10=460 -> Bob was 1st.
        # Double-subtracting the hit (500-(42-8)=466) would wrongly rank Alice 1st.
        assert managers[1]["previous_rank"] == 1
        assert managers[0]["previous_rank"] == 2


# ---------------------------------------------------------------------------
# Issue #147: the season's first gameweek has nothing to have moved from
# ---------------------------------------------------------------------------


class TestFirstGameweekHasNoPreviousPosition:
    """Neither collector may hand a GW1 manager a previous position.

    Derived, it is always their current one (every previous total is zero,
    so the tie-break gives everyone their place back); taken from draft's
    `last_rank`, it is whatever the API puts there before a table exists.
    Either way "held their position" is indistinguishable from "there was
    no previous gameweek" -- most damagingly in the ledger row, which
    outlives the API that could settle it.
    """

    async def test_classic_gw1_derives_no_previous_rank(self):
        standings = [
            {"entry": i, "player_name": f"M{i}", "event_total": 80 - i, "total": 80 - i}
            for i in range(1, 4)
        ]
        client = _FakeClassicClient(
            _standings_response(standings),
            {i: _picks_response(points=80 - i, total_points=80 - i) for i in range(1, 4)},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=1,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        assert [m["overall_rank"] for m in data["managers"]] == [1, 2, 3]
        assert all("previous_rank" not in m for m in data["managers"])

    async def test_classic_gw2_still_derives_movement(self):
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 20, "total": 100},
            {"entry": 2, "player_name": "Bob", "event_total": 70, "total": 90},
        ]
        client = _FakeClassicClient(
            _standings_response(standings),
            {
                1: _picks_response(points=20, total_points=100),
                2: _picks_response(points=70, total_points=90),
            },
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=2,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        # Previous totals: Alice 80, Bob 20 -- so Alice held 1st and Bob rose.
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Alice"]["previous_rank"] == 1
        assert by_name["Bob"]["previous_rank"] == 2

    async def _draft_data(self, *, gw: int, last_rank: int | None):
        """One live draft manager. `last_rank=None` omits the field, which is
        what sends the collector to its derived-movement fallback instead of
        trusting the league's own figure."""
        draft_player = make_draft_player(id=900, code=4900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, code=4900, web_name="Star", team_id=1)
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value={
            "league": {"name": "Draft League"},
            "standings": [
                {"league_entry": 10, "event_total": 52, "total": 500, "rank": 1}
                | ({} if last_rank is None else {"last_rank": last_rank}),
            ],
            "league_entries": [
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
            ],
        })
        client.get_bootstrap_static = AsyncMock(return_value={"elements": [draft_player]})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(
            return_value={"picks": [{"element": 900, "position": 1}], "subs": []},
        )
        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            return await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=gw, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=True,
            )

    async def test_draft_gw1_ignores_the_api_last_rank(self):
        data = await self._draft_data(gw=1, last_rank=1)
        assert data["managers"][0]["overall_rank"] == 1
        assert "previous_rank" not in data["managers"][0]

    async def test_draft_gw2_carries_the_api_last_rank(self):
        data = await self._draft_data(gw=2, last_rank=3)
        assert data["managers"][0]["previous_rank"] == 3

    async def test_draft_gw1_derives_no_movement_when_the_api_omits_last_rank(self):
        """Without `last_rank` the collector falls back to deriving movement
        itself, which is the path GW1 breaks: every previous total is zero,
        so each manager would be handed back their own current position."""
        data = await self._draft_data(gw=1, last_rank=None)
        assert data["managers"][0]["overall_rank"] == 1
        assert "previous_rank" not in data["managers"][0]

    async def test_draft_gw2_derives_movement_when_the_api_omits_last_rank(self):
        """The same fallback still runs once a previous gameweek exists --
        otherwise the test above would pass with the derivation removed."""
        data = await self._draft_data(gw=2, last_rank=None)
        assert data["managers"][0]["previous_rank"] == 1

    async def test_draft_treats_a_zero_last_rank_as_no_previous_table(self):
        """Zero is the API's "nobody stood anywhere yet" sentinel, not a
        position -- stored as one it renders as a drop from 0th."""
        data = await self._draft_data(gw=2, last_rank=0)
        assert "previous_rank" not in data["managers"][0]


# ---------------------------------------------------------------------------
# Per-manager fetch concurrency
# ---------------------------------------------------------------------------


class TestManagerFetchConcurrency:
    async def test_both_network_calls_respect_the_concurrency_cap(self):
        """Picks and transfers are two calls per manager; both must hold the permit."""
        live = {"picks": 0, "transfers": 0}
        peak = {"picks": 0, "transfers": 0}

        # Transfers are held open far longer than picks so that, if they are not
        # throttled, managers released by the picks semaphore pile into the
        # transfers stage faster than it drains and the peak exceeds the cap.
        ticks = {"picks": 1, "transfers": 30}

        class _FakeClient:
            async def _tracked(self, kind: str) -> None:
                live[kind] += 1
                peak[kind] = max(peak[kind], live[kind])
                for _ in range(ticks[kind]):
                    await asyncio.sleep(0)
                live[kind] -= 1

            async def get_manager_picks(self, entry_id: int, gw: int) -> dict:
                await self._tracked("picks")
                return {
                    "picks": [],
                    "entry_history": {"event_transfers": 2, "event_transfers_cost": 0},
                    "active_chip": None,
                    "automatic_subs": [],
                }

            async def get_manager_transfers(self, entry_id: int) -> list:
                await self._tracked("transfers")
                return []

        standings = [
            {"entry": i, "player_name": f"M{i:02d}", "event_total": 50, "total": 50}
            for i in range(1, 20)
        ]
        managers = await _fetch_all_manager_data(
            _FakeClient(), standings, 2, {}, {}, {},
        )

        assert len(managers) == 19
        assert peak["picks"] <= _PICKS_CONCURRENCY
        assert peak["transfers"] <= _PICKS_CONCURRENCY


# ---------------------------------------------------------------------------
# U1: classic point-in-time headline numbers
# ---------------------------------------------------------------------------


class _FakeClassicClient:
    """Fake FPLClient covering exactly what collect_classic_recap_data uses."""

    def __init__(
        self,
        standings_response: dict,
        picks_by_entry: dict[int, dict],
        transfers_by_entry: dict[int, list] | None = None,
        history_by_entry: dict[int, dict] | None = None,
    ):
        self._standings_response = standings_response
        self._picks_by_entry = picks_by_entry
        self._transfers_by_entry = transfers_by_entry or {}
        self._history_by_entry = history_by_entry or {}

    async def get_classic_league_standings(self, league_id, page=1):
        return self._standings_response

    async def get_manager_picks(self, entry_id, gameweek):
        return self._picks_by_entry[entry_id]

    async def get_manager_transfers(self, entry_id):
        return self._transfers_by_entry.get(entry_id, [])

    async def get_manager_history(self, entry_id):
        return self._history_by_entry.get(entry_id, {"current": []})


def _standings_response(rows: list[dict], start_event: int | None = None) -> dict:
    league: dict[str, Any] = {"name": "Test League"}
    if start_event is not None:
        league["start_event"] = start_event
    return {"league": league, "standings": {"results": rows}}


def _picks_response(points: int, total_points: int, transfers_cost: int = 0) -> dict:
    return {
        "picks": [],
        "entry_history": {
            "points": points,
            "total_points": total_points,
            "event_transfers_cost": transfers_cost,
            "event_transfers": 0,
        },
        "active_chip": None,
        "automatic_subs": [],
    }


class TestClassicHeadlineNumberSourcing:
    async def test_ae1_replay_reports_entry_history_not_standings(self):
        """Covers AE1: standings carry only the always-current numbers;
        entry_history carries the point-in-time truth for a replay."""
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 60, "total": 200}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: _picks_response(points=45, total_points=120)},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        m = data["managers"][0]
        assert m["gw_points"] == 45
        assert m["gross_points"] == 45
        assert m["total_points"] == 120

    async def test_picks_fetch_failure_manager_absent_but_others_unaffected(self):
        """A manager whose picks fetch raises is dropped from `managers`;
        `_compute_standings_movement`'s existing league_rows fallback (not
        touched by U1) still accounts for them without renumbering others."""
        class _RaisingClient(_FakeClassicClient):
            async def get_manager_picks(self, entry_id, gameweek):
                if entry_id == 2:
                    raise RuntimeError("boom")
                return await super().get_manager_picks(entry_id, gameweek)

        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 480},
        ]
        client = _RaisingClient(
            _standings_response(standings),
            {1: _picks_response(points=50, total_points=500)},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        assert [m["manager_name"] for m in data["managers"]] == ["Alice"]

    async def test_gross_field_identical_regardless_of_use_net_points(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500}]
        picks = {1: _picks_response(points=50, total_points=500, transfers_cost=8)}

        data_gross = await collect_classic_recap_data(
            _FakeClassicClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}, "use_net_points": False}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        data_net = await collect_classic_recap_data(
            _FakeClassicClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}, "use_net_points": True}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        assert data_gross["managers"][0]["gross_points"] == 50
        assert data_net["managers"][0]["gross_points"] == 50
        assert data_gross["managers"][0]["gw_points"] == 50
        assert data_net["managers"][0]["gw_points"] == 42

    async def test_missing_entry_history_on_a_live_capture_falls_back_to_standings(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 33, "total": 333}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: {"picks": [], "active_chip": None, "automatic_subs": []}},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        m = data["managers"][0]
        assert m["gross_points"] == 33
        assert m["total_points"] == 333

    async def test_missing_entry_history_on_a_replay_drops_the_manager(self, caplog):
        """Issue #272: the standings belong to a later gameweek, so there is
        nothing left to source this gameweek's headline numbers from. The
        manager is dropped as a failed picks fetch is, leaving the cohort to
        record them UNKNOWN rather than an OK row carrying the wrong era's
        points."""
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 33, "total": 333}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: {"picks": [], "active_chip": None, "automatic_subs": []}},
        )
        with caplog.at_level(logging.WARNING, logger="fpl_cli.cli._league_recap_data"):
            data = await collect_classic_recap_data(
                client, {"fpl": {"classic_league_id": 1}}, gw=10,
                live_stats={}, player_map={}, teams={}, is_live_gw=False,
            )
        assert data["managers"] == []
        # The cohort is what `build_history_rows` turns into the unknown row.
        assert [c["manager_name"] for c in data["standings_cohort"]] == ["Alice"]
        assert "Alice" in caplog.text
        assert "unknown" in caplog.text

    async def test_missing_entry_history_on_a_replay_leaves_the_rest_intact(self):
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 480},
        ]
        client = _FakeClassicClient(
            _standings_response(standings),
            {
                1: _picks_response(points=45, total_points=420),
                2: {"picks": [], "active_chip": None, "automatic_subs": []},
            },
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        assert [m["manager_name"] for m in data["managers"]] == ["Alice"]
        assert data["managers"][0]["gross_points"] == 45

    async def test_missing_cumulative_total_on_a_replay_is_left_unset(self):
        """A cumulative total has somewhere to land that gross points does
        not: `total_points` is optional, and unset reads as unavailable
        everywhere downstream."""
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 60, "total": 600}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: {
                "picks": [],
                "entry_history": {"points": 45, "event_transfers_cost": 0, "event_transfers": 0},
                "active_chip": None,
                "automatic_subs": [],
            }},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        m = data["managers"][0]
        assert m["gross_points"] == 45
        assert "total_points" not in m
        assert "overall_rank" not in m

    async def test_missing_cumulative_total_on_a_live_capture_falls_back_to_standings(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 45, "total": 600}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: {
                "picks": [],
                "entry_history": {"points": 45, "event_transfers_cost": 0, "event_transfers": 0},
                "active_chip": None,
                "automatic_subs": [],
            }},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        assert data["managers"][0]["total_points"] == 600

    async def test_reconciliation_does_not_run_for_a_past_gameweek(self):
        """A replay is allowed to diverge from (always-current) standings."""
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 999, "total": 999}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: _picks_response(points=45, total_points=120, transfers_cost=4)},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=5,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        assert data["managers"][0]["gross_points"] == 45

    async def test_reconciliation_raises_for_the_live_gameweek(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 999, "total": 999}]
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: _picks_response(points=45, total_points=120, transfers_cost=4)},
        )
        with pytest.raises(RecapReconciliationError):
            await collect_classic_recap_data(
                client, {"fpl": {"classic_league_id": 1}}, gw=5,
                live_stats={}, player_map={}, teams={}, is_live_gw=True,
            )


class TestReconcileClassicHeadlineNumbers:
    def test_inconclusive_when_no_manager_took_a_hit(self, caplog):
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=0, total_points=999)]
        standings = [{"entry": 1, "event_total": 999, "total": 999}]
        with caplog.at_level(logging.DEBUG, logger="fpl_cli.cli._league_recap_data"):
            _reconcile_classic_headline_numbers(managers, standings)  # must not raise
        assert "inconclusive" in caplog.text

    def test_raises_when_a_hit_taker_disagrees_naming_both_values(self):
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=8, total_points=500)]
        standings = [{"entry": 1, "event_total": 45, "total": 500}]
        with pytest.raises(RecapReconciliationError) as exc_info:
            _reconcile_classic_headline_numbers(managers, standings)
        assert "50" in str(exc_info.value)
        assert "45" in str(exc_info.value)

    def test_passes_silently_when_hit_taker_agrees(self):
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=8, total_points=500)]
        standings = [{"entry": 1, "event_total": 50, "total": 500}]
        _reconcile_classic_headline_numbers(managers, standings)  # must not raise

    def test_hit_taker_netted_in_standings_is_not_a_mapping_failure(self, caplog):
        """The FPL league table displays a gameweek net of the hit. A
        standings row exactly the hit lower than entry_history is that same
        gross/net difference, not the wrong field, and must not abort."""
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=4, total_points=500)]
        standings = [{"entry": 1, "event_total": 46, "total": 500}]
        with caplog.at_level(logging.INFO, logger="fpl_cli.cli._league_recap_data"):
            _reconcile_classic_headline_numbers(managers, standings)  # must not raise
        assert "net of the hit" in caplog.text

    def test_gap_that_is_not_the_hit_still_raises(self):
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=4, total_points=500)]
        standings = [{"entry": 1, "event_total": 30, "total": 500}]
        with pytest.raises(RecapReconciliationError):
            _reconcile_classic_headline_numbers(managers, standings)

    def test_cumulative_total_divergence_warns_not_raises(self, caplog):
        managers = [_make_manager(entry_id=1, gw_points=50, gross_points=50, transfer_cost=0, total_points=120)]
        standings = [{"entry": 1, "event_total": 50, "total": 500}]
        with caplog.at_level(logging.WARNING, logger="fpl_cli.cli._league_recap_data"):
            _reconcile_classic_headline_numbers(managers, standings)  # must not raise
        assert "divergence" in caplog.text


# ---------------------------------------------------------------------------
# U3: point-in-time league position
# ---------------------------------------------------------------------------


class TestDerivePointInTimePositions:
    def test_ranks_descending_by_total(self):
        result = derive_point_in_time_positions([(1, 100), (2, 150), (3, 90)])
        assert result == {2: 1, 1: 2, 3: 3}

    def test_entries_with_no_known_total_are_simply_omitted(self):
        result = derive_point_in_time_positions([(1, 100)])
        assert 2 not in result
        assert result == {1: 1}

    def test_tied_entries_share_a_place_and_the_next_total_skips_it(self):
        """Competition ranking, not ordinal: two managers level on points
        are genuinely indistinguishable here (classic breaks such a tie on
        fewest transfers, which nothing in the ledger records), so inventing
        an order for them would assert more than the data supports."""
        assert derive_point_in_time_positions([(1, 100), (2, 100), (3, 90)]) == {1: 1, 2: 1, 3: 3}

    def test_a_tie_ranks_the_same_whatever_order_it_arrives_in(self):
        """The input order is cohort standings order, which itself moves
        through the season -- so an ordinal tie-break made the same gameweek
        rank differently on a later backfill (issue #164 review)."""
        totals = [(1, 100), (2, 100), (3, 100), (4, 40)]
        assert derive_point_in_time_positions(totals) == derive_point_in_time_positions(
            list(reversed(totals)),
        )
        assert set(derive_point_in_time_positions(totals).values()) == {1, 4}

    def test_empty_input_returns_empty_mapping(self):
        assert derive_point_in_time_positions([]) == {}


class TestClassicLeaguePosition:
    async def test_ae1_positions_derived_from_totals_not_standings_order(self):
        """Covers AE1/R13: standings order (by event_total) differs from the
        point-in-time total order; overall_rank follows the latter."""
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 80, "total": 999},
            {"entry": 2, "player_name": "Bob", "event_total": 70, "total": 999},
            {"entry": 3, "player_name": "Charlie", "event_total": 60, "total": 999},
        ]
        picks = {
            1: _picks_response(points=80, total_points=300),
            2: _picks_response(points=70, total_points=320),
            3: _picks_response(points=60, total_points=310),
        }
        client = _FakeClassicClient(_standings_response(standings), picks)
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=15,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Bob"]["overall_rank"] == 1
        assert by_name["Charlie"]["overall_rank"] == 2
        assert by_name["Alice"]["overall_rank"] == 3

    async def test_no_start_event_makes_no_manager_history_call(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500}]
        history_calls: list[int] = []

        class _TrackedClient(_FakeClassicClient):
            async def get_manager_history(self, entry_id):
                history_calls.append(entry_id)
                return await super().get_manager_history(entry_id)

        client = _TrackedClient(
            _standings_response(standings),  # no start_event
            {1: _picks_response(points=50, total_points=500)},
        )
        await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        assert history_calls == []

    async def test_start_event_offset_applied_from_manager_history_baseline(self):
        """A league starting at GW5: ranking uses total_points minus each
        manager's season total as of GW4, not the raw season-wide total --
        matching the standings' own (already league-scoped) totals."""
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 200},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 210},
        ]
        # Season-wide order would put Alice ahead (500 > 480); league-scoped
        # order (matching the standings totals above) puts Bob ahead.
        picks = {
            1: _picks_response(points=50, total_points=500),
            2: _picks_response(points=40, total_points=480),
        }
        history = {
            1: {"current": [{"event": 4, "total_points": 300}]},
            2: {"current": [{"event": 4, "total_points": 270}]},
        }
        client = _FakeClassicClient(
            _standings_response(standings, start_event=5), picks, history_by_entry=history,
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Bob"]["overall_rank"] == 1
        assert by_name["Alice"]["overall_rank"] == 2
        assert by_name["Bob"]["total_points"] == 210
        assert by_name["Alice"]["total_points"] == 200


class TestClassicPositionCohort:
    async def test_live_gw_failed_fetch_does_not_renumber_the_managers_below(self):
        """A manager whose picks fetch fails still holds their place in the
        real table; on a live capture the standings row stands in for them so
        everyone below keeps their true position."""
        class _RaisingClient(_FakeClassicClient):
            async def get_manager_picks(self, entry_id, gameweek):
                if entry_id == 2:
                    raise RuntimeError("boom")
                return await super().get_manager_picks(entry_id, gameweek)

        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 450},
            {"entry": 3, "player_name": "Cara", "event_total": 30, "total": 400},
        ]
        picks = {
            1: _picks_response(points=50, total_points=500),
            3: _picks_response(points=30, total_points=400),
        }
        data = await collect_classic_recap_data(
            _RaisingClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Alice"]["overall_rank"] == 1
        # Cara is 3rd, not promoted to 2nd by Bob's absence.
        assert by_name["Cara"]["overall_rank"] == 3

    async def test_replay_failed_fetch_leaves_positions_unavailable(self):
        """On a replay the standings belong to a later gameweek, so they
        cannot stand in for the missing manager. Rather than rank one
        current-state total against point-in-time ones, no position is
        derived at all."""
        class _RaisingClient(_FakeClassicClient):
            async def get_manager_picks(self, entry_id, gameweek):
                if entry_id == 2:
                    raise RuntimeError("boom")
                return await super().get_manager_picks(entry_id, gameweek)

        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 450},
        ]
        picks = {1: _picks_response(points=50, total_points=300)}
        data = await collect_classic_recap_data(
            _RaisingClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        alice = data["managers"][0]
        assert "overall_rank" not in alice
        assert "previous_rank" not in alice

    async def test_replay_one_partial_response_only_costs_that_manager_a_position(self):
        """Bob's picks fetched fine but carry no `total_points`. He alone goes
        unranked; Alice and Cara, whose own responses were complete, keep
        their positions instead of being blanked with him (issue #294)."""
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 450},
            {"entry": 3, "player_name": "Cara", "event_total": 30, "total": 400},
        ]
        bob = _picks_response(points=40, total_points=0)
        del bob["entry_history"]["total_points"]
        picks = {
            1: _picks_response(points=50, total_points=320),
            2: bob,
            3: _picks_response(points=30, total_points=290),
        }
        data = await collect_classic_recap_data(
            _FakeClassicClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Alice"]["overall_rank"] == 1
        assert by_name["Cara"]["overall_rank"] == 2
        assert "overall_rank" not in by_name["Bob"]
        assert "previous_rank" not in by_name["Bob"]
        assert by_name["Alice"]["previous_rank"] == 1
        assert by_name["Cara"]["previous_rank"] == 2

    async def test_replay_start_offset_failure_only_costs_that_manager_a_position(self):
        """The same scoping when it is `_apply_league_start_offset` that drops
        a total: one failed history fetch leaves that manager unranked, not
        the whole league (issue #294)."""
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 200},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 210},
        ]
        picks = {
            1: _picks_response(points=50, total_points=500),
            2: _picks_response(points=40, total_points=480),
        }

        class _PartialHistoryClient(_FakeClassicClient):
            async def get_manager_history(self, entry_id):
                if entry_id == 2:
                    raise RuntimeError("boom")
                return await super().get_manager_history(entry_id)

        client = _PartialHistoryClient(
            _standings_response(standings, start_event=5), picks,
            history_by_entry={1: {"current": [{"event": 4, "total_points": 300}]}},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Alice"]["overall_rank"] == 1
        assert "overall_rank" not in by_name["Bob"]

    async def test_replay_with_a_complete_cohort_still_derives_positions(self):
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 450},
        ]
        picks = {
            1: _picks_response(points=50, total_points=300),
            2: _picks_response(points=40, total_points=320),
        }
        data = await collect_classic_recap_data(
            _FakeClassicClient(_standings_response(standings), picks),
            {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["Bob"]["overall_rank"] == 1
        assert by_name["Alice"]["overall_rank"] == 2

    async def test_gameweek_before_league_start_leaves_totals_unavailable(self):
        """Replaying GW3 of a league that started at GW5: there is no league
        table to place anyone on, and the GW4 baseline would drive every
        total negative and invert the order."""
        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 200},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 210},
        ]
        picks = {
            1: _picks_response(points=50, total_points=150),
            2: _picks_response(points=40, total_points=140),
        }
        history_calls: list[int] = []

        class _TrackedClient(_FakeClassicClient):
            async def get_manager_history(self, entry_id):
                history_calls.append(entry_id)
                return await super().get_manager_history(entry_id)

        client = _TrackedClient(
            _standings_response(standings, start_event=5), picks,
            history_by_entry={1: {"current": [{"event": 4, "total_points": 300}]}},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=3,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
        )
        assert history_calls == []
        for m in data["managers"]:
            assert "total_points" not in m
            assert "overall_rank" not in m


class TestApplyLeagueStartOffset:
    async def test_history_fetch_failure_drops_the_unscoped_total(self):
        """An un-offset season-wide total is on a different scale to the
        league-scoped ones: keeping it would rank that manager top and shift
        everyone below. It is dropped instead."""
        class _FailingHistoryClient:
            async def get_manager_history(self, entry_id):
                raise RuntimeError("network blip")

        managers = [_make_manager(entry_id=1, total_points=500)]
        await _apply_league_start_offset(_FailingHistoryClient(), managers, start_event=5)
        assert "total_points" not in managers[0]

    async def test_missing_baseline_row_drops_the_unscoped_total(self):
        class _NoBaselineClient:
            async def get_manager_history(self, entry_id):
                return {"current": [{"event": 6, "total_points": 999}]}

        managers = [_make_manager(entry_id=1, total_points=500)]
        await _apply_league_start_offset(_NoBaselineClient(), managers, start_event=5)
        assert "total_points" not in managers[0]

    async def test_one_failure_does_not_disturb_the_offset_managers(self):
        class _PartialClient:
            async def get_manager_history(self, entry_id):
                if entry_id == 2:
                    raise RuntimeError("network blip")
                return {"current": [{"event": 4, "total_points": 300}]}

        managers = [
            _make_manager(name="Alice", entry_id=1, total_points=500),
            _make_manager(name="Bob", entry_id=2, total_points=900),
        ]
        await _apply_league_start_offset(_PartialClient(), managers, start_event=5)
        assert managers[0]["total_points"] == 200
        assert "total_points" not in managers[1]


class TestStandingsMovementOptionalTotal:
    def test_manager_with_no_total_gets_no_previous_rank_and_others_unaffected(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=50, total_points=500),
            _make_manager(name="Bob", entry_id=2, gw_points=50, total_points=450),
            _make_manager(
                name="NoData", entry_id=3, gw_points=50,
                total_points=None, overall_rank=None, previous_rank=None,
            ),
        ]
        _compute_standings_movement(managers)
        assert "previous_rank" not in managers[2]
        # Alice prev=450, Bob prev=400 -> unaffected by NoData's exclusion.
        assert managers[0]["previous_rank"] == 1
        assert managers[1]["previous_rank"] == 2


class TestFormatStandingsBlockUnavailable:
    def test_missing_total_and_position_render_unavailable(self):
        """Covers AE8: an unreconstructable draft replay names both fields
        rather than rendering a blank or a zero."""
        managers = [
            _make_manager(
                name="Ghost", entry_id=1, gw_points=40,
                total_points=None, overall_rank=None, previous_rank=None,
            ),
        ]
        block = _format_standings_block(managers)
        assert "position unavailable" in block
        assert "total unavailable" in block

    def test_missing_total_only_still_renders_position(self):
        managers = [
            _make_manager(
                name="Ghost", entry_id=1, gw_points=40,
                total_points=None, overall_rank=3, previous_rank=None,
            ),
        ]
        block = _format_standings_block(managers)
        assert "3rd" in block
        assert "total unavailable" in block


# ---------------------------------------------------------------------------
# U2: draft point-in-time reconstruction
# ---------------------------------------------------------------------------


class TestDraftPointInTimeReconstruction:
    def _make_draft_client(
        self, league_details, bootstrap_elements, txns, picks_by_entry, draft_teams=(),
    ):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(
            return_value={"elements": bootstrap_elements, "teams": list(draft_teams)},
        )
        client.get_league_transactions = AsyncMock(return_value={"transactions": txns})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks_by_entry[entry_id])
        return client

    def _league_details(self, standings, entries):
        return {"league": {"name": "Draft League"}, "standings": standings, "league_entries": entries}

    async def test_ae1_computed_points_override_standings_event_total(self):
        """Covers AE1: event_total is always-current; the squad summed
        against live stats is the point-in-time truth."""
        draft_player = make_draft_player(id=900, code=4900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, code=4900, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 70, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=False,
            )
        m = data["managers"][0]
        assert m["gw_points"] == 52
        assert m["gross_points"] == 52

    @pytest.mark.parametrize(
        ("league", "expected"),
        [
            pytest.param(
                {"name": "Draft League", "drafts": [
                    {"id": 901, "event": 1, "draft_completed": "2026-08-13T19:47:59Z"},
                    {"id": 902, "event": 20, "draft_completed": None},
                ]},
                [
                    {"id": 901, "event": 1, "draft_completed": "2026-08-13T19:47:59Z"},
                    {"id": 902, "event": 20, "draft_completed": None},
                ],
                id="verbatim",
            ),
            pytest.param(
                {"name": "Draft League", "drafts": [{"id": 901}, "unreadable"]},
                [{"id": 901}, "unreadable"],
                id="unrecognised-entries-kept-for-the-snapshot-to-report",
            ),
            pytest.param({"name": "Draft League"}, None, id="absent"),
            pytest.param({"name": "Draft League", "drafts": None}, None, id="null"),
        ],
    )
    async def test_league_drafts_are_handed_on_for_the_picks_snapshot(self, league, expected):
        """#461: the snapshot reads `league.drafts` off the same league
        details the recap came from, rather than fetching them again, and
        verbatim: a missing list or an entry it cannot read is the snapshot's
        to report, not this collector's to drop quietly."""
        league_details = {
            **self._league_details(
                standings=[{"league_entry": 10, "event_total": 0, "total": 0}],
                entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
            ),
            "league": league,
        }
        picks = {1: {"picks": [], "subs": []}}
        client = self._make_draft_client(league_details, [], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={},
                players=[], teams={}, is_live_gw=False,
            )

        assert data["league_drafts"] == expected

    async def test_ae8_replay_leaves_total_and_position_unavailable(self):
        """Covers AE8: no ledger exists yet in Phase A, so a replay can never
        reconstruct a cumulative total or a league position for draft."""
        draft_player = make_draft_player(id=900, code=4900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, code=4900, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 52, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=False,
            )
        m = data["managers"][0]
        assert "total_points" not in m
        assert "overall_rank" not in m
        block = _format_standings_block(data["managers"])
        assert "position unavailable" in block
        assert "total unavailable" in block

    async def test_reconciliation_silent_when_computed_matches_standings(self):
        draft_player = make_draft_player(id=900, code=4900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, code=4900, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 52, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=True,
            )
        assert data["managers"][0]["gw_points"] == 52
        assert data["managers"][0]["total_points"] == 500

    async def test_reconciliation_raises_on_divergence_naming_manager(self):
        draft_player = make_draft_player(id=900, code=4900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, code=4900, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 999, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            with pytest.raises(RecapReconciliationError) as exc_info:
                await collect_draft_recap_data(
                    {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                    players=[main_player], teams={}, is_live_gw=True,
                )
        message = str(exc_info.value)
        assert "999" in message
        assert "52" in message
        assert "A B" in message

    async def test_unmatched_player_marked_and_not_silently_zero(self):
        """A draft player whose web-name/team match to a main player fails
        carries the unmatched marker rather than a silent, indistinguishable
        zero (U4's row shape relies on this to detect the difference)."""
        draft_player = make_draft_player(id=900, web_name="Mystery", team=1, element_type=3)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 0, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={},
                players=[], teams={}, is_live_gw=True,
            )
        squad_player = data["managers"][0]["squad"][0]
        assert squad_player["unmatched"] is True
        assert squad_player["points"] == 0

    async def test_an_unmatched_draft_player_falls_back_to_his_club_for_the_blank(self):
        """issue #169: with no main-game id there is nothing to look up in the
        gameweek's live data, so the club is all that is left to answer with.

        His draft club 1 is the main game's blanking club 2 (#423)."""
        draft_player = make_draft_player(id=900, web_name="Mystery", team=1, element_type=3)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 0, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(
            league_details, [draft_player], [], picks, draft_teams=_SWAPPED_DRAFT_TEAMS,
        )

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={},
                players=[], teams={1: make_team(id=1), 2: make_team(id=2)}, is_live_gw=True,
                bgw_team_ids=frozenset({2}), players_with_fixture=frozenset({900}),
            )
        squad_player = data["managers"][0]["squad"][0]
        assert squad_player["unmatched"] is True
        assert squad_player["had_fixture"] is False

    async def test_unmatched_player_shortfall_does_not_abort_the_recap(self):
        """One name/team mapping miss makes the computed sum short by
        construction. That is a known gap in the mapping, not the wrong
        field, so the live gameweek falls back to the standings number
        instead of killing the whole recap."""
        matched = make_draft_player(id=900, code=4900, web_name="Star", team=1, element_type=3)
        mystery = make_draft_player(id=901, web_name="Mystery", team=1, element_type=3)
        main_player = make_player(id=5, code=4900, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 70, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [
            {"element": 900, "position": 1},
            {"element": 901, "position": 2},
        ], "subs": []}}
        client = self._make_draft_client(league_details, [matched, mystery], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=True,
            )
        m = data["managers"][0]
        assert m["gw_points"] == 70
        assert [p["unmatched"] for p in m["squad"]] == [False, True]

    async def test_live_gw_keeps_the_leagues_own_rank_over_a_derived_one(self):
        """Draft h2h scores league points, so ties on `total` are common and
        the API breaks them on its own tiebreaks. A live capture uses the
        league's rank/last_rank rather than re-sorting `total`."""
        draft_player = make_draft_player(id=900, code=4900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, code=4900, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[
                {"league_entry": 10, "event_total": 52, "total": 6, "rank": 2, "last_rank": 1},
                {"league_entry": 11, "event_total": 52, "total": 6, "rank": 1, "last_rank": 3},
            ],
            entries=[
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
                {"id": 11, "entry_id": 2, "player_first_name": "C", "player_last_name": "D"},
            ],
        )
        picks = {
            1: {"picks": [{"element": 900, "position": 1}], "subs": []},
            2: {"picks": [{"element": 900, "position": 1}], "subs": []},
        }
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=True,
            )
        by_name = {m["manager_name"]: m for m in data["managers"]}
        assert by_name["A B"]["overall_rank"] == 2
        assert by_name["C D"]["overall_rank"] == 1
        assert by_name["A B"]["previous_rank"] == 1
        assert by_name["C D"]["previous_rank"] == 3

    async def test_standings_without_ranks_still_derive_movement(self):
        draft_player = make_draft_player(id=900, code=4900, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, code=4900, web_name="Star", team_id=1)
        league_details = self._league_details(
            standings=[{"league_entry": 10, "event_total": 52, "total": 500}],
            entries=[{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        )
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = self._make_draft_client(league_details, [draft_player], [], picks)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 52}},
                players=[main_player], teams={}, is_live_gw=True,
            )
        assert data["managers"][0]["previous_rank"] == 1


# ---------------------------------------------------------------------------
# compute_classic_awards
# ---------------------------------------------------------------------------


class TestClassicAwardsIntegration:
    def test_includes_transfer_awards(self):
        transfers = [
            RecapTransfer(
                player_in="Star", player_in_team="ARS", player_in_points=20,
                player_out="Bench", player_out_team="BOU", player_out_points=1,
                net=19, cost=0,
            ),
        ]
        managers = [
            _make_manager(name="Alice", transfers=transfers),
            _make_manager(name="Bob"),
        ]
        awards = _compute_shared_awards(managers, format_name="classic")
        assert "transfer_genius" in awards


# ---------------------------------------------------------------------------
# Waiver awards (draft)
# ---------------------------------------------------------------------------


def _make_manager_with_txns(
    name: str,
    transactions: list[RecapDraftTransaction],
    **kwargs,
) -> RecapManagerEntry:
    m = _make_manager(name=name, **kwargs)
    m["transactions"] = transactions
    return m


class TestWaiverAwards:
    def test_waiver_genius(self):
        txns = [
            RecapDraftTransaction(
                player_in="Star", player_in_team="ARS", player_in_points=18,
                player_out="Dud", player_out_team="LEI", player_out_points=1,
                net=17, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        assert "waiver_genius" in awards
        assert awards.get("waiver_genius", {}).get("manager_name") == "Alice"  # type: ignore[union-attr]

    def test_waiver_disaster(self):
        txns = [
            RecapDraftTransaction(
                player_in="Flop", player_in_team="WHU", player_in_points=0,
                player_out="Legend", player_out_team="MCI", player_out_points=15,
                net=-15, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        assert "waiver_disaster" in awards
        assert awards.get("waiver_disaster", {}).get("manager_name") == "Alice"  # type: ignore[union-attr]

    def test_disaster_detail_names_chain_endpoints_not_intermediate(self):
        # Manager runs Trossard out → Georginio → Dango. The literal worst raw
        # txn is Georginio-for-Trossard (-6), but Trossard's real replacement
        # is Dango (-1). The detail should name the endpoint pair.
        txns = [
            RecapDraftTransaction(
                player_in="Georginio", player_in_team="???", player_in_points=1,
                player_out="Trossard", player_out_team="???", player_out_points=7,
                net=-6, kind="w",
            ),
            RecapDraftTransaction(
                player_in="Dango", player_in_team="???", player_in_points=6,
                player_out="Georginio", player_out_team="???", player_out_points=1,
                net=5, kind="f",
            ),
            # Add a clear loss elsewhere so manager total is negative
            RecapDraftTransaction(
                player_in="Bust", player_in_team="???", player_in_points=0,
                player_out="Star", player_out_team="???", player_out_points=10,
                net=-10, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        detail = awards["waiver_disaster"]["detail"]
        assert "Bust in for Star" in detail  # the real worst (-10), unaffected by chain
        assert "Georginio" not in detail  # the intermediate must not appear

    def test_no_transactions_no_awards(self):
        managers = [_make_manager(name="Alice"), _make_manager(name="Bob")]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        assert "waiver_genius" not in awards
        assert "waiver_disaster" not in awards

    def test_draft_awards_via_shared(self):
        txns = [
            RecapDraftTransaction(
                player_in="Pick", player_in_team="EVE", player_in_points=10,
                player_out="Drop", player_out_team="NFO", player_out_points=3,
                net=7, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards = _compute_shared_awards(managers, format_name="draft")
        assert "waiver_genius" in awards
        assert "transfer_genius" not in awards
        assert "best_captain" not in awards
        assert "worst_captain" not in awards

    def test_waiver_genius_value_is_aggregate_with_no_hit_clause(self):
        # Draft has no transfer cost, so the detail should never mention "hit".
        txns = [
            RecapDraftTransaction(
                player_in="A", player_in_team="ARS", player_in_points=10,
                player_out="B", player_out_team="TOT", player_out_points=2,
                net=8, kind="w",
            ),
            RecapDraftTransaction(
                player_in="C", player_in_team="CHE", player_in_points=7,
                player_out="D", player_out_team="WHU", player_out_points=0,
                net=7, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        assert awards["waiver_genius"]["value"] == 15
        detail = awards["waiver_genius"]["detail"]
        assert "gained 15 net pts overall" in detail
        assert "+15 raw across 2 waivers" in detail
        assert "hit" not in detail
        assert "Best: A in for B (+8)" in detail
        assert "also C in for D (+7)" in detail

    def test_waiver_disaster_single_move_states_its_kind(self):
        # Single negative-net waiver. Draft always states the breakdown, even
        # for a lone move, so this reads "(1 waiver)" -- no "raw across"
        # (that's redundant at n=1) and no "hit" (waivers have no cost).
        txns = [
            RecapDraftTransaction(
                player_in="Flop", player_in_team="WHU", player_in_points=0,
                player_out="Legend", player_out_team="MCI", player_out_points=8,
                net=-8, kind="w",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        assert awards["waiver_disaster"]["value"] == -8
        detail = awards["waiver_disaster"]["detail"]
        assert "Alice lost 8 net pts overall (1 waiver)." in detail
        assert "Worst: Flop in for Legend (-8)." in detail
        assert "raw across" not in detail
        assert "hit" not in detail

    def test_waiver_genius_single_free_agent_states_its_kind(self):
        # Single positive-net free-agent pickup (kind "f"): the headline must
        # say "free agent", never fold it into "waiver".
        txns = [
            RecapDraftTransaction(
                player_in="Star", player_in_team="ARS", player_in_points=9,
                player_out="Dud", player_out_team="LEI", player_out_points=0,
                net=9, kind="f",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        detail = awards["waiver_genius"]["detail"]
        assert "Alice gained 9 net pts overall (1 free agent)." in detail
        assert "Best: Star in for Dud (+9)." in detail

    def test_waiver_genius_mixed_kinds_states_the_breakdown(self):
        # Two waivers, one free agent: the headline must count and label each
        # kind separately, in waiver/free-agent order, rather than reporting
        # "3 waivers".
        txns = [
            RecapDraftTransaction(
                player_in="A", player_in_team="ARS", player_in_points=6,
                player_out="B", player_out_team="TOT", player_out_points=0,
                net=6, kind="w",
            ),
            RecapDraftTransaction(
                player_in="C", player_in_team="CHE", player_in_points=5,
                player_out="D", player_out_team="WHU", player_out_points=0,
                net=5, kind="w",
            ),
            RecapDraftTransaction(
                player_in="E", player_in_team="EVE", player_in_points=4,
                player_out="F", player_out_team="NFO", player_out_points=0,
                net=4, kind="f",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        detail = awards["waiver_genius"]["detail"]
        assert "+15 raw across 3 moves: 2 waivers, 1 free agent" in detail

    def test_waiver_genius_all_free_agents_states_the_plural(self):
        txns = [
            RecapDraftTransaction(
                player_in="A", player_in_team="ARS", player_in_points=6,
                player_out="B", player_out_team="TOT", player_out_points=0,
                net=6, kind="f",
            ),
            RecapDraftTransaction(
                player_in="C", player_in_team="CHE", player_in_points=5,
                player_out="D", player_out_team="WHU", player_out_points=0,
                net=5, kind="f",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        detail = awards["waiver_genius"]["detail"]
        assert "+11 raw across 2 free agents" in detail

    def test_waiver_genius_unknown_kind_counts_as_other_move(self):
        # A kind outside {"w", "f"} (or missing entirely) must never be
        # folded into the waiver count.
        txns = [
            RecapDraftTransaction(
                player_in="A", player_in_team="ARS", player_in_points=6,
                player_out="B", player_out_team="TOT", player_out_points=0,
                net=6, kind="w",
            ),
            RecapDraftTransaction(
                player_in="C", player_in_team="CHE", player_in_points=5,
                player_out="D", player_out_team="WHU", player_out_points=0,
                net=5, kind="",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        detail = awards["waiver_genius"]["detail"]
        assert "+11 raw across 2 moves: 1 waiver, 1 other move" in detail

    def test_waiver_genius_counts_chain_intermediate_dropped_from_display(self):
        # Truffert in for Frimpong (waiver), Georginio in for Trossard
        # (waiver), Dango in for Georginio (free agent). The contracted
        # display collapses the last two into Dango in for Trossard and never
        # names Georginio, but the headline must still count all 3 raw moves
        # (2 waivers, 1 free agent) -- the count a reader checks against the
        # transactions page.
        txns = [
            RecapDraftTransaction(
                player_in="Truffert", player_in_team="???", player_in_points=7,
                player_out="Frimpong", player_out_team="???", player_out_points=1,
                net=6, kind="w",
            ),
            RecapDraftTransaction(
                player_in="Georginio", player_in_team="???", player_in_points=1,
                player_out="Trossard", player_out_team="???", player_out_points=2,
                net=-1, kind="w",
            ),
            RecapDraftTransaction(
                player_in="Dango", player_in_team="???", player_in_points=8,
                player_out="Georginio", player_out_team="???", player_out_points=1,
                net=7, kind="f",
            ),
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        detail = awards["waiver_genius"]["detail"]
        assert "+12 raw across 3 moves: 2 waivers, 1 free agent" in detail
        assert "Georginio" not in detail

    def test_waiver_detail_caps_at_three(self):
        txns = [
            RecapDraftTransaction(
                player_in=f"In{i}", player_in_team="ARS", player_in_points=10 - i,
                player_out=f"Out{i}", player_out_team="TOT", player_out_points=0,
                net=10 - i, kind="w",
            )
            for i in range(5)
        ]
        managers = [
            _make_manager_with_txns("Alice", txns),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        detail = awards["waiver_genius"]["detail"]
        assert "Best: In0 in for Out0 (+10)" in detail
        assert "2 more omitted" in detail
        assert "In4" not in detail


def _lost(pin: str, pout: str, kind: str = "w", priority: int | None = 1) -> RecapDraftLostClaim:
    return RecapDraftLostClaim(
        player_in=pin, player_in_team="???",
        player_out=pout, player_out_team="???",
        kind=kind, priority=priority,
    )


def _txn(pin: str, pin_pts: int, pout: str, pout_pts: int, kind: str = "w") -> RecapDraftTransaction:
    return RecapDraftTransaction(
        player_in=pin, player_in_team="???", player_in_points=pin_pts,
        player_out=pout, player_out_team="???", player_out_points=pout_pts,
        net=pin_pts - pout_pts, kind=kind,
    )


class TestFormatMoveCounts:
    """One rendering of a kind-count breakdown, shared by the award headline
    and the waiver roster (PR #303 review)."""

    def test_a_single_kind_reads_as_its_count_alone(self):
        assert format_move_counts([("transfer", 2)]) == "2 transfers"
        assert format_move_counts([("waiver", 2), ("free agent", 0), ("other move", 0)]) == "2 waivers"

    def test_a_lone_move_is_singular(self):
        assert format_move_counts([("free agent", 1)]) == "1 free agent"

    def test_mixed_kinds_lead_with_the_total(self):
        assert (
            format_move_counts([("waiver", 2), ("free agent", 1), ("other move", 0)])
            == "3 moves: 2 waivers, 1 free agent"
        )

    def test_empty_buckets_are_dropped_and_nothing_reads_as_nothing(self):
        assert format_move_counts([("waiver", 0), ("free agent", 0)]) == ""

    def test_the_award_and_the_roster_render_one_breakdown_alike(self):
        txns = [_txn("A", 9, "B", 1), _txn("C", 4, "D", 2, kind="f")]
        managers = [_make_manager_with_txns("Alice", txns), _make_manager(name="Bob")]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        rendered = format_move_counts(draft_transaction_kind_counts(txns))
        assert rendered == "2 moves: 1 waiver, 1 free agent"
        assert f"across {rendered}" in awards["waiver_genius"]["detail"]
        assert f"**Alice** ({rendered}; waivers +8, free agents +2, net" in format_recap_waivers_context(data)


def _lost_by(manager: RecapManagerEntry, *claims: RecapDraftLostClaim) -> RecapManagerEntry:
    manager["lost_claims"] = list(claims)
    return manager


class TestContestedDraftClaims:
    """Issue #330: a waiver is a race, and the recap reported only the
    winner. The gameweek's lost claims regrouped by player are the races,
    and the accepted move that brought the same player in names the winner."""

    def test_the_reported_race_names_the_winner_and_every_beaten_manager(self):
        """The reported shape: four managers claimed one player, two of them
        at priority 1, and the recap filed the winner's pickup as a shrug."""
        managers = [
            _make_manager_with_txns("Dan", [_txn("Elanga", 2, "Gravenberch", 2)], entry_id=4),
            _lost_by(_make_manager(name="Cam", entry_id=3), _lost("Elanga", "Bergvall", priority=2)),
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Elanga", "Dango", priority=1)),
            _lost_by(_make_manager(name="Alice", entry_id=1), _lost("Elanga", "Savio", priority=1)),
        ]
        [contest] = contested_draft_claims(managers)
        assert contest["player"] == "Elanga"
        assert contest["winner"] == "Dan"
        assert contest["claimants"] == 4
        assert [(c["manager_name"], c["priority"]) for c in contest["losers"]] == [
            ("Alice", 1), ("Bob", 1), ("Cam", 2),
        ]
        assert format_contested_claim(contest) == (
            "Elanga was claimed by 4 managers: Dan won him; "
            "Alice (priority 1), Bob (priority 1) and Cam (priority 2) were beaten to him."
        )

    def test_a_move_nobody_else_claimed_is_not_a_race(self):
        managers = [
            _make_manager_with_txns("Alice", [_txn("Wood", 6, "Watkins", 1)], entry_id=1),
            _make_manager(name="Bob", entry_id=2),
        ]
        assert contested_draft_claims(managers) == []

    def test_classic_rows_carry_no_claims_and_so_no_races(self):
        managers = [_make_manager(name="Alice", transfers=[_make_transfer("A", "B", 5, 1)])]
        assert contested_draft_claims(managers) == []

    def test_races_are_ordered_most_claimants_first_then_earliest_priorities(self):
        """Isidor's race was two managers; Elanga's was four. Between two
        races of equal size, the one more managers made a first choice
        leads, so the ordering says something about how hard each was fought."""
        managers = [
            _make_manager_with_txns("Dan", [_txn("Elanga", 2, "Gravenberch", 2)], entry_id=4),
            _make_manager_with_txns("Eve", [_txn("Isidor", 5, "Bowen", 2)], entry_id=5),
            _make_manager_with_txns("Fay", [_txn("Wood", 5, "Bowen", 2)], entry_id=6),
            _lost_by(_make_manager(name="Alice", entry_id=1), _lost("Elanga", "Savio", priority=1)),
            _lost_by(
                _make_manager(name="Bob", entry_id=2),
                _lost("Elanga", "Dango", priority=1), _lost("Wood", "Dango", priority=1),
            ),
            _lost_by(
                _make_manager(name="Cam", entry_id=3),
                _lost("Elanga", "Bergvall", priority=2), _lost("Isidor", "Richarlison", priority=5),
            ),
        ]
        contests = contested_draft_claims(managers)
        assert [(c["player"], c["claimants"]) for c in contests] == [
            ("Elanga", 4), ("Wood", 2), ("Isidor", 2),
        ]

    def test_a_race_whose_winner_was_not_fetched_is_still_reported_and_counts_nobody_unseen(self):
        """The winner's picks fetch failed, so no fetched manager's moves
        brought the player in. The race is still reported for the editorial
        -- with the winner unnamed, not dropped -- and its claimant count is
        the managers the recap can actually see, never a phantom winner."""
        managers = [
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Elanga", "Savio")),
            _lost_by(_make_manager(name="Cam", entry_id=3), _lost("Elanga", "Wood", priority=2)),
        ]
        [contest] = contested_draft_claims(managers)
        assert contest["winner"] is None
        assert contest["claimants"] == 2
        assert format_contested_claim(contest) == (
            "Elanga was claimed by Bob (priority 1) and Cam (priority 2), who were beaten "
            "to him; the winner could not be identified."
        )
        [alone] = contested_draft_claims(managers[:1])
        assert alone["claimants"] == 1
        assert format_contested_claim(alone) == (
            "Elanga was claimed by Bob (priority 1), who was beaten to him; "
            "the winner could not be identified."
        )

    def test_a_manager_who_lost_two_claims_on_one_player_is_named_once_at_their_best_priority(self):
        """A conditional chain offering two different drops for the same
        player is one manager wanting him once, not two claimants."""
        managers = [
            _make_manager_with_txns("Alice", [_txn("Elanga", 2, "Savio", 1)], entry_id=1),
            _lost_by(
                _make_manager(name="Bob", entry_id=2),
                _lost("Elanga", "Watkins", priority=3), _lost("Elanga", "Wood", priority=2),
            ),
        ]
        [contest] = contested_draft_claims(managers)
        assert contest["claimants"] == 2
        assert [(c["manager_name"], c["priority"]) for c in contest["losers"]] == [("Bob", 2)]

    def test_two_players_sharing_a_name_are_two_races(self):
        """Grouped by the stable code where the collector resolved one, so a
        second Silva is a second race rather than a bigger one."""
        alice = _make_manager_with_txns(
            "Alice", [_txn("Silva", 2, "A", 1), _txn("Silva", 5, "B", 1)], entry_id=1,
        )
        alice["transactions"][0]["player_in_code"] = 100
        alice["transactions"][1]["player_in_code"] = 200
        bob = _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Silva", "C", priority=1))
        bob["lost_claims"][0]["player_in_code"] = 200
        cam = _lost_by(_make_manager(name="Cam", entry_id=3), _lost("Silva", "D", priority=1))
        cam["lost_claims"][0]["player_in_code"] = 100
        contests = contested_draft_claims([alice, bob, cam])
        assert [
            (c["player_code"], c["winner"], [loser["manager_name"] for loser in c["losers"]])
            for c in contests
        ] == [(100, "Alice", ["Cam"]), (200, "Alice", ["Bob"])]

    def test_the_winner_is_never_also_among_the_beaten(self):
        """#341 review: once a manager's first claim for a player lands, any
        later claim of theirs for him is denied on the incoming side -- a
        `di`, since it is the incoming player that is gone -- so one manager
        can carry both an accepted move and a lost claim for one player.
        They wanted him once and got him; counting them twice read a two-way
        race as three claimants and named them on both sides."""
        managers = [
            _make_manager_with_txns("Alice", [_txn("Elanga", 2, "Savio", 1)], entry_id=1),
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Elanga", "Dango", priority=1)),
        ]
        _lost_by(managers[0], _lost("Elanga", "Wood", priority=2))
        [contest] = contested_draft_claims(managers)
        assert contest["claimants"] == 2
        assert [c["manager_name"] for c in contest["losers"]] == ["Bob"]
        assert format_contested_claim(contest) == (
            "Elanga was claimed by 2 managers: Alice won him; Bob (priority 1) was beaten to him."
        )

    def test_a_manager_beaten_only_by_their_own_accepted_claim_is_no_race(self):
        managers = [_make_manager_with_txns("Alice", [_txn("Elanga", 2, "Savio", 1)], entry_id=1)]
        _lost_by(managers[0], _lost("Elanga", "Wood", priority=2))
        assert contested_draft_claims(managers) == []

    def test_two_managers_sharing_a_name_are_told_apart(self):
        """Keyed by row, not display name: the winner's namesake still lost."""
        managers = [
            _make_manager_with_txns("Sam", [_txn("Elanga", 2, "Savio", 1)], entry_id=1),
            _lost_by(_make_manager(name="Sam", entry_id=2), _lost("Elanga", "Dango", priority=1)),
            _lost_by(_make_manager(name="Bob", entry_id=3), _lost("Elanga", "Wood", priority=1)),
        ]
        [contest] = contested_draft_claims(managers)
        assert contest["winner"] == "Sam"
        assert contest["claimants"] == 3
        assert [c["manager_name"] for c in contest["losers"]] == ["Bob", "Sam"]

    def test_a_winner_who_moved_the_player_on_again_still_won_him(self):
        """Read off the raw move list, not the awards' contracted one: B in
        for A and then C in for B is still a win of B."""
        managers = [
            _make_manager_with_txns("Alice", [_txn("B", 4, "A", 1), _txn("C", 9, "B", 4)], entry_id=1),
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("B", "Z")),
        ]
        [contest] = contested_draft_claims(managers)
        assert contest["winner"] == "Alice"

    def test_the_winner_is_the_accepted_move_of_the_kind_the_race_was_lost_in(self):
        """The waiver winner moved the player on and a rival signed him as a
        free agent before the deadline: two accepted moves bring him in, and
        the one the lost waiver claims were beaten by is the waiver."""
        managers = [
            _make_manager_with_txns("Cam", [_txn("Elanga", 2, "Wood", 1, kind="f")], entry_id=3),
            _make_manager_with_txns("Alice", [_txn("Elanga", 2, "Savio", 1, kind="w")], entry_id=1),
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Elanga", "X", kind="w", priority=1)),
        ]
        [contest] = contested_draft_claims(managers)
        assert contest["winner"] == "Alice"

    def test_a_free_agent_claim_is_tagged_by_kind_and_carries_no_priority(self):
        managers = [
            _make_manager_with_txns("Alice", [_txn("Elanga", 2, "Savio", 1, kind="f")], entry_id=1),
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Elanga", "Wood", kind="f", priority=None)),
        ]
        [contest] = contested_draft_claims(managers)
        assert format_contested_claim(contest) == (
            "Elanga was claimed by 2 managers: Alice won him; Bob (free agent) was beaten to him."
        )

    def test_a_waiver_claim_the_feed_sent_no_priority_for_is_named_bare(self):
        managers = [
            _make_manager_with_txns("Alice", [_txn("Elanga", 2, "Savio", 1)], entry_id=1),
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Elanga", "Wood", priority=None)),
        ]
        [contest] = contested_draft_claims(managers)
        assert format_contested_claim(contest) == (
            "Elanga was claimed by 2 managers: Alice won him; Bob was beaten to him."
        )


class TestMostContestedAward:
    """Issue #330: the third waiver award, about the league rather than one
    manager -- the player the most managers claimed, on a week where enough
    of them did to make a pile-up."""

    def test_the_threshold_is_a_pile_up_not_any_lost_claim(self):
        """Two claimants is every lost claim by definition; the award waits
        for at least two managers to have been beaten to the same player."""
        assert MOST_CONTESTED_MIN_CLAIMANTS == 3

    def _week(self) -> list[RecapManagerEntry]:
        return [
            _make_manager_with_txns("Dan", [_txn("Elanga", 2, "Gravenberch", 2)], entry_id=4),
            _make_manager_with_txns("Eve", [_txn("Isidor", 5, "Bowen", 2)], entry_id=5),
            _lost_by(_make_manager(name="Alice", entry_id=1), _lost("Elanga", "Savio", priority=1)),
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Elanga", "Dango", priority=1)),
            _lost_by(_make_manager(name="Cam", entry_id=3), _lost("Isidor", "Richarlison", priority=5)),
        ]

    def test_the_most_claimed_player_is_the_award(self):
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_most_contested_award(self._week(), awards)
        award = awards["most_contested"]
        assert award["manager_name"] == "Dan"
        assert award["value"] == 3
        assert award["detail"] == (
            "Elanga was claimed by 3 managers: Dan won him; "
            "Alice (priority 1) and Bob (priority 1) were beaten to him."
        )
        assert "Isidor" not in award["detail"]

    def test_no_race_means_no_award(self):
        managers = [_make_manager_with_txns("Alice", [_txn("A", 1, "B", 0)])]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_most_contested_award(managers, awards)
        assert "most_contested" not in awards

    def test_a_two_way_race_is_below_the_threshold_and_stays_editorial_colour(self):
        """One manager beaten to a player is the routine outcome of a waiver
        round, not a headline: no award, but the editorial's waiver roster
        still carries the race."""
        managers = [
            _make_manager_with_txns("Eve", [_txn("Isidor", 5, "Bowen", 2)], entry_id=5),
            _lost_by(_make_manager(name="Cam", entry_id=3), _lost("Isidor", "Richarlison", priority=1)),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_most_contested_award(managers, awards)
        assert "most_contested" not in awards
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        assert (
            "- Isidor was claimed by 2 managers: Eve won him; Cam (priority 1) was beaten to him."
        ) in format_recap_waivers_context(data)

    def test_the_shared_awards_carry_it_for_draft_only(self):
        assert "most_contested" in _compute_shared_awards(self._week(), format_name="draft")
        assert "most_contested" not in _compute_shared_awards(self._week(), format_name="classic")

    def test_a_race_nobody_can_be_seen_to_have_won_never_counts_towards_the_award(self):
        """Someone has to have won the player. Three managers beaten to him
        with no fetched manager's move bringing him in is editorial context,
        not a headline handing him to nobody the recap can name."""
        managers = [
            _lost_by(_make_manager(name=f"Loser{i}", entry_id=i), _lost("Elanga", "Savio", priority=i))
            for i in (1, 2, 3)
        ]
        awards = _compute_shared_awards(managers, format_name="draft")
        assert "most_contested" not in awards
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        assert (
            "- Elanga was claimed by Loser1 (priority 1), Loser2 (priority 2) and "
            "Loser3 (priority 3), who were beaten to him; the winner could not be identified."
        ) in format_recap_waivers_context(data)

    def test_an_unresolved_bigger_race_does_not_outrank_a_won_smaller_one(self):
        managers = [
            _make_manager_with_txns("Eve", [_txn("Isidor", 5, "Bowen", 2)], entry_id=5),
            _lost_by(_make_manager(name="Alice", entry_id=1), _lost("Isidor", "Savio", priority=1)),
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Isidor", "Dango", priority=1)),
        ] + [
            _lost_by(_make_manager(name=f"Loser{i}", entry_id=10 + i), _lost("Elanga", "Wood", priority=1))
            for i in range(1, 6)
        ]
        awards = _compute_shared_awards(managers, format_name="draft")
        award = awards["most_contested"]
        assert (award["manager_name"], award["value"]) == ("Eve", 3)
        assert award["detail"].startswith("Isidor was claimed by 3 managers: Eve won him;")
        assert "Elanga" not in award["detail"]

    def test_a_winners_own_denied_claim_never_lifts_a_race_over_the_threshold(self):
        """#341 review: counted twice, a two-way race cleared the threshold
        and took the headline it exists to withhold."""
        managers = [
            _make_manager_with_txns("Alice", [_txn("Elanga", 2, "Savio", 1)], entry_id=1),
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Elanga", "Dango", priority=1)),
        ]
        _lost_by(managers[0], _lost("Elanga", "Wood", priority=2))
        assert "most_contested" not in _compute_shared_awards(managers, format_name="draft")

    def test_a_tie_on_claimants_names_every_player_tied(self):
        managers = [
            _make_manager_with_txns("Dan", [_txn("Elanga", 2, "Gravenberch", 2)], entry_id=4),
            _make_manager_with_txns("Eve", [_txn("Isidor", 5, "Bowen", 2)], entry_id=5),
            _lost_by(
                _make_manager(name="Alice", entry_id=1),
                _lost("Elanga", "Savio", priority=1), _lost("Isidor", "Savio", priority=2),
            ),
            _lost_by(
                _make_manager(name="Bob", entry_id=2),
                _lost("Isidor", "Dango", priority=1), _lost("Elanga", "Dango", priority=2),
            ),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_most_contested_award(managers, awards)
        award = awards["most_contested"]
        assert award["manager_name"] == "Dan and Eve"
        assert award["value"] == 3
        assert award["detail"] == (
            "Elanga was claimed by 3 managers: Dan won him; "
            "Alice (priority 1) and Bob (priority 2) were beaten to him. "
            "Isidor was claimed by 3 managers: Eve won him; "
            "Bob (priority 1) and Alice (priority 2) were beaten to him."
        )

    def test_a_wide_tie_is_capped_like_the_other_awards(self):
        managers = [
            _make_manager_with_txns(
                "Win", [_txn(player, 2, f"out{i}", 1) for i, player in enumerate("ABCD")], entry_id=9,
            ),
        ] + [
            _lost_by(
                _make_manager(name=f"Loser{i}", entry_id=i),
                *(_lost(player, "Z", priority=1) for player in "ABCD"),
            )
            for i in (1, 2)
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_most_contested_award(managers, awards)
        detail = awards["most_contested"]["detail"]
        assert detail.count("was claimed by 3 managers") == 3
        assert detail.endswith(" 1 more player claimed by 3 managers omitted.")
        # One manager won every tied race: named once, never once per race,
        # and never for the race the cap dropped (#341 review).
        assert awards["most_contested"]["manager_name"] == "Win"


class TestContractDraftTxnChains:
    """Chain rebuilds within a manager-GW collapse to endpoint pairs."""

    def test_no_chain_passthrough(self):
        txns = [_txn("Truffert", 7, "Frimpong", 1), _txn("Doku", 15, "Gordon", 0)]
        assert _contract_draft_txn_chains(txns) == txns

    def test_single_txn_passthrough(self):
        txns = [_txn("Doku", 15, "Gordon", 0)]
        assert _contract_draft_txn_chains(txns) == txns

    def test_collapses_three_step_chain(self):
        # Trossard out → Georginio → Dango (Oliver's GW35)
        txns = [
            _txn("Truffert", 7, "Frimpong", 1),
            _txn("Georginio", 1, "Trossard", 7),
            _txn("Dango", 6, "Georginio", 1, kind="f"),
        ]
        result = _contract_draft_txn_chains(txns)
        # Expect Truffert/Frimpong unchanged, plus Dango/Trossard collapsed
        assert len(result) == 2
        names = {(t["player_in"], t["player_out"]): t["net"] for t in result}
        assert names[("Truffert", "Frimpong")] == 6
        assert names[("Dango", "Trossard")] == 6 - 7  # -1
        # Sum is preserved
        assert sum(t["net"] for t in result) == sum(t["net"] for t in txns)

    def test_closed_loop_contracts_to_nothing(self):
        # Drop X for Y, then drop Y for X — back where we started
        txns = [_txn("Y", 5, "X", 3), _txn("X", 3, "Y", 5)]
        assert _contract_draft_txn_chains(txns) == []

    def test_multi_hop_chain(self):
        # A→B→C→D: only A out, D in survive
        txns = [
            _txn("B", 4, "A", 1),
            _txn("C", 6, "B", 4),
            _txn("D", 9, "C", 6),
        ]
        result = _contract_draft_txn_chains(txns)
        assert len(result) == 1
        assert result[0]["player_in"] == "D"
        assert result[0]["player_out"] == "A"
        assert result[0]["net"] == 9 - 1


class TestBucketDraftTxns:
    """Regression: draft txn `entry` field is FPL entry_id, not league_entry id."""

    def test_remaps_entry_id_to_league_entry_id(self):
        league_entries = [
            {"id": 10, "entry_id": 10, "player_first_name": "A"},
            {"id": 20, "entry_id": 21, "player_first_name": "B"},
            {"id": 30, "entry_id": 31, "player_first_name": "C"},
        ]
        gw_txns = [
            {"entry": 10, "element_in": 1, "element_out": 2, "kind": "w"},
            {"entry": 21, "element_in": 3, "element_out": 4, "kind": "w"},
            {"entry": 21, "element_in": 5, "element_out": 6, "kind": "w"},
            {"entry": 31, "element_in": 7, "element_out": 8, "kind": "w"},
        ]

        bucketed = _bucket_draft_txns_by_league_entry(gw_txns, league_entries)

        assert set(bucketed.keys()) == {10, 20, 30}
        assert len(bucketed[20]) == 2
        assert len(bucketed[30]) == 1
        assert len(bucketed[10]) == 1

    def test_drops_unknown_entry_ids(self):
        league_entries = [{"id": 100, "entry_id": 200}]
        gw_txns = [
            {"entry": 200, "element_in": 1},
            {"entry": 999, "element_in": 2},
            {"entry": None, "element_in": 3},
        ]

        bucketed = _bucket_draft_txns_by_league_entry(gw_txns, league_entries)

        assert bucketed == {100: [{"entry": 200, "element_in": 1}]}


# ---------------------------------------------------------------------------
# Fines evaluation
# ---------------------------------------------------------------------------


class TestEvaluateLeagueFines:
    def _settings_with_fines(self, rules: list[dict] | None = None) -> dict:
        """Build settings with fines configured."""
        if rules is None:
            rules = [{"type": "last-place", "penalty": "Pint on video"}]
        return {"fines": {"classic": rules, "draft": rules}}

    def test_no_fines_config_returns_empty(self):
        managers = [_make_manager(name="Alice")]
        result = evaluate_league_fines(managers, {}, "classic").fines
        assert result == []

    def test_last_place_fine_triggered(self):
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        managers = [
            _make_manager(name="Alice", gw_points=80, squad=squad),
            _make_manager(name="Bob", gw_points=20, entry_id=2, squad=squad),
        ]
        result = evaluate_league_fines(managers, self._settings_with_fines(), "classic").fines
        assert len(result) == 1
        assert result[0]["manager_name"] == "Bob"
        assert result[0]["rule_type"] == "last-place"
        assert "Finished last" in result[0]["message"]
        assert "Pint on video" in result[0]["message"]

    def test_red_card_fine_triggered(self):
        red_player = _make_squad_player(name="Hothead", red_cards=1, contributed=True)
        squad = [red_player] + [_make_squad_player(name=f"P{i}") for i in range(10)]
        rules = [{"type": "red-card", "penalty": "Buy the round"}]
        managers = [
            _make_manager(name="Alice", squad=squad),
        ]
        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic").fines
        assert len(result) == 1
        assert result[0]["rule_type"] == "red-card"
        assert "Hothead" in result[0]["message"]

    def test_a_red_card_ruling_carries_the_players_it_names(self):
        """#176: the stored ruling has to hold a reference to who was fined.
        The message alone is only what FPL called him at ruling time."""
        red_player = _make_squad_player(
            name="Sávio", code=510_281, red_cards=1, contributed=True,
        )
        squad = [red_player] + [_make_squad_player(name=f"P{i}") for i in range(10)]
        rules = [{"type": "red-card", "penalty": "Buy the round"}]
        managers = [_make_manager(name="Alice", squad=squad)]

        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic").fines

        assert result[0]["players"] == [{"name": "Sávio", "code": 510_281}]

    def test_a_ruling_that_names_nobody_records_an_empty_list_not_silence(self):
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        managers = [
            _make_manager(name="Alice", gw_points=80, squad=squad),
            _make_manager(name="Bob", gw_points=20, entry_id=2, squad=squad),
        ]

        result = evaluate_league_fines(managers, self._settings_with_fines(), "classic").fines

        assert result[0]["players"] == []

    def test_red_card_fine_triggers_on_bench_boost_bench_slot(self):
        # BB-flagged bench slot counts as contributed; a red card there must fine.
        bb_bench_red = _make_squad_player(
            name="BBRed", red_cards=1, contributed=True, is_bench_boost_player=True,
        )
        squad = [bb_bench_red] + [_make_squad_player(name=f"P{i}") for i in range(10)]
        rules = [{"type": "red-card", "penalty": "Round"}]
        managers = [_make_manager(name="Alice", active_chip="bboost", squad=squad)]
        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic").fines
        assert len(result) == 1
        assert result[0]["rule_type"] == "red-card"
        assert "BBRed" in result[0]["message"]

    def test_red_card_on_non_bb_bench_does_not_trigger(self):
        # Pre-existing behaviour: benched red card without BB is not fined.
        bench_red = _make_squad_player(name="BenchRed", red_cards=1, contributed=False)
        squad = [bench_red] + [_make_squad_player(name=f"P{i}") for i in range(10)]
        rules = [{"type": "red-card", "penalty": "Round"}]
        managers = [_make_manager(name="Alice", squad=squad)]
        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic").fines
        assert result == []

    def test_fines_failure_for_one_manager_doesnt_affect_others(self):
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        managers = [
            _make_manager(name="Alice", gw_points=80, squad=squad),
            _make_manager(name="Bob", gw_points=20, entry_id=2, squad=[]),
        ]
        # Bob has empty squad - fines eval might fail for edge cases, but shouldn't crash
        result = evaluate_league_fines(managers, self._settings_with_fines(), "classic").fines
        # Should still get Alice's result (not last place, so no fine for her)
        # Bob is last place and should get fined despite empty squad
        assert any(r["manager_name"] == "Bob" for r in result)

    def test_a_manager_whose_evaluation_raises_is_not_reported_as_ruled(self):
        """Their fines are dropped silently, so counting them as ruled would
        record "every rule checked, none triggered" for a manager nothing was
        checked against -- a false acquittal (#165 review)."""
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        managers = [
            _make_manager(name="Alice", gw_points=80, squad=squad),
            _make_manager(name="Bob", gw_points=20, entry_id=2, squad=squad),
        ]
        from fpl_cli.cli import _league_recap_data

        real = _league_recap_data.evaluate_fines

        def _boom(config, format_name, league_data, team_data, **kwargs):
            if league_data and league_data.get("user_gw_points") == 20:
                raise ValueError("malformed squad")
            return real(config, format_name, league_data, team_data, **kwargs)

        with patch.object(_league_recap_data, "evaluate_fines", _boom):
            ruling = evaluate_league_fines(managers, self._settings_with_fines(), "classic")

        assert ruling.ruled_manager_keys == frozenset({1})
        assert ruling.fines == []

    def test_a_tie_for_last_fines_every_manager_level_on_it(self):
        """Which of a tied pair `min()` reached first used to decide who paid,
        and the other was recorded owing nothing (#336)."""
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=80, squad=squad),
            _make_manager(name="Bob", entry_id=2, gw_points=37, squad=squad),
            _make_manager(name="Cara", entry_id=3, gw_points=37, squad=squad),
        ]

        result = evaluate_league_fines(managers, self._settings_with_fines(), "classic").fines

        assert sorted(f["manager_key"] for f in result) == [2, 3]
        assert {f["rule_type"] for f in result} == {"last-place"}

    def test_a_tie_for_last_is_read_on_net_points_when_they_are_tracked(self):
        """Bob's hit is what levelled him with Cara, so gross would miss it."""
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=80, gross_points=80, squad=squad),
            _make_manager(
                name="Bob", entry_id=2, gw_points=37, gross_points=41,
                transfer_cost=4, squad=squad,
            ),
            _make_manager(name="Cara", entry_id=3, gw_points=37, gross_points=37, squad=squad),
        ]
        settings = self._settings_with_fines()
        settings["use_net_points"] = True

        result = evaluate_league_fines(managers, settings, "classic").fines

        assert sorted(f["manager_key"] for f in result) == [2, 3]

    def test_an_unconfigured_league_still_reports_every_manager_as_ruled(self):
        """"No rule was configured" is itself a ruling -- of nothing. Leaving
        the managers out would record silence instead."""
        managers = [_make_manager(name="Alice"), _make_manager(name="Bob", entry_id=2)]

        ruling = evaluate_league_fines(managers, {}, "classic")

        assert ruling.ruled_manager_keys == frozenset({1, 2})

    def test_the_message_names_the_handler_s_players_not_a_re_read_of_its_prose(self):
        """The recap message used to recover the names by splitting the
        handler's own message back apart. It takes them from the result now,
        so the two cannot disagree (issue #176)."""
        squad = [
            _make_squad_player(name="A (sic)", code=1, red_cards=1, contributed=True),
            _make_squad_player(name="B", code=2, red_cards=1, contributed=True),
        ]
        rules = [{"type": "red-card", "penalty": "Round"}]
        managers = [_make_manager(name="Alice", squad=squad)]

        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic").fines

        assert result[0]["message"] == "Red card in starting XI (A (sic), B). Round"

    def test_below_threshold_is_measured_on_gross_points_not_gross_plus_the_hit(self):
        """`gross_points` is already gross whatever `use_net_points` says.
        Adding the hit back to `gw_points` overshot it on the gross side of
        that flip, inflating the score the threshold was measured against and
        letting a fined week slip through (issue #136)."""
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        rules = [{"type": "below-threshold", "threshold": 30, "penalty": "Pint"}]
        managers = [
            _make_manager(name="Alice", gw_points=28, gross_points=28, transfer_cost=4, squad=squad),
        ]

        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic").fines

        assert [r["rule_type"] for r in result] == ["below-threshold"]

    def test_last_place_reports_the_real_gross_score_not_an_inflated_one(self):
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        rules = [{"type": "last-place", "penalty": "Pint"}]
        managers = [
            _make_manager(name="Alice", gw_points=60, gross_points=60, squad=squad),
            _make_manager(
                name="Bob", entry_id=2, gw_points=20, gross_points=20,
                transfer_cost=8, squad=squad,
            ),
        ]

        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic").fines

        assert [r["manager_name"] for r in result] == ["Bob"]

    def test_multiple_fines_for_same_manager(self):
        red_player = _make_squad_player(name="Hothead", red_cards=1, contributed=True)
        squad = [red_player] + [_make_squad_player(name=f"P{i}") for i in range(10)]
        rules = [
            {"type": "last-place", "penalty": "Pint"},
            {"type": "red-card", "penalty": "Round"},
        ]
        managers = [
            _make_manager(name="Alice", gw_points=80, squad=squad),
            _make_manager(name="Bob", gw_points=20, entry_id=2, squad=squad),
        ]
        result = evaluate_league_fines(managers, self._settings_with_fines(rules), "classic").fines
        bob_fines = [r for r in result if r["manager_name"] == "Bob"]
        # Bob should get last-place fine and red-card fine
        assert len(bob_fines) >= 1  # At least last-place
        rule_types = {f["rule_type"] for f in bob_fines}
        assert "last-place" in rule_types


_GW8_RULES = [
    {"type": "red-card", "penalty": "Pint on video"},
    {"type": "last-place", "penalty": "Pint on video"},
]


def _gw8_week(red_carded: int = 10) -> list[RecapManagerEntry]:
    """GW8 2026-27 classic in miniature (#504): `red_carded` managers started
    Palmer the week he was sent off, and one more finished last without him."""
    palmer = [_make_squad_player(name="Palmer", red_cards=1)] + [
        _make_squad_player(name=f"P{i}") for i in range(10)
    ]
    clean = [_make_squad_player(name=f"P{i}") for i in range(11)]
    managers = [
        _make_manager(name=f"Manager {chr(65 + i)}", entry_id=i + 1, gw_points=60 - i, squad=palmer)
        for i in range(red_carded)
    ]
    managers.append(_make_manager(name="Manager K", entry_id=99, gw_points=20, squad=clean))
    return managers


def _ruled(managers: list[RecapManagerEntry], rules: list[dict] | None = None) -> list[RecapFineResult]:
    """The fines as the recap really rules them, so the grouping is tested
    against messages `_recap_fine_message` wrote rather than hand-typed ones."""
    settings = {"fines": {"classic": rules or _GW8_RULES}}
    return evaluate_league_fines(managers, settings, "classic").fines


def _fine(
    name: str, rule_type: str, trigger: str, penalty: str = "Pint on video", key: int | None = None,
) -> RecapFineResult:
    """A ruling built by hand, in the shape `evaluate_league_fines` produces."""
    fine = RecapFineResult(
        manager_name=name, rule_type=rule_type, trigger=trigger, penalty=penalty,
        message=f"{trigger}. {penalty}" if penalty else f"{trigger}.",
    )
    if key is not None:
        fine["manager_key"] = key
    return fine


_PALMER = "Red card in starting XI (Palmer)"
_RICE = "Red card in starting XI (Rice)"
_LAST = "Finished last in the gameweek"


class TestRecapFineLines:
    """Issue #504: the Fines section groups by the fine, not one line per manager."""

    def test_gw8_collapses_eleven_fines_into_two_lines(self):
        fines = _ruled(_gw8_week())
        assert len(fines) == 11

        lines = recap_fine_lines(fines, ["red-card", "last-place"])

        assert [(line.lead, line.detail, line.grouped) for line in lines] == [
            (
                _PALMER,
                "10 managers: Manager A, Manager B, Manager C, Manager D, Manager E, "
                "Manager F, Manager G, Manager H, Manager I and Manager J. Pint on video",
                True,
            ),
            ("Manager K", "Finished last in the gameweek. Pint on video", False),
        ]

    def test_a_week_where_every_fine_differs_reads_exactly_as_before(self):
        fines = [
            _fine("Bob", "last-place", _LAST), _fine("Alice", "red-card", _PALMER), _fine("Cam", "red-card", _RICE),
        ]

        lines = recap_fine_lines(fines, ["red-card", "last-place"])

        assert [(line.lead, line.detail, line.grouped) for line in lines] == [
            (f["manager_name"], f["message"], False) for f in fines
        ]

    def test_red_cards_for_different_players_stay_separate_lines(self):
        lines = recap_fine_lines([
            _fine("A", "red-card", _PALMER), _fine("B", "red-card", _RICE), _fine("C", "red-card", _PALMER),
            _fine("D", "red-card", _RICE), _fine("E", "red-card", _RICE),
        ])

        assert [line.lead for line in lines] == [_RICE, _PALMER], "biggest group first"
        assert lines[0].detail == "3 managers: B, D and E. Pint on video"
        assert lines[1].detail == "2 managers: A and C. Pint on video"

    def test_equal_groups_follow_the_config_rule_order(self):
        fines = [_fine(n, "red-card", _PALMER) for n in ("A", "B")] + [
            _fine(n, "last-place", _LAST) for n in ("C", "D")
        ]

        lines = recap_fine_lines(fines, ["last-place", "red-card"])

        assert [line.lead for line in lines] == [_LAST, _PALMER]

    def test_single_fines_follow_every_shared_line_in_ruling_order(self):
        """Config order only ranks the shared lines; a single fine is never
        lifted above a group, even when its rule is configured first."""
        fines = [
            _fine("A", "last-place", _LAST), _fine("B", "red-card", _PALMER),
            _fine("C", "red-card", _RICE), _fine("D", "red-card", _PALMER),
        ]

        lines = recap_fine_lines(fines, ["last-place", "red-card"])

        assert [(line.lead, line.grouped) for line in lines] == [
            (_PALMER, True), ("A", False), ("C", False),
        ]

    def test_the_same_rule_with_different_penalties_stays_apart(self):
        lines = recap_fine_lines([_fine("A", "last-place", _LAST, "Pint"), _fine("B", "last-place", _LAST, "£5")])

        assert [line.grouped for line in lines] == [False, False]

    @pytest.mark.parametrize("penalty", ["Team buys a round", "Pay £5 each", "Pay £5 to the pot"])
    def test_the_penalty_reads_as_configured(self, penalty: str):
        """Free text from settings.yaml: suffixing it "each" turns a shared
        forfeit into one per manager, or doubles a word already there."""
        (line,) = recap_fine_lines([_fine(n, "red-card", _PALMER, penalty) for n in ("A", "B")])

        assert line.detail == f"2 managers: A and B. {penalty}"

    def test_a_group_with_no_penalty_ends_on_the_names(self):
        fines = _ruled(_gw8_week(red_carded=2), [{"type": "red-card", "penalty": ""}])

        (line,) = recap_fine_lines(fines)

        assert line.detail == "2 managers: Manager A and Manager B."

    def test_two_managers_sharing_a_name_keep_a_line_each(self):
        """"2 managers: Sam and Sam" tells a reader nothing."""
        fines = [_fine("Sam", "red-card", _PALMER, key=1), _fine("Sam", "red-card", _PALMER, key=2)]

        lines = recap_fine_lines(fines)

        assert [(line.lead, line.grouped) for line in lines] == [("Sam", False), ("Sam", False)]

    def test_one_manager_fined_twice_alike_is_not_counted_as_two(self):
        fines = [_fine("Sam", "last-place", _LAST, key=1), _fine("Sam", "last-place", _LAST, key=1)]

        lines = recap_fine_lines(fines)

        assert [(line.lead, line.grouped) for line in lines] == [("Sam", False), ("Sam", False)]

    def test_a_ruling_without_a_trigger_never_groups(self):
        """A rule the recap does not word itself, or a ruling built by hand
        with only a message, keeps the line it always had."""
        fines = [
            RecapFineResult(manager_name=n, rule_type="red-card", message=f"{_PALMER}. Pint") for n in ("A", "B")
        ]

        lines = recap_fine_lines(fines)

        assert [(line.lead, line.grouped) for line in lines] == [("A", False), ("B", False)]

    def test_a_player_name_with_brackets_still_groups(self):
        fines = _ruled([
            _make_manager(name=n, entry_id=i, squad=[_make_squad_player(name="Gabriel (ARS)", red_cards=1)])
            for i, n in enumerate(("A", "B"), start=1)
        ], [{"type": "red-card", "penalty": "Pint"}])

        (line,) = recap_fine_lines(fines)

        assert (line.lead, line.detail) == ("Red card in starting XI (Gabriel (ARS))", "2 managers: A and B. Pint")

    def test_the_same_red_carded_players_group_whatever_order_each_squad_lists_them(self):
        """The handler names offenders in squad order, so two managers who
        started the same pair read them in whichever order each picked them."""
        def squad(*first: str) -> list[RecapManagerPlayer]:
            return [_make_squad_player(name=n, red_cards=1) for n in first] + [
                _make_squad_player(name=f"P{i}") for i in range(9)
            ]

        fines = _ruled([
            _make_manager(name="A", entry_id=1, squad=squad("Palmer", "Rice")),
            _make_manager(name="B", entry_id=2, squad=squad("Rice", "Palmer")),
            _make_manager(name="C", entry_id=3, squad=squad("Palmer")),
        ], [{"type": "red-card", "penalty": "Pint"}])

        lines = recap_fine_lines(fines)

        assert [(line.lead, line.detail) for line in lines] == [
            ("Red card in starting XI (Palmer, Rice)", "2 managers: A and B. Pint"),
            ("C", "Red card in starting XI (Palmer). Pint"),
        ]

    def test_the_ruling_carries_the_message_it_was_written_from(self):
        fines = _ruled(_gw8_week())

        assert all(f["message"] == f"{f['trigger']}. {f['penalty']}" for f in fines)
        assert len(fines) == 11, "one record per manager per fine"


class TestFinesReport:
    """#504: the saved report prints the same grouped lines as the console."""

    async def _render(self, tmp_path: Path, data: dict[str, Any]) -> str:
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run(context={"report_type": "league-recap", "gameweek": 8, "data": data})
        assert result.data is not None
        return Path(result.data["report_path"]).read_text(encoding="utf-8")

    @pytest.mark.parametrize("fpl_format", ["classic", "draft"])
    async def test_gw8_renders_two_lines_not_eleven(self, tmp_path: Path, fpl_format: str):
        managers = _gw8_week()
        content = await self._render(tmp_path, {
            "gameweek": 8, "league_name": "Test League", "fpl_format": fpl_format,
            "managers": managers, "awards": {}, "fines": _ruled(managers),
            "fine_rules_evaluated": ["red-card", "last-place"],
        })

        section = content.split("# Fines\n\n", 1)[1].split("\n---", 1)[0].strip()
        assert section.splitlines() == [
            "- **Red card in starting XI (Palmer)** - 10 managers: Manager A, Manager B, Manager C, "
            "Manager D, Manager E, Manager F, Manager G, Manager H, Manager I and Manager J. "
            "Pint on video",
            "- **Manager K:** Finished last in the gameweek. Pint on video",
        ]


class TestFinesContextGroupsByFine:
    """#504: the editorial's Fines section reads the same grouped lines, so the
    forfeit reaches the model once per fine rather than once per manager."""

    def test_gw8_hands_the_model_two_fine_lines_not_eleven(self):
        managers = _gw8_week() + [
            _make_manager(name=name, entry_id=entry_id, gw_points=70)
            for name, entry_id in (("Manager L", 100), ("Manager M", 101))
        ]
        data = _make_recap_data(managers=managers, fines=_ruled(managers))
        data["fine_rules_evaluated"] = ["red-card", "last-place"]

        text = format_recap_fines_context(data)

        assert text.splitlines() == [
            "Total managers fined this gameweek: 11 of 13",
            "- Red card in starting XI (Palmer) - 10 managers: Manager A, Manager B, Manager C, "
            "Manager D, Manager E, Manager F, Manager G, Manager H, Manager I and Manager J. "
            "Pint on video",
            "- Manager K: Finished last in the gameweek. Pint on video",
            "Not fined this gameweek (2): Manager L, Manager M",
        ]

    def test_a_week_where_every_fine_differs_reads_exactly_as_before(self):
        fines = [
            _fine("Bob", "last-place", _LAST), _fine("Alice", "red-card", _PALMER), _fine("Cam", "red-card", _RICE),
        ]

        text = format_recap_fines_context(_make_recap_data(fines=fines))

        assert text.splitlines() == [f"- {f['manager_name']}: {f['message']}" for f in fines]

    def test_each_manager_on_a_shared_line_is_still_placed_in_their_own_season(self):
        fines = [_fine("Ada", "red-card", _PALMER, key=1), _fine("Bob", "red-card", _PALMER, key=2)]
        tally = SeasonFinesTally(
            season="2025-26", fpl_format="classic", league_id=1,
            through_gameweek=10, start_gameweek=1, rule_types=["red-card"],
            managers=[
                ManagerFineTally(
                    manager_key=key, manager_name=name, counts={"red-card": count},
                    fined_gameweeks=fined, ruled_gameweeks_by_rule={"red-card": _ALL_TEN},
                )
                for key, name, count, fined in ((1, "Ada", 1, [10]), (2, "Bob", 2, [3, 10]))
            ],
        )

        lines = format_recap_fines_context(_make_recap_data(fines=fines), tally).splitlines()

        assert lines[0] == "- Red card in starting XI (Palmer) - 2 managers: Ada and Bob. Pint on video"
        assert (
            "- Ada: this gameweek's red-card fine is Ada's first of the season. "
            "No earlier gameweek carries a red-card fine against Ada."
        ) in lines
        assert "- Bob: this gameweek's red-card fine is Bob's second of the season, this one included." in lines


class TestConfiguredFineRuleTypes:
    """What a capture records as ruled, so an empty `fines` list can be told
    apart from a gameweek nobody ruled (issue #136)."""

    def test_unconfigured_fines_report_an_empty_ruling(self):
        assert configured_fine_rule_types({}, "classic") == []

    def test_the_configured_rules_come_back_in_config_order(self):
        settings = {"fines": {"classic": [
            {"type": "below-threshold", "threshold": 25},
            {"type": "last-place"},
        ]}}
        assert configured_fine_rule_types(settings, "classic") == [
            "below-threshold", "last-place",
        ]

    def test_each_format_reports_only_its_own_rules(self):
        settings = {"fines": {
            "classic": [{"type": "red-card"}],
            "draft": [{"type": "last-place"}],
        }}
        assert configured_fine_rule_types(settings, "draft") == ["last-place"]


# ---------------------------------------------------------------------------
# CLI stop condition
# ---------------------------------------------------------------------------


class TestLeagueRecapCommandStopCondition:
    def test_reconciliation_failure_exits_non_zero(self):
        """A reconciliation failure is a stop condition, so the command must
        exit non-zero -- a scripted caller (gw-prep) has no other way to tell
        it apart from a successful run."""
        from click.testing import CliRunner

        from fpl_cli.cli.league_recap import league_recap_command

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_players = AsyncMock(return_value=[])
        client.get_teams = AsyncMock(return_value=[])
        client.get_gameweek_live = AsyncMock(return_value={"elements": []})
        client.get_fixtures = AsyncMock(return_value=[])
        # GW5 finished and still current: the live capture the reconciliation
        # is written for, and the only state in which it runs at all.
        client.get_gameweeks = AsyncMock(
            return_value=[{"id": 5, "finished": True, "is_current": True}]
        )
        client.get_season_year = AsyncMock(return_value=2026)
        client.get_gameweek_status = AsyncMock(return_value=make_gameweek_status(5))
        resolved = {"gw": 5, "gw_data": None, "api_current_gw_id": 5}

        with (
            patch("fpl_cli.cli.league_recap.get_settings", return_value={"fpl": {"classic_league_id": 1}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.cli.review._review_resolve_gw", AsyncMock(return_value=resolved)),
            patch(
                "fpl_cli.cli._league_recap_data.collect_classic_recap_data",
                AsyncMock(side_effect=RecapReconciliationError("numbers disagree")),
            ),
        ):
            result = CliRunner().invoke(league_recap_command, [])

        assert result.exit_code != 0
        assert "numbers disagree" in result.output


# ---------------------------------------------------------------------------
# LLM prompt formatting
# ---------------------------------------------------------------------------


def _make_recap_data(managers=None, awards=None, fines=None):
    """Build a minimal LeagueRecapData for prompt tests."""
    from fpl_cli.cli._league_recap_types import LeagueRecapData, RecapAwards

    data = LeagueRecapData(
        gameweek=10,
        league_name="Test League",
        fpl_format="classic",
        managers=managers or [],
        awards=awards or RecapAwards(),
    )
    if fines is not None:
        data["fines"] = fines
    return data


class TestPromptFormatting:
    def test_awards_context_includes_winners(self):
        awards = _compute_shared_awards([
            _make_manager(name="Alice", gw_points=80, captain="Salah", captain_points=15),
            _make_manager(name="Bob", gw_points=30, captain="Haaland", captain_points=2),
        ])
        data = _make_recap_data(awards=awards)
        text = format_recap_awards_context(data)
        assert "Alice" in text
        assert "Gw Winner" in text

    def test_standings_context_includes_movement(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=80, total_points=500, overall_rank=1, previous_rank=2),
            _make_manager(name="Bob", entry_id=2, gw_points=30, total_points=400, overall_rank=2, previous_rank=1),
        ]
        data = _make_recap_data(managers=managers)
        text = format_recap_standings_context(data)
        assert "Alice" in text
        assert "Bob" in text
        assert "|" in text  # markdown table

    def test_standings_context_names_previous_leader(self):
        """Issue #189: the previous leader is stated outright, so the model
        never has to infer it from the Prev column or the size of a fall --
        which is how the editorial wrongly credited the manager who fell
        furthest with having previously topped the table."""
        managers = [
            _make_manager(name="Faller", entry_id=1, gw_points=20, overall_rank=16, previous_rank=5),
            _make_manager(name="Leader", entry_id=2, gw_points=90, overall_rank=1, previous_rank=1),
        ]
        text = format_recap_standings_context(_make_recap_data(managers=managers))
        assert "Previous gameweek's leader (Prev rank 1): Leader" in text
        assert text.index("Previous gameweek's leader") < text.index("| Pos |")

    def test_standings_context_names_both_previous_leaders_when_tied(self):
        """`previous_rank` is competition ranking (`derive_point_in_time_positions`),
        so two managers level on points genuinely share Prev rank 1. Naming
        only one and telling the model to deny the other's claim would repeat
        this PR's own bug (PR #216 review)."""
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=50, overall_rank=2, previous_rank=1),
            _make_manager(name="Bob", entry_id=2, gw_points=40, overall_rank=3, previous_rank=1),
        ]
        text = format_recap_standings_context(_make_recap_data(managers=managers))
        assert "Previous gameweek's leaders (Prev rank 1, tied): Alice, Bob" in text
        assert "never credit just one of them alone" in text

    def test_standings_context_no_previous_leader_when_no_previous_gameweek(self):
        """Season opener / first captured gameweek: no manager has a
        previous_rank, so the section says so explicitly rather than letting
        the model invent a "previously led" claim for anyone."""
        managers = [
            _make_manager(name="Alice", entry_id=1, previous_rank=None),
            _make_manager(name="Bob", entry_id=2, previous_rank=None),
        ]
        text = format_recap_standings_context(_make_recap_data(managers=managers))
        assert "No previous gameweek's standings exist" in text

    def test_standings_context_sorts_unranked_last_like_the_report(self):
        """A manager with no derivable position sorts after every ranked one
        in both the prompt table and the rendered standings block -- the two
        must not disagree on order."""
        managers = [
            _make_manager(name="Ghost", entry_id=1, gw_points=60, total_points=None,
                          overall_rank=None, previous_rank=None),
            _make_manager(name="Alice", entry_id=2, gw_points=50, overall_rank=1, previous_rank=1),
        ]
        text = format_recap_standings_context(_make_recap_data(managers=managers))
        assert text.index("Alice") < text.index("Ghost")

    def test_standings_context_states_each_managers_gameweek_rank(self):
        """Issue #483: the table is in league order, so its last row sat Pos 8
        beside a GW Pts of 31 and the editorial wrote "finished eighth on 31"
        for a manager seventh on the week. GW Rank states the week's placing,
        level scores sharing one, so it is read rather than counted."""
        rows = [
            ("Ann", 39, 1, 382), ("Bea", 30, 2, 330), ("Cy", 45, 3, 300),
            ("Dee", 45, 4, 280), ("Eli", 31, 5, 252),
        ]
        managers = [
            _make_manager(name=name, entry_id=i, gw_points=pts, overall_rank=rank, previous_rank=rank,
                          total_points=total)
            for i, (name, pts, rank, total) in enumerate(rows, start=1)
        ]
        text = format_recap_standings_context(_make_recap_data(managers=managers))
        assert "| Pos | Prev | Manager | GW Pts | GW Rank | Total |" in text
        assert "| 5 | 5 | Eli | 31 | 4 | 252 |" in text
        assert "| 2 | 2 | Bea | 30 | 5 | 330 |" in text
        assert "| 3 | 3 | Cy | 45 | 1 | 300 |" in text
        assert "| 4 | 4 | Dee | 45 | 1 | 280 |" in text
        assert "| 1 | 1 | Ann | 39 | 3 | 382 |" in text
        assert "a gameweek placing is read from GW Rank, never from Pos" in text
        assert text.index("Columns: Pos is the league position") < text.index("| Pos |")

    def test_system_prompt_ties_each_placing_to_its_column(self):
        """Issue #483: a placing names whether it is for the week or in the
        league, and comes from the matching column."""
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert (
            "Take a gameweek placing from the GW Standings section's GW Rank column and a league "
            "placing from its Pos column, never one from the other"
        ) in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_standings_context_shows_chip_for_classic(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=80, total_points=500, overall_rank=1, active_chip="WC"),
            _make_manager(name="Bob", entry_id=2, gw_points=30, total_points=400, overall_rank=2),
        ]
        data = _make_recap_data(managers=managers)
        text = format_recap_standings_context(data)
        assert "[WC]" in text
        assert "Alice [WC]" in text
        # Bob has no chip - no tag
        assert "Bob [" not in text

    def test_standings_context_hides_chip_for_draft(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=80, total_points=500, overall_rank=1, active_chip="WC"),
        ]
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        text = format_recap_standings_context(data)
        assert "[WC]" not in text

    def test_chips_context_groups_by_chip_with_counts(self):
        # Deliberately unsorted input — formatter must order by gw_points desc within each chip
        managers = [
            _make_manager(name="Nova", entry_id=4, gw_points=46, active_chip="WC"),
            _make_manager(name="Theo", entry_id=2, gw_points=58, active_chip="WC"),
            _make_manager(name="Zara", entry_id=1, gw_points=70, active_chip="WC"),
            _make_manager(name="Idris", entry_id=3, gw_points=55, active_chip="WC"),
            _make_manager(name="Wren", entry_id=6, gw_points=76, active_chip="BB"),
            _make_manager(name="Milo", entry_id=5, gw_points=85, active_chip="BB"),
            _make_manager(name="Ines", entry_id=7, gw_points=60),
        ]
        data = _make_recap_data(managers=managers)
        text = format_recap_chips_context(data)
        assert "Wildcard" in text and "(4)" in text
        assert "Bench Boost" in text and "(2)" in text
        assert "Ines" not in text
        # Names must appear in descending gw_points order within each chip group
        wc_line = next(line for line in text.splitlines() if "Wildcard" in line)
        assert wc_line.index("Zara") < wc_line.index("Theo") < wc_line.index("Idris") < wc_line.index("Nova")
        bb_line = next(line for line in text.splitlines() if "Bench Boost" in line)
        assert bb_line.index("Milo") < bb_line.index("Wren")

    def test_chips_context_empty_when_no_chips(self):
        managers = [_make_manager(name="Alex", entry_id=1, gw_points=60)]
        data = _make_recap_data(managers=managers)
        assert format_recap_chips_context(data) == ""

    def test_chips_context_empty_for_draft(self):
        managers = [_make_manager(name="Cam", entry_id=1, gw_points=70, active_chip="WC")]
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        assert format_recap_chips_context(data) == ""

    def test_captains_context_groups_by_intended_pick(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, captain="Haaland", captain_points=7),
            _make_manager(name="Bob", entry_id=2, captain="Haaland", captain_points=7),
            _make_manager(name="Cam", entry_id=3, captain="Haaland", captain_points=7),
            _make_manager(name="Dave", entry_id=4, captain="Salah", captain_points=10),
            _make_manager(name="Eve", entry_id=5, captain="Gibbs-White", captain_points=4),
        ]
        data = _make_recap_data(managers=managers)
        text = format_recap_captains_context(data)
        assert text.startswith("Total captains: 5")
        # Modal pick first (×3), then alphabetical tiebreak between the two ×1 groups
        haaland_line_idx = next(i for i, ln in enumerate(text.splitlines()) if "Haaland" in ln)
        gibbs_line_idx = next(i for i, ln in enumerate(text.splitlines()) if "Gibbs-White" in ln)
        salah_line_idx = next(i for i, ln in enumerate(text.splitlines()) if "Salah" in ln)
        assert haaland_line_idx < gibbs_line_idx < salah_line_idx
        # Within Haaland group, managers alphabetical
        haaland_line = text.splitlines()[haaland_line_idx]
        assert haaland_line.index("Alice") < haaland_line.index("Bob") < haaland_line.index("Cam")
        assert "(×3)" in haaland_line
        assert "(7 pts)" in haaland_line

    def test_captains_context_annotates_dnp_with_vc_takeover(self):
        managers = [
            _make_manager(
                name="Alice",
                entry_id=1,
                captain="Haaland",
                captain_points=0,
                captain_played=False,
                vice_captain="Salah",
                vice_captain_points=12,
            ),
            _make_manager(name="Bob", entry_id=2, captain="Haaland", captain_points=7),
        ]
        data = _make_recap_data(managers=managers)
        text = format_recap_captains_context(data)
        assert "Alice (dnp; vice Salah scored 12 pts)" in text
        assert "Bob (7 pts)" in text

    def test_captains_context_prints_a_lone_point_in_the_singular(self):
        """A captain on exactly one point, or a vice who took over for one,
        reads "1 pt" like the chips and move rosters do -- one document, one
        unit."""
        managers = [
            _make_manager(name="Alice", entry_id=1, captain="Haaland", captain_points=1),
            _make_manager(
                name="Bob", entry_id=2, captain="Salah", captain_points=0, captain_played=False,
                vice_captain="Saka", vice_captain_points=1,
            ),
        ]
        text = format_recap_captains_context(_make_recap_data(managers=managers))
        assert "Alice (1 pt)" in text
        assert "Bob (dnp; vice Saka scored 1 pt)" in text
        assert "1 pts" not in text

    def test_captains_context_empty_for_draft(self):
        managers = [_make_manager(name="Cam", entry_id=1, captain="Haaland")]
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        assert format_recap_captains_context(data) == ""

    def test_captains_context_empty_when_no_managers(self):
        data = _make_recap_data(managers=[])
        assert format_recap_captains_context(data) == ""

    def test_synthesis_prompt_includes_chips_section(self):
        _, user = get_recap_synthesis_prompt(
            gw=32, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            chips_text="- **Wildcard** (4): Zara, Theo, Idris, Nova",
        )
        assert "## Chips Played" in user
        assert "Wildcard** (4)" in user

    def test_synthesis_system_prompt_locks_captain_outliers_to_section(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "## Captains" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "verbatim" in RECAP_SYNTHESIS_SYSTEM_PROMPT.lower()

    def test_synthesis_prompt_includes_captains_section_for_classic(self):
        _, user = get_recap_synthesis_prompt(
            gw=35, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            captains_text="Total captains: 3\n- **Haaland** (×2): Alice (7 pts), Bob (7 pts)",
        )
        assert "## Captains" in user
        assert "Total captains: 3" in user

    def test_synthesis_prompt_omits_captains_section_when_empty(self):
        _, user = get_recap_synthesis_prompt(
            gw=35, league_name="Test", fpl_format="draft",
            awards_text="x", standings_text="| t |", fines_text="",
            captains_text="",
        )
        assert "## Captains" not in user

    def test_transfers_context_lists_every_mover_with_moves_hit_and_net(self):
        """Issue #71: every mover, every move, the hit, the post-hit net, and
        who stood still -- not just the best and worst the awards name."""
        managers = [
            _make_manager(
                name="Alice", entry_id=1, transfer_cost=4, transfers_made=2,
                transfers=[
                    _make_transfer("Haaland", "Isak", 12, 3, cost=4),
                    _make_transfer("Saka", "Palmer", 2, 2, cost=4),
                ],
            ),
            _make_manager(name="Bob", entry_id=2, transfers_made=0, transfers=[]),
            _make_manager(
                name="Cam", entry_id=3, transfers_made=1,
                transfers=[_make_transfer("Wood", "Watkins", 1, 9)],
            ),
        ]
        text = format_recap_transfers_context(_make_recap_data(managers=managers))
        lines = text.splitlines()
        assert lines[0] == "Total managers who made transfers: 2 of 3"
        assert (
            "- **Alice** (2 transfers, -4 hit, net +5 overall after the hit): "
            "Haaland (12 pts) in for Isak (3 pts), +9; Saka (2 pts) in for Palmer (2 pts), +0"
        ) in lines
        assert "- **Cam** (1 transfer, no hit, net -8 overall): Wood (1 pt) in for Watkins (9 pts), -8" in lines
        assert lines[-1] == "Made no transfers (1): Bob"

    def test_transfers_context_orders_movers_best_net_first(self):
        managers = [
            _make_manager(name="Cam", entry_id=3, transfers=[_make_transfer("A", "B", 1, 9)]),
            _make_manager(name="Alice", entry_id=1, transfers=[_make_transfer("C", "D", 12, 3)]),
            _make_manager(name="Bob", entry_id=2, transfers=[_make_transfer("E", "F", 5, 5)]),
        ]
        text = format_recap_transfers_context(_make_recap_data(managers=managers))
        assert text.index("**Alice**") < text.index("**Bob**") < text.index("**Cam**")

    def test_transfers_context_ranks_on_the_post_hit_net(self):
        """+9 raw on a -8 hit ranks below +3 raw with no hit, the same figure
        the transfer awards are drawn from."""
        managers = [
            _make_manager(
                name="Alice", entry_id=1, transfer_cost=8,
                transfers=[_make_transfer("A", "B", 12, 3, cost=8)],
            ),
            _make_manager(name="Bob", entry_id=2, transfers=[_make_transfer("C", "D", 5, 2)]),
        ]
        text = format_recap_transfers_context(_make_recap_data(managers=managers))
        assert text.index("**Bob**") < text.index("**Alice**")
        assert "net +1 overall after the hit" in text

    def test_transfers_context_tags_the_chip_a_mover_played(self):
        managers = [
            _make_manager(
                name="Alice", entry_id=1, active_chip="WC", transfers_made=2,
                transfers=[_make_transfer("A", "B", 4, 1), _make_transfer("C", "D", 6, 2)],
            ),
        ]
        text = format_recap_transfers_context(_make_recap_data(managers=managers))
        assert "- **Alice** [WC] (2 transfers, no hit, net +7 overall):" in text

    def test_transfers_context_keeps_an_uncaptured_mover_among_the_movers(self):
        """A failed transfer fetch leaves the list empty while the API's count
        says they moved: they are a mover with unknown moves, never a manager
        who stood still."""
        managers = [
            _make_manager(name="Alice", entry_id=1, transfers=[_make_transfer("A", "B", 4, 1)]),
            _make_manager(name="Bob", entry_id=2, transfer_cost=4, transfers_made=3, transfers=[]),
            _make_manager(name="Cam", entry_id=3, transfers_made=0, transfers=[]),
        ]
        text = format_recap_transfers_context(_make_recap_data(managers=managers))
        assert text.startswith("Total managers who made transfers: 2 of 3")
        assert (
            "- **Bob** (3 transfers, -4 hit): the moves themselves were not captured, "
            "so who came in and who went out is unknown - name nobody"
        ) in text
        assert "Made no transfers (1): Cam" in text
        assert "Bob" not in text.splitlines()[-1]

    def test_transfers_context_withholds_the_net_when_the_captured_list_came_back_short(self):
        """The hit is charged for the whole gameweek, missing moves included,
        so a partial swing minus the whole hit is not a net to hand the model:
        the line gives the captured swing before the hit and calls the net
        unknown, and the manager lists after the fully captured movers however
        good that swing looks."""
        managers = [
            _make_manager(
                name="Alice", entry_id=1, transfer_cost=4, transfers_made=3,
                transfers=[_make_transfer("A", "B", 4, 1, cost=4), _make_transfer("C", "D", 6, 2, cost=4)],
            ),
            _make_manager(name="Bob", entry_id=2, transfers=[_make_transfer("E", "F", 1, 9)]),
        ]
        text = format_recap_transfers_context(_make_recap_data(managers=managers))
        assert (
            "- **Alice** (3 transfers, -4 hit; only 2 of the 3 moves were captured, +7 across "
            "those before the hit, so the gameweek's net is unknown): "
            "A (4 pts) in for B (1 pt), +3; C (6 pts) in for D (2 pts), +4"
        ) in text
        alice_line = next(line for line in text.splitlines() if "**Alice**" in line)
        assert "net +" not in alice_line
        assert text.index("**Bob**") < text.index("**Alice**")

    def test_transfers_context_omits_the_stayed_line_when_everyone_moved(self):
        managers = [_make_manager(name="Alice", entry_id=1, transfers=[_make_transfer("A", "B", 4, 1)])]
        text = format_recap_transfers_context(_make_recap_data(managers=managers))
        assert "Total managers who made transfers: 1 of 1" in text
        assert "Made no transfers" not in text

    def test_transfers_context_empty_when_nobody_transferred(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, transfers_made=0, transfers=[]),
            _make_manager(name="Bob", entry_id=2),
        ]
        assert format_recap_transfers_context(_make_recap_data(managers=managers)) == ""

    def test_transfers_context_empty_for_draft(self):
        managers = [_make_manager(name="Alice", entry_id=1, transfers=[_make_transfer("A", "B", 4, 1)])]
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        assert format_recap_transfers_context(data) == ""

    def test_transfers_context_empty_when_no_managers(self):
        assert format_recap_transfers_context(_make_recap_data(managers=[])) == ""

    def test_synthesis_prompt_includes_transfers_section_after_chips(self):
        _, user = get_recap_synthesis_prompt(
            gw=5, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            chips_text="- **Wildcard** (1): Alice",
            transfers_text="Total managers who made transfers: 1 of 2\n- **Alice** (1 transfer, no hit, net +3): A (4 pts) in for B (1 pts), +3",
            player_clubs_text="- A: Arsenal",
        )
        assert "## Transfers" in user
        assert "Total managers who made transfers: 1 of 2" in user
        assert user.index("## Chips Played") < user.index("## Transfers") < user.index("## Player Clubs")

    def test_synthesis_prompt_omits_transfers_section_when_empty(self):
        _, user = get_recap_synthesis_prompt(
            gw=5, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            transfers_text="",
        )
        assert "## Transfers" not in user

    def _draft_data(self, managers):
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = "draft"
        return data

    def test_waivers_context_lists_every_mover_with_kind_tags_and_net(self):
        """Issue #301: every draft mover with each move tagged by kind, the
        net, an explicit mover count, and who stood still -- not just the
        best and worst the two waiver awards name."""
        managers = [
            _make_manager_with_txns("Alice", [
                _txn("Savinho", 0, "Maddison", 1),
                _txn("Dango", 6, "Georginio", 1, kind="f"),
            ], entry_id=1),
            _make_manager(name="Bob", entry_id=2),
            _make_manager_with_txns("Cam", [_txn("Wood", 1, "Watkins", 9)], entry_id=3),
        ]
        text = format_recap_waivers_context(self._draft_data(managers))
        lines = text.splitlines()
        assert lines[0] == "Total managers who made waiver or free-agent moves: 2 of 3"
        assert (
            "- **Alice** (2 moves: 1 waiver, 1 free agent; waivers -1, free agents +5, net +4 overall): "
            "Savinho (0 pts) in for Maddison (1 pt), -1 [waiver]; "
            "Dango (6 pts) in for Georginio (1 pt), +5 [free agent]"
        ) in lines
        assert "- **Cam** (1 waiver, net -8 overall): Wood (1 pt) in for Watkins (9 pts), -8 [waiver]" in lines
        assert lines[-1] == "Made no moves and submitted no claims (1): Bob"

    def test_waivers_context_lists_a_chain_as_the_raw_moves_not_the_contracted_pair(self):
        """B in for A, then C in for B, is two moves the manager made. The
        awards contract them to C-for-A so their Best/Worst line never names
        a player the manager did not end the gameweek with; the roster is a
        record of activity, so it keeps both -- and the net is the same."""
        managers = [_make_manager_with_txns("Alice", [_txn("B", 4, "A", 1), _txn("C", 9, "B", 4)])]
        text = format_recap_waivers_context(self._draft_data(managers))
        assert (
            "- **Alice** (2 waivers, net +8 overall): "
            "B (4 pts) in for A (1 pt), +3 [waiver]; C (9 pts) in for B (4 pts), +5 [waiver]"
        ) in text
        assert "C (9 pts) in for A" not in text

    def test_waivers_context_orders_movers_best_net_first(self):
        managers = [
            _make_manager_with_txns("Cam", [_txn("A", 1, "B", 9)], entry_id=3),
            _make_manager_with_txns("Alice", [_txn("C", 12, "D", 3)], entry_id=1),
            _make_manager_with_txns("Bob", [_txn("E", 5, "F", 5)], entry_id=2),
        ]
        text = format_recap_waivers_context(self._draft_data(managers))
        assert text.index("**Alice**") < text.index("**Bob**") < text.index("**Cam**")

    def test_waivers_context_tags_an_unknown_kind_as_an_other_move(self):
        """The collector stores the API's `kind` verbatim, the empty string
        included; the roster labels what it cannot name rather than calling
        it a waiver -- the same fallback the awards' headline count uses."""
        managers = [_make_manager_with_txns("Alice", [_txn("A", 2, "B", 5, kind="")])]
        text = format_recap_waivers_context(self._draft_data(managers))
        assert "- **Alice** (1 other move, net -3 overall): A (2 pts) in for B (5 pts), -3 [other move]" in text

    def test_waivers_context_and_the_waiver_award_label_a_move_alike(self):
        """One vocabulary for one move (issue #301): the kind the roster tags
        a free-agent pickup with is the kind the award's headline counts it
        under, so the editorial never reads the same move two ways."""
        managers = [
            _make_manager_with_txns("Alice", [_txn("A", 9, "B", 1, kind="f")]),
            _make_manager(name="Bob"),
        ]
        awards: RecapAwards = {}  # type: ignore[typeddict-item]
        _compute_waiver_awards(managers, awards)
        text = format_recap_waivers_context(self._draft_data(managers))
        assert "(1 free agent, net +8 overall): A (9 pts) in for B (1 pt), +8 [free agent]" in text
        assert "(1 free agent)" in awards["waiver_genius"]["detail"]

    def test_a_manager_whose_only_activity_was_a_lost_claim_is_not_listed_as_inactive(self):
        """Issue #329: the reported failure. A manager went in at priority 1
        for Elanga and was beaten to him; the roster said he made no moves and
        the editorial called it sitting the waiver wire out."""
        managers = [
            _make_manager_with_txns("Alice", [_txn("Savinho", 4, "Maddison", 1)], entry_id=1),
            _make_manager(name="Bob", entry_id=2),
            _make_manager(name="Cam", entry_id=3),
        ]
        managers[1]["lost_claims"] = [_lost("Elanga", "Savio")]
        text = format_recap_waivers_context(self._draft_data(managers))
        assert (
            "Claimed a player but lost him to a rival, so ended with no move (1): "
            "Bob (claimed Elanga [waiver, priority 1])"
        ) in text
        # Scoped to the line itself: the contested block that closes the
        # section names Bob too, as the manager beaten to Elanga.
        [stayed] = [line for line in text.splitlines() if line.startswith("Made no moves")]
        assert stayed == "Made no moves and submitted no claims (1): Cam"

    def test_a_movers_lost_claims_are_listed_alongside_the_moves_that_landed(self):
        managers = [_make_manager_with_txns("Alice", [_txn("Wood", 6, "Watkins", 1)])]
        managers[0]["lost_claims"] = [_lost("Elanga", "Savio", priority=2)]
        text = format_recap_waivers_context(self._draft_data(managers))
        assert "also claimed and lost: Elanga [waiver, priority 2]" in text
        assert "Total managers who made waiver or free-agent moves: 1 of 1" in text

    def test_a_free_agent_claim_prints_no_priority(self):
        managers = [_make_manager(name="Alice", entry_id=1)]
        managers[0]["lost_claims"] = [_lost("Elanga", "Savio", kind="f", priority=None)]
        text = format_recap_waivers_context(self._draft_data(managers))
        assert "Alice (claimed Elanga [free agent])" in text
        assert "priority" not in text

    def test_the_section_is_written_when_the_only_activity_was_a_lost_claim(self):
        """Nobody moved, so the section used to be suppressed entirely -- and
        the editorial then had no waiver data at all to contradict it."""
        managers = [
            _make_manager(name="Alice", entry_id=1),
            _make_manager(name="Bob", entry_id=2),
        ]
        managers[0]["lost_claims"] = [_lost("Elanga", "Savio")]
        text = format_recap_waivers_context(self._draft_data(managers))
        assert text.splitlines()[0] == "Total managers who made waiver or free-agent moves: 0 of 2"
        assert "Alice (claimed Elanga [waiver, priority 1])" in text
        assert "Made no moves and submitted no claims (1): Bob" in text

    def test_waivers_context_closes_with_every_contested_player(self):
        """Issue #330: the outbid group says a manager lost a race; this says
        which race and to whom, in the sentence the award and report print."""
        managers = [
            _make_manager_with_txns("Dan", [_txn("Elanga", 2, "Gravenberch", 2)], entry_id=4),
            _make_manager_with_txns("Eve", [_txn("Isidor", 5, "Bowen", 2)], entry_id=5),
            _lost_by(_make_manager(name="Alice", entry_id=1), _lost("Elanga", "Savio", priority=1)),
            _lost_by(_make_manager(name="Bob", entry_id=2), _lost("Elanga", "Dango", priority=1)),
            _lost_by(_make_manager(name="Cam", entry_id=3), _lost("Isidor", "Richarlison", priority=5)),
        ]
        text = format_recap_waivers_context(self._draft_data(managers))
        lines = text.splitlines()
        assert lines[-3:] == [
            "Contested players (2) - claimed by more than one manager, so only one could have him:",
            "- Elanga was claimed by 3 managers: Dan won him; "
            "Alice (priority 1) and Bob (priority 1) were beaten to him.",
            "- Isidor was claimed by 2 managers: Eve won him; Cam (priority 5) was beaten to him.",
        ]
        # The outbid group still stands beside it: the two are complementary.
        assert (
            "Claimed a player but lost him to a rival, so ended with no move (3): "
            "Alice (claimed Elanga [waiver, priority 1]); Bob (claimed Elanga [waiver, priority 1]); "
            "Cam (claimed Isidor [waiver, priority 5])"
        ) in lines

    def test_waivers_context_has_no_contested_block_when_no_claim_was_lost(self):
        managers = [_make_manager_with_txns("Alice", [_txn("A", 4, "B", 1)])]
        text = format_recap_waivers_context(self._draft_data(managers))
        assert "Contested players" not in text

    def test_awards_context_includes_most_contested(self):
        awards = RecapAwards(most_contested=RecapAwardEntry(
            manager_name="Dan", value=4,
            detail="Elanga was claimed by 4 managers: Dan won him; Alice (priority 1) was beaten to him.",
        ))
        text = format_recap_awards_context(_make_recap_data(awards=awards))
        assert "- **Most Contested:** Elanga was claimed by 4 managers: Dan won him" in text

    def test_synthesis_system_prompt_pins_who_wanted_whom_to_the_contested_lines(self):
        system, _ = get_recap_synthesis_prompt(
            gw=3, league_name="Test", fpl_format="draft",
            awards_text="x", standings_text="| t |", fines_text="",
        )
        assert '"Contested players" lines' in system
        assert "the race was won on the league's waiver order" in system
        # The licence is for waiver races alone: a free-agent race was
        # first come first served (#341 review).
        assert "a race tagged (free agent) was first come first served" in system
        assert "Most Contested only the player the most managers claimed" in system

    def test_waivers_context_omits_the_stayed_line_when_everyone_moved(self):
        managers = [_make_manager_with_txns("Alice", [_txn("A", 4, "B", 1)])]
        text = format_recap_waivers_context(self._draft_data(managers))
        assert "Total managers who made waiver or free-agent moves: 1 of 1" in text
        assert "Made no moves" not in text

    def test_waivers_context_empty_when_nobody_moved(self):
        managers = [
            _make_manager_with_txns("Alice", [], entry_id=1),
            _make_manager(name="Bob", entry_id=2),
        ]
        assert format_recap_waivers_context(self._draft_data(managers)) == ""

    def test_waivers_context_empty_for_classic(self):
        managers = [_make_manager_with_txns("Alice", [_txn("A", 4, "B", 1)])]
        assert format_recap_waivers_context(_make_recap_data(managers=managers)) == ""

    def test_waivers_context_empty_when_no_managers(self):
        assert format_recap_waivers_context(self._draft_data([])) == ""

    def test_synthesis_prompt_includes_waivers_section_for_draft(self):
        _, user = get_recap_synthesis_prompt(
            gw=5, league_name="Test", fpl_format="draft",
            awards_text="x", standings_text="| t |", fines_text="",
            waivers_text=(
                "Total managers who made waiver or free-agent moves: 1 of 2\n"
                "- **Alice** (1 waiver, net +3): A (4 pts) in for B (1 pt), +3 [waiver]"
            ),
            player_clubs_text="- A: Arsenal",
        )
        assert "## Waivers and Free Agents" in user
        assert "Total managers who made waiver or free-agent moves: 1 of 2" in user
        assert user.index("## GW Standings") < user.index("## Waivers and Free Agents") < user.index("## Player Clubs")
        assert "## Transfers" not in user

    def test_synthesis_prompt_omits_waivers_section_when_empty(self):
        _, user = get_recap_synthesis_prompt(
            gw=5, league_name="Test", fpl_format="draft",
            awards_text="x", standings_text="| t |", fines_text="",
            waivers_text="",
        )
        assert "## Waivers and Free Agents" not in user

    def test_synthesis_system_prompt_names_the_draft_roster_as_the_source_of_truth(self):
        """#301: draft narration is pinned to the waiver roster rather than to
        the two waiver awards, the kind tag decides what a move is called, and
        the say-nothing fallback fires only when neither roster is present."""
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert (
            'the "## Waivers and Free Agents" section is the source of truth for waiver claims '
            "and free-agent signings"
        ) in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "never call a move tagged [free agent] a waiver claim" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "Do not infer waiver activity from the Awards section" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert (
            'If there is no "## Transfers" section, no "## Waivers and Free Agents" section '
            "and no transfers note"
        ) in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_fines_context_includes_triggered(self):
        from fpl_cli.cli._league_recap_types import RecapFineResult

        fines = [RecapFineResult(manager_name="Bob", rule_type="last-place", message="Bob finished last")]
        data = _make_recap_data(fines=fines)
        text = format_recap_fines_context(data)
        assert "Bob" in text
        assert "last" in text

    def test_fines_context_empty_when_no_fines(self):
        data = _make_recap_data()
        text = format_recap_fines_context(data)
        assert text == ""

    def test_synthesis_prompt_returns_tuple(self):
        system, user = get_recap_synthesis_prompt(
            gw=10, league_name="Test League", fpl_format="classic",
            awards_text="Alice won", standings_text="| table |",
            fines_text="Bob fined",
        )
        assert "newsletter" in system.lower()
        assert "Gameweek 10" in user
        assert "Alice won" in user
        assert "Bob fined" in user

    def test_gw1_prompt_states_that_no_transfers_exist(self):
        """GW1 squads are built pre-deadline, so the game records no transfers."""
        _, user = get_recap_synthesis_prompt(
            gw=1, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )
        assert "No transfers were made this gameweek" in user
        assert "GW1 squads are built before the" in user

    def test_later_gameweek_prompt_has_no_transfer_note(self):
        _, user = get_recap_synthesis_prompt(
            gw=2, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )
        assert "No transfers were made this gameweek" not in user

    def test_draft_gw1_prompt_has_no_classic_transfer_note(self):
        """Draft uses waivers, not transfers - the classic GW1 note must not appear."""
        _, user = get_recap_synthesis_prompt(
            gw=1, league_name="Test", fpl_format="draft",
            awards_text="x", standings_text="| t |", fines_text="",
        )
        assert "No transfers were made this gameweek" not in user

    def test_synthesis_system_prompt_fences_transfer_invention(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert 'treat the "## Transfers" section as the source of truth' in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "Do not infer transfer activity from the Awards section" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "not licence to invent" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_synthesis_system_prompt_binds_club_claims_to_the_data(self):
        """#150: clubs are supplied now, so the rule binds to them rather than
        banning the subject outright."""
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert (
            "Never state a club for a player other than the club given for them"
            in RECAP_SYNTHESIS_SYSTEM_PROMPT
        )
        assert "## Player Clubs" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "goes a season out of date" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_synthesis_prompt_carries_the_player_clubs_section(self):
        _, user = get_recap_synthesis_prompt(
            gw=10, league_name="Test", fpl_format="classic",
            awards_text="awards", standings_text="standings", fines_text="",
            player_clubs_text="- Gyokeres: Arsenal",
        )
        assert "## Player Clubs" in user
        assert "- Gyokeres: Arsenal" in user

    def test_synthesis_prompt_omits_player_clubs_when_unresolved(self):
        """No club map (a replay, or a team lookup that failed) leaves the
        section out, and the rule then forbids naming any club."""
        _, user = get_recap_synthesis_prompt(
            gw=10, league_name="Test", fpl_format="classic",
            awards_text="awards", standings_text="standings", fines_text="",
        )
        assert "## Player Clubs" not in user


    def test_synthesis_prompt_omits_fines_when_empty(self):
        _, user = get_recap_synthesis_prompt(
            gw=10, league_name="Test", fpl_format="draft",
            awards_text="awards", standings_text="standings",
            fines_text="",
        )
        assert "Fines" not in user


# ---------------------------------------------------------------------------
# U12: League History prompt section and season framing
# ---------------------------------------------------------------------------


def _history_entry(
    text: str = "Alice: 3 captain blanks in a row (GW4-GW6).",
    *,
    kind: NoteKind = NoteKind.STREAK,
    surfaces: frozenset[NoteSurface] = frozenset({NoteSurface.CONSOLE, NoteSurface.REPORT, NoteSurface.PROMPT}),
    window: GameweekWindow | None = GameweekWindow(start_gameweek=4, end_gameweek=6),
) -> NotesPackEntry:
    return NotesPackEntry(kind=kind, text=text, surfaces=surfaces, window=window, length=3)


def _history_pack(
    entries: list[NotesPackEntry] | None = None,
    coverage_entries: list[NotesPackEntry] | None = None,
    *,
    season_count_entries: list[NotesPackEntry] | None = None,
    season_record_entries: list[NotesPackEntry] | None = None,
    phase: SeasonPhase = SeasonPhase.MIDPOINT,
    phase_text: str = "GW20 is the season midpoint.",
    fpl_format: str = "classic",
) -> NotesPack:
    return NotesPack(
        season="2026-27", fpl_format=fpl_format, league_id=42, gameweek=20, phase=phase,
        league_start_gameweek=1,
        season_phase_entry=NotesPackEntry(
            kind=NoteKind.SEASON_PHASE, text=phase_text,
            surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
        ),
        entries=entries or [],
        season_count_entries=season_count_entries or [],
        season_record_entries=season_record_entries or [],
        coverage_entries=coverage_entries if coverage_entries is not None else [
            NotesPackEntry(
                kind=NoteKind.COVERAGE, text="Recorded history is complete from its start (GW1) through GW20.",
                surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
            ),
        ],
    )


class TestFormatRecapLeagueHistoryContext:
    def test_ae3_a_streak_renders_with_its_window(self):
        pack = _history_pack(entries=[_history_entry()])
        text = format_recap_league_history_context(pack)

        assert "Alice" in text
        assert "GW4-GW6" in text
        assert "Total League History streak entries: 1" in text

    def test_a_below_minimum_entry_is_withheld_from_the_prompt(self):
        entry = _history_entry(text="Alice: single blank", surfaces=frozenset())
        pack = _history_pack(entries=[entry])
        text = format_recap_league_history_context(pack)

        assert "single blank" not in text
        assert "Total League History streak entries: 0" in text

    def test_coverage_entries_always_render(self):
        pack = _history_pack(entries=[])
        text = format_recap_league_history_context(pack)

        assert "Recorded history is complete from its start (GW1) through GW20." in text

    def test_the_season_phase_line_is_included(self):
        pack = _history_pack(phase_text="GW38 is the season finale.")
        text = format_recap_league_history_context(pack)

        assert "GW38 is the season finale." in text

    def test_no_pack_at_all_states_absence_explicitly_rather_than_returning_empty(self):
        text = format_recap_league_history_context(None)

        assert text != ""
        assert "No league history" in text

    def test_streak_count_is_followed_only_by_streak_bullets_before_coverage_appears(self):
        """Both categories populated together -- the gap the earlier
        single-category tests never exercised. The streak count's N must
        bound exactly N bullets before any coverage-labeled text; a
        coverage caveat must not read as an uncounted (N+1)th streak."""
        streak_entries = [
            _history_entry(text="Alice: 3 captain blanks in a row (GW4-GW6)."),
            _history_entry(text="Bob: Hit run of 2 (GW5-GW6)."),
        ]
        coverage_entries = [
            NotesPackEntry(
                kind=NoteKind.COVERAGE,
                text="Recorded history is complete from its start (GW1) through GW20.",
                surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
            ),
            NotesPackEntry(
                kind=NoteKind.COVERAGE,
                text=(
                    "Carol: recorded history begins at GW10, later than the league's "
                    "start (GW1); earlier gameweeks are not available for this manager."
                ),
                surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
                manager_key=3,
                manager_name="Carol",
            ),
        ]
        pack = _history_pack(entries=streak_entries, coverage_entries=coverage_entries)
        text = format_recap_league_history_context(pack)
        lines = text.splitlines()

        count_index = lines.index("Total League History streak entries: 2")
        streak_bullet_lines = lines[count_index + 1 : count_index + 3]
        assert streak_bullet_lines == [
            "- Alice: 3 captain blanks in a row (GW4-GW6).",
            "- Bob: Hit run of 2 (GW5-GW6).",
        ]

        # Nothing coverage-labeled leaks into the counted streak lines.
        for line in streak_bullet_lines:
            assert "Coverage" not in line
            assert "Carol" not in line
            assert "Recorded history is complete" not in line

        # The very next non-blank content is the season-count total (its own
        # counted block, empty here), then the season-record total (empty
        # too), then the coverage label -- never a third, uncounted
        # streak-shaped bullet.
        remainder = [line for line in lines[count_index + 3 :] if line]
        assert remainder[0] == "Total season-count entries: 0"
        assert remainder[1] == "Total season-record entries: 0"
        assert remainder[2] == "Coverage:"
        assert remainder[3] == "- Recorded history is complete from its start (GW1) through GW20."
        assert remainder[4] == (
            "- Carol: recorded history begins at GW10, later than the league's "
            "start (GW1); earlier gameweeks are not available for this manager."
        )

    def test_a_surfaced_season_count_renders_under_its_own_total(self):
        count_entry = NotesPackEntry(
            kind=NoteKind.SEASON_COUNT,
            text="Alice: 4 gameweek wins this season (GW1-GW20), the latest this gameweek.",
            surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}),
            occurrences=4,
        )
        text = format_recap_league_history_context(
            _history_pack(season_count_entries=[count_entry]),
        )
        lines = text.splitlines()

        count_index = lines.index("Total season-count entries: 1")
        assert lines[count_index + 1] == (
            "- Alice: 4 gameweek wins this season (GW1-GW20), the latest this gameweek."
        )

    def test_season_records_render_under_their_own_total_before_coverage(self):
        """Issue #494: the season's highest and lowest gameweek score get
        their own counted block, so neither earlier count's scope changes."""
        records = [
            NotesPackEntry(
                kind=NoteKind.SEASON_RECORD,
                text=(
                    "The league's highest gameweek score this season is 72 points, scored twice: by "
                    "Alice in GW1 and by Bob in GW4 (GW1-GW20). No score this gameweek equalled it."
                ),
                surfaces=frozenset({NoteSurface.PROMPT}), points=72,
            ),
            NotesPackEntry(
                kind=NoteKind.SEASON_RECORD,
                text=(
                    "The league's lowest gameweek score this season is 18 points, scored once: by "
                    "Carol this gameweek (GW1-GW20)."
                ),
                surfaces=frozenset({NoteSurface.PROMPT}), points=18,
            ),
        ]
        lines = [
            line for line in format_recap_league_history_context(
                _history_pack(season_record_entries=records),
            ).splitlines() if line
        ]

        total = lines.index("Total season-record entries: 2")
        assert lines[total - 1] == "Total season-count entries: 0"
        assert lines[total + 1] == f"- {records[0].text}"
        assert lines[total + 2] == f"- {records[1].text}"
        assert lines[total + 3] == "Coverage:"

    def test_no_season_record_is_stated_as_a_zero_total_rather_than_left_out(self):
        text = format_recap_league_history_context(_history_pack())

        assert "Total season-record entries: 0" in text

    def test_system_prompt_holds_a_season_superlative_to_the_record_line(self):
        """Issue #494: "the week of the season" was said of a 65 in a season
        whose best was 72, with no section stating either."""
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert (
            'A gameweek score is "the week of the season", "the season\'s best", "a season low" or '
            'any other best or worst of the season only when a "## League History" season-record '
            "line names it"
        ) in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert (
            "streaks, season counts and the season's highest and lowest gameweek scores from the "
            '"## League History" section'
        ) in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_a_count_that_did_not_grow_this_gameweek_is_withheld_from_the_prompt(self):
        """A surfaceless season count (retained for `--format json` only)
        must not leak into the prompt as stale colour."""
        stale = NotesPackEntry(
            kind=NoteKind.SEASON_COUNT,
            text="Bob: 2 captain blanks this season (GW1-GW20).",
            surfaces=frozenset(),
            occurrences=2,
        )
        text = format_recap_league_history_context(
            _history_pack(season_count_entries=[stale]),
        )

        assert "Total season-count entries: 0" in text
        assert "Bob: 2 captain blanks" not in text


class TestLeagueHistoryPromptSection:
    def test_ae3_a_streak_renders_under_its_own_heading(self):
        pack = _history_pack(entries=[_history_entry()])
        _, user = get_recap_synthesis_prompt(
            gw=20, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            league_history_text=format_recap_league_history_context(pack),
        )

        assert "## League History" in user
        assert "GW4-GW6" in user

    def test_the_section_reads_each_entrys_prompt_sentence_not_its_ledger_line(self):
        """Issue #452: the model quotes what it is handed, so it is handed
        the sentence; an entry with no prompt form falls back to its text."""
        streak = NotesPackEntry(
            kind=NoteKind.STREAK, text="Alice: 3 captain blanks in a row (GW4-GW6).",
            prompt_text="Alice has 3 captain blanks in a row (GW4-GW6).",
            surfaces=frozenset({NoteSurface.CONSOLE, NoteSurface.REPORT, NoteSurface.PROMPT}), length=3,
        )
        count = NotesPackEntry(
            kind=NoteKind.SEASON_COUNT, text="Bob: 4 gameweek wins this season (GW1-GW6).",
            prompt_text="Bob has 4 gameweek wins this season (GW1-GW6).",
            surfaces=frozenset({NoteSurface.REPORT, NoteSurface.PROMPT}), occurrences=4,
        )
        bare = _history_entry("Carol: 2 last-place finishes in a row (GW5-GW6).")

        text = format_recap_league_history_context(
            _history_pack(entries=[streak, bare], season_count_entries=[count]),
        )

        assert "- Alice has 3 captain blanks in a row (GW4-GW6)." in text
        assert "- Bob has 4 gameweek wins this season (GW1-GW6)." in text
        assert "- Carol: 2 last-place finishes in a row (GW5-GW6)." in text
        assert "Alice:" not in text
        assert "Bob:" not in text

    def test_the_history_rules_appear_even_when_no_pack_was_supplied(self):
        """KTD9: the rule is unconditional -- present in the system prompt
        whether or not this call site even has a pack to inject."""
        system, _ = get_recap_synthesis_prompt(
            gw=20, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )

        assert "League History" in system
        assert "forbidden" in system.lower()

    def test_the_blanket_stick_to_gameweek_rule_names_its_bounded_exceptions(self):
        """League History for the league's own past; Prior Seasons (issue
        #131) for a manager's FPL record before it; Season Fines and Fines
        for where a fine sits in a season (#452, which rules 8, 11 and 12
        allowed while this rule forbade). Every one is named in the blanket
        rule itself, so none reads as a loophole in it."""
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert (
            "Stick to what happened this gameweek. The only facts spanning more than this gameweek"
            in RECAP_SYNTHESIS_SYSTEM_PROMPT
        )
        assert '"## League History" section' in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert '"## Prior Seasons" rule below' in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert 'season fine totals from the "## Season Fines" section' in RECAP_SYNTHESIS_SYSTEM_PROMPT
        # The previous-leader statement is the one cross-gameweek fact the
        # Standings section carries, so it is named as a source rather than
        # left contradicting the previous-leader rule (#452 review).
        assert "the GW Standings section's previous-leader statement" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "Beyond that statement, the Awards and GW Standings sections describe one gameweek" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_the_season_count_rule_frames_counts_as_optional_colour(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "season-count line" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "never derive or extrapolate a season total" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_a_held_run_must_not_be_simplified_to_consecutive(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "not recorded" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "never simplified to" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_research_summary_keeps_its_own_heading_alongside_league_history(self):
        pack = _history_pack(entries=[_history_entry()])
        _, user = get_recap_synthesis_prompt(
            gw=20, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            research_summary="Some research-role output.",
            league_history_text=format_recap_league_history_context(pack),
        )

        assert "## GW Context (from research)" in user
        assert "Some research-role output." in user
        assert "## League History" in user
        assert user.index("## League History") < user.index("## GW Context (from research)")

    def test_ae6_the_finale_framing_instruction_is_present(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "finale" in RECAP_SYNTHESIS_SYSTEM_PROMPT.lower()
        assert "season phase" in RECAP_SYNTHESIS_SYSTEM_PROMPT.lower()

    def test_a_draft_pack_has_no_captain_derived_entries(self):
        """Delegated to U8/U9's format filtering -- confirmed at the prompt
        consumption boundary too."""
        pack = _history_pack(
            entries=[_history_entry(text="Bob: Waiver win run of 2 (GW19-GW20).")],
            fpl_format="draft",
        )
        text = format_recap_league_history_context(pack)

        assert "Captain" not in text
        assert "Waiver win run" in text

    def test_the_rendered_prompt_states_the_packs_declared_entry_count(self):
        pack = _history_pack(entries=[_history_entry(), _history_entry(text="Bob: Hit run of 3 (GW1-GW3).")])
        _, user = get_recap_synthesis_prompt(
            gw=20, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            league_history_text=format_recap_league_history_context(pack),
        )

        assert "Total League History streak entries: 2" in user

    def test_no_league_history_section_when_the_caller_supplies_no_text(self):
        """Every other optional section already follows this convention;
        League History does too when a caller genuinely has nothing (an
        empty string is distinct from `format_recap_league_history_context`'s
        own always-non-empty output -- callers that actually run the
        formatter never hit this branch)."""
        _, user = get_recap_synthesis_prompt(
            gw=20, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )

        assert "## League History" not in user


# ---------------------------------------------------------------------------
# Standings block rendering (WhatsApp-friendly text format)
# ---------------------------------------------------------------------------


class TestFormatStandingsBlock:
    def test_sorts_by_gw_points_descending(self):
        managers = [
            _make_manager(name="Alice", gw_points=40, overall_rank=1, previous_rank=1, total_points=2000),
            _make_manager(name="Bob", gw_points=80, overall_rank=2, previous_rank=3, total_points=1900),
            _make_manager(name="Charlie", gw_points=60, overall_rank=3, previous_rank=2, total_points=1850),
        ]
        block = _format_standings_block(managers)
        lines = block.splitlines()
        assert lines[0].lstrip().startswith("1.")
        assert "Bob" in lines[0]
        assert "Charlie" in lines[1]
        assert "Alice" in lines[2]

    def test_tie_on_gw_points_resolves_by_overall_rank(self):
        managers = [
            _make_manager(name="Lower", gw_points=70, overall_rank=5, previous_rank=5),
            _make_manager(name="Higher", gw_points=70, overall_rank=2, previous_rank=2),
        ]
        block = _format_standings_block(managers)
        lines = block.splitlines()
        assert "Higher" in lines[0]
        assert "Lower" in lines[1]

    def test_level_gw_points_share_a_rank(self):
        """Issue #483: the row number is the gameweek placing, so two managers
        level on points share it rather than being numbered apart on league
        position, and the next score down skips the rank they took."""
        managers = [
            _make_manager(name="Lower", gw_points=70, overall_rank=5, previous_rank=5),
            _make_manager(name="Higher", gw_points=70, overall_rank=2, previous_rank=2),
            _make_manager(name="Last", gw_points=40, overall_rank=1, previous_rank=1),
        ]
        ranks = [line.split(".")[0].strip() for line in _format_standings_block(managers).splitlines()]
        assert ranks == ["1", "1", "3"]

    def test_rank_matches_the_prompt_tables_gw_rank(self):
        """Issue #483: the saved report and the prompt state one gameweek
        placing per manager, so a "joint-top for the week" the editorial reads
        off the prompt is what a reader finds in the report."""
        managers = [
            _make_manager(name="Ann Lee", entry_id=1, gw_points=45, overall_rank=3, previous_rank=3, total_points=300),
            _make_manager(name="Bea Gale", entry_id=2, gw_points=45, overall_rank=1, previous_rank=1, total_points=380),
            _make_manager(name="Cy Hart", entry_id=3, gw_points=31, overall_rank=2, previous_rank=2, total_points=340),
        ]
        assert _format_standings_block(managers).splitlines() == [
            " 1.  Bea Gale   45   (1st, 380)",
            " 1.  Ann Lee    45   (3rd, 300)",
            " 3.  Cy Hart    31   (2nd, 340)",
        ]
        prompt = format_recap_standings_context(_make_recap_data(managers=managers))
        assert "| 1 | 1 | Bea Gale | 45 | 1 | 380 |" in prompt
        assert "| 3 | 3 | Ann Lee | 45 | 1 | 300 |" in prompt
        assert "| 2 | 2 | Cy Hart | 31 | 3 | 340 |" in prompt

    def test_no_pipe_characters(self):
        managers = [_make_manager(name="Solo", gw_points=50)]
        assert "|" not in _format_standings_block(managers)

    def test_unchanged_rank_has_no_arrow(self):
        managers = [_make_manager(name="Stable", overall_rank=4, previous_rank=4)]
        block = _format_standings_block(managers)
        assert "↑" not in block
        assert "↓" not in block

    def test_climbed_rank_shows_up_arrow(self):
        managers = [_make_manager(name="Climber", overall_rank=3, previous_rank=7)]
        block = _format_standings_block(managers)
        assert "↑4" in block

    def test_dropped_rank_shows_down_arrow(self):
        managers = [_make_manager(name="Faller", overall_rank=8, previous_rank=5)]
        block = _format_standings_block(managers)
        assert "↓3" in block

    def test_chip_marker_appended_to_name(self):
        managers = [
            _make_manager(name="Short", gw_points=777, active_chip="BB"),
            _make_manager(name="LongerManagerName", gw_points=555),
        ]
        block = _format_standings_block(managers)
        assert "Short (BB)" in block
        # Padding adapts to longest display string: GW points column aligns.
        # Use distinctive values that can't collide with rank/ordinal/total substrings.
        lines = block.splitlines()
        pts_positions = [line.index(str(pts)) for line, pts in zip(lines, [777, 555], strict=True)]
        assert pts_positions[0] == pts_positions[1]

    def test_empty_string_active_chip_treated_as_no_chip(self):
        managers = [_make_manager(name="Solo", gw_points=50, active_chip="")]
        block = _format_standings_block(managers)
        assert "()" not in block
        assert "Solo" in block

    def test_ordinal_rendering(self):
        managers = [
            _make_manager(name=f"M{r}", gw_points=100 - r, overall_rank=r, previous_rank=r)
            for r in (1, 2, 3, 11, 12, 13, 21, 22, 23)
        ]
        block = _format_standings_block(managers)
        for expected in ("1st", "2nd", "3rd", "11th", "12th", "13th", "21st", "22nd", "23rd"):
            assert expected in block, f"missing ordinal {expected}"

    def test_empty_managers_returns_empty_string(self):
        assert _format_standings_block([]) == ""

    def test_includes_total_points(self):
        managers = [_make_manager(name="Solo", gw_points=50, total_points=1735)]
        assert "1735" in _format_standings_block(managers)


class TestLeagueRecapTemplateRender:
    async def test_rendered_recap_has_no_pipes_in_standings(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = {
            "gameweek": 10,
            "league_name": "Test League",
            "fpl_format": "classic",
            "managers": [
                _make_manager(name="Alice", gw_points=80, overall_rank=1, previous_rank=2, total_points=900, active_chip="WC"),
                _make_manager(name="Bob", gw_points=60, overall_rank=2, previous_rank=1, total_points=890),
            ],
            "awards": {},
        }
        result = await agent.run(context={
            "report_type": "league-recap",
            "gameweek": 10,
            "data": data,
        })
        assert result.data is not None
        report_path = result.data.get("report_path")
        assert report_path
        content = Path(report_path).read_text()

        standings_start = content.index("# GW Standings")
        standings_end = content.index("---", standings_start)
        standings_section = content[standings_start:standings_end]

        assert "|" not in standings_section
        # Arrow, chip, and ordinal survive the Jinja fenced-block render
        assert "↑1" in standings_section
        assert "(WC)" in standings_section
        assert "1st" in standings_section
        # Highest GW points appears first
        assert standings_section.index("Alice") < standings_section.index("Bob")
        # Block is wrapped in a fenced code block for Obsidian monospace
        assert "```" in standings_section


class TestLeagueRecapMostContestedRender:
    """Issue #330: the saved draft report carries the award beside the other
    two waiver awards, and nothing more about the week's races -- every race
    is editorial context, not report furniture."""

    _ELANGA = (
        "Elanga was claimed by 4 managers: Dan won him; "
        "Alice (priority 1), Bob (priority 1) and Cam (priority 2) were beaten to him."
    )

    async def _render(self, tmp_path, data):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run(context={"report_type": "league-recap", "gameweek": 3, "data": data})
        assert result.data is not None
        return Path(result.data["report_path"]).read_text()

    def _draft_data(self, **extra):
        return {
            "gameweek": 3,
            "league_name": "Draft League",
            "fpl_format": "draft",
            "managers": [
                _make_manager(name="Dan", gw_points=50, overall_rank=1, previous_rank=1, total_points=150),
            ],
            "awards": {},
            **extra,
        }

    async def test_the_award_renders_under_awards_and_nowhere_else(self, tmp_path):
        content = await self._render(tmp_path, self._draft_data(
            awards={"most_contested": {"manager_name": "Dan", "value": 4, "detail": self._ELANGA}},
        ))
        assert f"**Most Contested:** {self._ELANGA}" in content
        assert content.count("Elanga was claimed by") == 1
        assert "Contested Claims" not in content

    async def test_no_award_means_no_mention(self, tmp_path):
        content = await self._render(tmp_path, self._draft_data())
        assert "Most Contested" not in content


# ---------------------------------------------------------------------------
# Best legal XI reselection (issue #350)
# ---------------------------------------------------------------------------


def _names(players: tuple[RecapManagerPlayer, ...]) -> list[str]:
    return [p["name"] for p in players]


class TestBestXISelection:
    """#350: what the bench cost is the best legal XI's gain over the one
    that played -- one keeper, eight formations, the doubled player held."""

    def test_names_the_swap_highest_scorer_first(self):
        pick = best_xi_selection(_overstated_bench(), captain_played=True)
        assert pick.gain == 23
        assert _names(pick.benched) == ["Groß", "Gvardiol"]
        assert _names(pick.dropped) == ["Rice", "Colwill"]

    def test_bench_keeper_is_worth_the_difference_over_the_one_who_played(self):
        squad = _squad(
            [("GK", 2, "Starter")] + _flat_xi(2)[1:],
            [("GK", 8, "Spare"), ("DEF", 0), ("MID", 0), ("FWD", 0)],
        )
        pick = best_xi_selection(squad, captain_played=True)
        assert pick.gain == 6
        assert _names(pick.benched) == ["Spare"]
        assert _names(pick.dropped) == ["Starter"]

    def test_bench_keeper_behind_the_one_who_played_is_worth_nothing(self):
        squad = _squad([("GK", 6)] + _flat_xi(2)[1:], [("GK", 3), ("DEF", 0), ("MID", 0), ("FWD", 0)])
        pick = best_xi_selection(squad, captain_played=True)
        assert (pick.gain, pick.benched, pick.dropped) == (0, (), ())

    def test_outfielder_no_formation_has_room_for_is_worth_nothing(self):
        # Five better defenders already: the bench defender outscored every
        # midfielder and the forward, but no legal shape lets a sixth defender
        # take one of their places.
        squad = _squad(
            [("GK", 2)] + [("DEF", 6)] * 5 + [("MID", 1)] * 4 + [("FWD", 1)],
            [("GK", 0), ("DEF", 4), ("MID", 0), ("FWD", 0)],
        )
        assert best_xi_selection(squad, captain_played=True).gain == 0

    def test_formation_change_brings_two_positions_in(self):
        squad = _squad(
            [("GK", 2)] + [("DEF", 4)] * 3 + [("MID", 4)] * 4
            + [("FWD", 1, "F1"), ("FWD", 1, "F2"), ("FWD", 1, "F3")],
            [("GK", 0), ("DEF", 6, "SpareDef"), ("MID", 5, "SpareMid"), ("FWD", 0)],
        )
        pick = best_xi_selection(squad, captain_played=True)
        # 3-4-3 to 4-5-1: two forwards out, a defender and a midfielder in.
        assert pick.gain == 9
        assert _names(pick.benched) == ["SpareDef", "SpareMid"]
        assert len(pick.dropped) == 2 and set(_names(pick.dropped)) < {"F1", "F2", "F3"}

    # One squad, three armband situations. Unheld, the best XI swaps the
    # 2-point captain for the 5-point bench midfielder (+3); held, the only
    # improvement left is a defender making way for him in a 3-5-2 (+1).
    # A 3-4-3 bringing the 4-point bench forward in for a 4-point defender
    # matches the unheld total, and must not be the shape named.
    def _armband_squad(self) -> list[RecapManagerPlayer]:
        return _squad(
            [("GK", 5)] + [("DEF", 4)] * 4
            + [("MID", 2, "Skipper"), ("MID", 6, "Deputy"), ("MID", 6), ("MID", 6), ("FWD", 5), ("FWD", 5)],
            [("GK", 1), ("DEF", 3), ("MID", 5, "SpareMid"), ("FWD", 4)],
            captain="Skipper", vice="Deputy",
        )

    def test_the_captain_who_played_is_held_in_the_xi(self):
        pick = best_xi_selection(self._armband_squad(), captain_played=True)
        assert pick.gain == 1
        assert _names(pick.benched) == ["SpareMid"]
        assert [(p["position"], p["points"]) for p in pick.dropped] == [("DEF", 4)]

    def test_the_vice_is_held_instead_when_the_captain_did_not_play(self):
        pick = best_xi_selection(self._armband_squad(), captain_played=False)
        assert pick.gain == 3
        assert _names(pick.benched) == ["SpareMid"]
        assert _names(pick.dropped) == ["Skipper"]

    def test_draft_holds_nobody(self):
        squad = self._armband_squad()
        for p in squad:
            p["is_captain"] = p["is_vice_captain"] = False
        assert best_xi_selection(squad, captain_played=False).gain == 3

    def test_a_vice_who_did_not_count_holds_nothing(self):
        # The captain blanked and the vice was auto-subbed out: nobody was
        # doubled, so forcing the vice's zero into the XI would understate.
        squad = _squad(
            [("GK", 2)] + [("DEF", 2)] * 4 + [("MID", 2)] * 3 + [("MID", 1, "Weak"), ("FWD", 2), ("FWD", 2)],
            [("GK", 0), ("MID", 0, "Deputy"), ("MID", 5, "SpareMid"), ("FWD", 0)],
            vice="Deputy",
        )
        pick = best_xi_selection(squad, captain_played=False)
        assert pick.gain == 4
        assert _names(pick.benched) == ["SpareMid"]
        assert _names(pick.dropped) == ["Weak"]

    def test_a_bench_player_level_with_a_starter_is_not_a_better_pick(self):
        squad = _squad(_flat_xi(2), [("GK", 2), ("DEF", 2), ("MID", 2), ("FWD", 2)])
        pick = best_xi_selection(squad, captain_played=True)
        assert (pick.gain, pick.benched, pick.dropped) == (0, (), ())

    def test_bench_boost_has_nothing_left_on_the_bench(self):
        squad = _squad(_flat_xi(2) + [("GK", 9), ("DEF", 9), ("MID", 9), ("FWD", 9)])
        assert best_xi_selection(squad, captain_played=True).gain == 0

    def test_five_two_three_is_a_legal_shape(self):
        """#352 review: 5-2-3 satisfies the formation limits but was missing
        from `VALID_FORMATIONS`, so a squad whose best XI needs two
        midfielders settled for 5-3-2 and understated the gain."""
        squad = _squad(
            [("GK", 2)] + [("DEF", 4)] * 3
            + [("MID", 6), ("MID", 2), ("MID", 1, "M3"), ("MID", 0, "M4")]
            + [("FWD", 8), ("FWD", 8), ("FWD", 8, "F3")],
            [("GK", 0), ("DEF", 14, "D4"), ("DEF", 13, "D5"), ("MID", 0)],
        )
        pick = best_xi_selection(squad, captain_played=False)
        # 5-2-3 brings both defenders in for the two weakest midfielders;
        # 5-3-2 would have had to drop an 8-point forward for one of them.
        assert pick.gain == 26
        assert _names(pick.benched) == ["D4", "D5"]
        assert _names(pick.dropped) == ["M3", "M4"]

    def test_a_starter_auto_subbed_out_is_never_named_as_benched(self):
        """#352 review: a starter auto-subbed out has no minutes and a zero,
        which beats a team-mate on a red card -- and he started, so he must
        not be printed as benched."""
        squad = _squad(
            [("GK", 2), ("DEF", 7), ("DEF", 6), ("DEF", -2, "RedCard"), ("DEF", -1, "CameOn")]
            + [("MID", 4)] * 4 + [("FWD", 3)] * 2,
            [("GK", 0), ("DEF", 0, "SubbedOut"), ("MID", -3), ("FWD", -3)],
        )
        subbed_out = next(p for p in squad if p["name"] == "SubbedOut")
        subbed_out["auto_sub_out"] = True
        pick = best_xi_selection(squad, captain_played=True)
        assert (pick.gain, pick.benched, pick.dropped) == (0, (), ())

    def test_an_unmatched_draft_player_has_no_score_to_reselect_on(self):
        """#352 review: a draft player the main game could not be matched
        to records a false zero, so a bench player on 5 would "beat" a
        starter who may really have scored 9."""
        squad = _squad(
            _flat_xi(2)[:-1] + [("FWD", 0, "Unknown")],
            [("GK", 0), ("DEF", 0), ("MID", 0), ("FWD", 5)],
        )
        next(p for p in squad if p["name"] == "Unknown")["unmatched"] = True
        pick = best_xi_selection(squad, captain_played=False)
        assert (pick.gain, pick.benched, pick.dropped) == (0, (), ())

    def test_a_squad_that_cannot_field_a_legal_xi_has_no_gain(self):
        # A pick the bootstrap could not resolve leaves the recorded squad
        # short; here there is no keeper at all.
        squad = _squad(_flat_xi(2)[1:], [("DEF", 9), ("MID", 9), ("FWD", 9)])
        pick = best_xi_selection(squad, captain_played=True)
        assert (pick.gain, pick.benched, pick.dropped) == (0, (), ())
        assert best_xi_selection([], captain_played=True).gain == 0


class TestCollectorBestXIGain:
    """Both collectors stamp the gain beside the raw bench sum, which stays
    what the API calls `points_on_bench` for the ledger's sake."""

    # (position, points), picks order: a 4-4-2 with the captain up front and
    # the #350 bench behind it -- 31 raw, 23 gained.
    _SLOTS = [
        (PlayerPosition.GOALKEEPER, 5),
        (PlayerPosition.DEFENDER, 4), (PlayerPosition.DEFENDER, 4),
        (PlayerPosition.DEFENDER, 4), (PlayerPosition.DEFENDER, 2),
        (PlayerPosition.MIDFIELDER, 6), (PlayerPosition.MIDFIELDER, 6),
        (PlayerPosition.MIDFIELDER, 6), (PlayerPosition.MIDFIELDER, 3),
        (PlayerPosition.FORWARD, 8), (PlayerPosition.FORWARD, 7),
        (PlayerPosition.GOALKEEPER, 3), (PlayerPosition.MIDFIELDER, 17),
        (PlayerPosition.DEFENDER, 11), (PlayerPosition.FORWARD, 0),
    ]
    _CAPTAIN_SLOT = 10

    async def test_classic_collector_stamps_the_gain(self):
        player_map = {
            i: make_player(id=i, code=100 + i, web_name=f"P{i}", team_id=1, position=position)
            for i, (position, _) in enumerate(self._SLOTS, start=1)
        }
        live_stats = {
            i: {"total_points": points, "minutes": 90 if points else 0}
            for i, (_, points) in enumerate(self._SLOTS, start=1)
        }
        picks = _picks_response(points=63, total_points=63)
        picks["picks"] = [
            {
                "element": i, "position": i,
                "multiplier": (2 if i == self._CAPTAIN_SLOT else 1) if i <= 11 else 0,
                "is_captain": i == self._CAPTAIN_SLOT, "is_vice_captain": i == 11,
            }
            for i in range(1, 16)
        ]
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 63, "total": 63}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=4,
            live_stats=live_stats, player_map=player_map, teams={}, is_live_gw=False,
        )
        m = data["managers"][0]
        assert m["bench_points"] == 31
        assert m["best_xi_gain"] == 23

    async def test_draft_collector_stamps_the_gain(self):
        draft_players = [
            make_draft_player(id=900 + i, code=100 + i, web_name=f"P{i}", team=1, element_type=position.value)
            for i, (position, _) in enumerate(self._SLOTS, start=1)
        ]
        main_players = [
            make_player(id=i, code=100 + i, web_name=f"P{i}", team_id=1, position=position)
            for i, (position, _) in enumerate(self._SLOTS, start=1)
        ]
        live_stats = {i: {"total_points": points} for i, (_, points) in enumerate(self._SLOTS, start=1)}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value={
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 55, "total": 55}],
            "league_entries": [{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        })
        client.get_bootstrap_static = AsyncMock(return_value={"elements": draft_players})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(return_value={
            "picks": [{"element": 900 + i, "position": i} for i in range(1, 16)],
            "subs": [],
        })
        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=4, live_stats=live_stats,
                players=main_players, teams={}, is_live_gw=False,
            )
        m = data["managers"][0]
        assert m["bench_points"] == 31
        assert m["best_xi_gain"] == 23


# ---------------------------------------------------------------------------
# Unit 1: chip-aware classic pick flags
# ---------------------------------------------------------------------------


class TestClassicPickFlags:
    """Cover _classic_pick_flags: (is_bench, is_bench_boost_player, contributed)."""

    def test_non_bb_starter_contributes(self):
        pick = {"position": 5, "multiplier": 1}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip=None, player_id=1, auto_sub_in_ids=set(),
        )
        assert (is_bench, is_bbp, contributed) == (False, False, True)

    def test_non_bb_bench_does_not_contribute(self):
        pick = {"position": 13, "multiplier": 0}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip=None, player_id=1, auto_sub_in_ids=set(),
        )
        assert (is_bench, is_bbp, contributed) == (True, False, False)

    def test_bb_bench_contributes_and_flagged(self):
        pick = {"position": 13, "multiplier": 0}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip="bboost", player_id=1, auto_sub_in_ids=set(),
        )
        assert (is_bench, is_bbp, contributed) == (True, True, True)

    def test_bb_starter_not_flagged_as_bench_boost(self):
        pick = {"position": 1, "multiplier": 1}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip="bboost", player_id=1, auto_sub_in_ids=set(),
        )
        assert (is_bench, is_bbp, contributed) == (False, False, True)

    def test_auto_sub_in_contributes_without_bb(self):
        pick = {"position": 13, "multiplier": 0}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip=None, player_id=99, auto_sub_in_ids={99},
        )
        # Auto-sub target still counts as bench slot by position, but contributes
        assert (is_bench, is_bbp, contributed) == (True, False, True)

    def test_bb_and_auto_sub_in_both_contribute(self):
        pick = {"position": 13, "multiplier": 0}
        is_bench, is_bbp, contributed = _classic_pick_flags(
            pick=pick, active_chip="bboost", player_id=99, auto_sub_in_ids={99},
        )
        assert (is_bench, is_bbp, contributed) == (True, True, True)

    def test_other_chips_do_not_flip_bench_contributed(self):
        # Only bboost flips bench contributed; wildcard, freehit, 3xc do not.
        pick = {"position": 13, "multiplier": 0}
        for chip in ("wildcard", "freehit", "3xc"):
            _, is_bbp, contributed = _classic_pick_flags(
                pick=pick, active_chip=chip, player_id=1, auto_sub_in_ids=set(),
            )
            assert is_bbp is False
            assert contributed is False


# ---------------------------------------------------------------------------
# U4: ledger row model and collector contract
# ---------------------------------------------------------------------------


class TestCollectorLedgerContract:
    """The collector must carry everything capture needs to build a row."""

    async def test_cohort_holds_every_standings_row_including_failed_fetches(self):
        class _RaisingClient(_FakeClassicClient):
            async def get_manager_picks(self, entry_id, gameweek):
                if entry_id == 2:
                    raise RuntimeError("boom")
                return await super().get_manager_picks(entry_id, gameweek)

        standings = [
            {"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500},
            {"entry": 2, "player_name": "Bob", "event_total": 40, "total": 480},
            {"entry": 3, "player_name": "Cara", "event_total": 30, "total": 460},
        ]
        client = _RaisingClient(
            _standings_response(standings),
            {
                1: _picks_response(points=50, total_points=500),
                3: _picks_response(points=30, total_points=460),
            },
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 77}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        cohort = data["standings_cohort"]
        assert [c["manager_key"] for c in cohort] == [1, 2, 3]
        assert [c["manager_name"] for c in cohort] == ["Alice", "Bob", "Cara"]
        assert [c["gw_points"] for c in cohort] == [50, 40, 30]
        assert [c["total_points"] for c in cohort] == [500, 480, 460]
        assert data["league_id"] == 77
        assert data["standings_truncated"] is False

    async def test_classic_truncated_standings_are_flagged(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500}]
        response = _standings_response(standings)
        response["standings"]["has_next"] = True
        client = _FakeClassicClient(response, {1: _picks_response(points=50, total_points=500)})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        assert data["standings_truncated"] is True

    async def test_classic_entry_carries_the_four_rollover_only_fields(self):
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 50, "total": 500}]
        picks = _picks_response(points=50, total_points=500)
        picks["entry_history"].update(
            {"value": 1013, "bank": 7, "overall_rank": 412_345, "event_transfers": 2},
        )
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={}, teams={}, is_live_gw=True,
        )
        m = data["managers"][0]
        # The API's `value` verbatim, bank included -- hence `team_value`.
        assert m["team_value"] == 1013
        assert m["bank"] == 7
        assert m["global_rank"] == 412_345
        assert m["transfers_made"] == 2
        # Global rank and league position are separate fields on the entry.
        assert m["overall_rank"] == 1

    async def test_classic_squad_player_carries_the_stable_code(self):
        player = make_player(id=5, code=99_001, web_name="Star", team_id=1)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [{"element": 5, "position": 1, "multiplier": 1, "is_captain": True}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={5: {"total_points": 6, "minutes": 90}},
            player_map={5: player}, teams={}, is_live_gw=False,
        )
        squad = data["managers"][0]["squad"]
        assert [p["code"] for p in squad] == [99_001]
        assert squad[0]["had_fixture"] is True

    async def test_blank_gameweek_marks_only_the_clubs_without_a_fixture(self):
        playing = make_player(id=5, code=1, web_name="Plays", team_id=1)
        blanking = make_player(id=6, code=2, web_name="Blanks", team_id=2)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [
            {"element": 5, "position": 1, "multiplier": 1},
            {"element": 6, "position": 2, "multiplier": 1},
        ]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: playing, 6: blanking}, teams={},
            is_live_gw=False, bgw_team_ids=frozenset({2}),
        )
        by_name = {p["name"]: p for p in data["managers"][0]["squad"]}
        assert by_name["Plays"]["had_fixture"] is True
        assert by_name["Blanks"]["had_fixture"] is False

    async def test_normal_gameweek_marks_no_player_as_fixtureless(self):
        player = make_player(id=5, code=1, web_name="Plays", team_id=1)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [{"element": 5, "position": 1, "multiplier": 1}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: player}, teams={}, is_live_gw=False,
        )
        assert all(p["had_fixture"] for p in data["managers"][0]["squad"])

    async def test_a_replay_reads_the_blank_off_the_gameweek_not_the_current_club(self):
        """issue #169: `had_fixture` was `player.team_id not in bgw_team_ids`, so
        a replay asked today's bootstrap which club the player is at. A player
        who has since moved to a club that blanked that gameweek had his own
        real fixture erased -- and `LedgerCaptaincy.had_fixture` gates the
        captain-blank condition, so the streak silently moved with him."""
        # Today he is at club 2, which blanked GW10. In GW10 he was at club 1
        # and played, which is what the gameweek's own live data records.
        mover = make_player(id=5, code=1, web_name="Mover", team_id=2)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [{"element": 5, "position": 1, "multiplier": 1}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})

        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: mover}, teams={}, is_live_gw=False,
            bgw_team_ids=frozenset({2}), players_with_fixture=frozenset({5}),
        )
        assert data["managers"][0]["squad"][0]["had_fixture"] is True

    async def test_a_replay_does_not_invent_a_fixture_for_a_player_who_had_none(self):
        """The other direction, and the more damaging one: he has since moved to
        a club that played, so the club derivation records a blank he could
        never have had and the captain-blank run counts it."""
        mover = make_player(id=5, code=1, web_name="Mover", team_id=1)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [{"element": 5, "position": 1, "multiplier": 1}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})

        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: mover}, teams={}, is_live_gw=False,
            bgw_team_ids=frozenset(), players_with_fixture=frozenset(),
        )
        assert data["managers"][0]["squad"][0]["had_fixture"] is False

    async def test_without_the_gameweek_signal_the_club_still_answers(self):
        """`players_with_fixture=None` is the gameweek declining to answer, and
        must leave the existing club-based derivation exactly as it was."""
        blanking = make_player(id=6, code=2, web_name="Blanks", team_id=2)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [{"element": 6, "position": 1, "multiplier": 1}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})

        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={6: blanking}, teams={}, is_live_gw=False,
            bgw_team_ids=frozenset({2}), players_with_fixture=None,
        )
        assert data["managers"][0]["squad"][0]["had_fixture"] is False

    async def test_classic_transfers_carry_both_player_codes(self):
        pin = make_player(id=5, code=111, web_name="In", team_id=1)
        pout = make_player(id=6, code=222, web_name="Out", team_id=1)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["entry_history"]["event_transfers"] = 1
        client = _FakeClassicClient(
            _standings_response(standings),
            {1: picks},
            transfers_by_entry={1: [{"event": 10, "element_in": 5, "element_out": 6}]},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: pin, 6: pout}, teams={}, is_live_gw=False,
        )
        tr = data["managers"][0]["transfers"][0]
        assert tr["player_in_code"] == 111
        assert tr["player_out_code"] == 222

    def test_fines_are_keyed_by_manager_not_by_display_name(self):
        """Two managers can share a display name, so a stored ruling has to
        carry the key the row is written under."""
        rules = [{"type": "last-place", "penalty": "Pint on video"}]
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        managers = [
            _make_manager(name="Same Name", entry_id=1, gw_points=80, squad=squad),
            _make_manager(name="Same Name", entry_id=2, gw_points=10, squad=squad),
        ]
        fines = evaluate_league_fines(managers, {"fines": {"classic": rules}}, "classic").fines
        assert [f["manager_key"] for f in fines] == [2]

    def test_draft_fines_key_on_the_league_local_id(self):
        rules = [{"type": "last-place", "penalty": "Pint on video"}]
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        top = _make_manager(name="Alice", entry_id=0, gw_points=80, squad=squad)
        bottom = _make_manager(name="Bob", entry_id=0, gw_points=10, squad=squad)
        top["league_entry_id"] = 10
        bottom["league_entry_id"] = 11
        fines = evaluate_league_fines([top, bottom], {"fines": {"draft": rules}}, "draft").fines
        assert [f["manager_key"] for f in fines] == [11]

    async def test_draft_keys_two_unclaimed_teams_distinctly(self):
        draft_player = make_draft_player(id=900, code=555, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, code=555, web_name="Star", team_id=1)
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [
                {"league_entry": 10, "event_total": 5, "total": 5},
                {"league_entry": 11, "event_total": 5, "total": 5},
            ],
            "league_entries": [
                {"id": 10, "entry_id": None, "entry_name": "Ghost A"},
                {"id": 11, "entry_id": None, "entry_name": "Ghost B"},
            ],
        }
        picks = {None: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(return_value={"elements": [draft_player]})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks[entry_id])

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 3}}, gw=15, live_stats={5: {"total_points": 5}},
                players=[main_player], teams={}, is_live_gw=False,
            )
        assert sorted(m["league_entry_id"] for m in data["managers"]) == [10, 11]
        assert sorted(c["manager_key"] for c in data["standings_cohort"]) == [10, 11]
        assert all(c["entry_id"] is None for c in data["standings_cohort"])
        assert data["league_id"] == 3

    async def test_draft_unmatched_player_carries_no_code(self):
        matched = make_draft_player(id=900, code=555, web_name="Star", team=1, element_type=3)
        stranger = make_draft_player(id=901, web_name="Nobody", team=1, element_type=3)
        main_player = make_player(id=5, code=555, web_name="Star", team_id=1)
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 5, "total": 5}],
            "league_entries": [{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        }
        picks = {
            1: {
                "picks": [
                    {"element": 900, "position": 1},
                    {"element": 901, "position": 2},
                ],
                "subs": [],
            },
        }
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(return_value={"elements": [matched, stranger]})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks[entry_id])

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 5}},
                players=[main_player], teams={}, is_live_gw=False,
            )
        by_name = {p["name"]: p for p in data["managers"][0]["squad"]}
        assert by_name["Star"]["code"] == 555
        assert by_name["Star"]["unmatched"] is False
        assert by_name["Nobody"]["code"] is None
        assert by_name["Nobody"]["unmatched"] is True

    async def test_draft_player_renamed_in_the_main_game_still_matches(self):
        """#168: the main game renamed Savinho to Savio and the draft game
        kept the old spelling, so a (web_name, team) join dropped him to
        unmatched with a null code and a fabricated zero. Both bootstraps
        carry the same code, which is what the join uses."""
        renamed = make_draft_player(id=403, code=510281, web_name="Savinho", team=19, element_type=3)
        main_player = make_player(id=403, code=510281, web_name="Sávio", team_id=19)
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 9, "total": 9}],
            "league_entries": [{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        }
        picks = {1: {"picks": [{"element": 403, "position": 1}], "subs": []}}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(return_value={"elements": [renamed]})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks[entry_id])

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=1, live_stats={403: {"total_points": 9}},
                players=[main_player], teams={}, is_live_gw=False,
            )
        squad_player = data["managers"][0]["squad"][0]
        assert squad_player["unmatched"] is False
        assert squad_player["code"] == 510281
        assert squad_player["points"] == 9


class TestCollectorGameweekClubs:
    """#177: a first capture and a coarse-to-detailed upgrade have no recorded
    row to carry a club forward from, so the gameweek has to answer instead."""

    _TEAMS = {t.id: t for t in (
        make_team(id=1, name="Alpha", short_name="ALP"),
        make_team(id=2, name="Beta", short_name="BET"),
    )}

    async def test_classic_squad_records_the_club_the_gameweek_places_him_at(self):
        moved = make_player(id=5, code=99_001, web_name="Star", team_id=2)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [{"element": 5, "position": 1, "multiplier": 1}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: moved}, teams=self._TEAMS,
            is_live_gw=False, gameweek_clubs={5: 1},
        )
        player = data["managers"][0]["squad"][0]
        assert player["team"] == "ALP"
        assert player["team_name"] == "Alpha"

    async def test_classic_squad_keeps_todays_club_where_the_gameweek_is_silent(self):
        player = make_player(id=5, code=99_001, web_name="Star", team_id=2)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["picks"] = [{"element": 5, "position": 1, "multiplier": 1}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: player}, teams=self._TEAMS,
            is_live_gw=False, gameweek_clubs=None,
        )
        assert data["managers"][0]["squad"][0]["team"] == "BET"

    async def test_classic_transfers_are_stamped_from_the_gameweek_too(self):
        """The player moved *out* never appears in the squad, so his club has
        nowhere else to come from."""
        incoming = make_player(id=5, code=99_001, web_name="In", team_id=2)
        outgoing = make_player(id=6, code=99_002, web_name="Out", team_id=2)
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 6, "total": 6}]
        picks = _picks_response(points=6, total_points=6)
        picks["entry_history"]["event_transfers"] = 1
        client = _FakeClassicClient(
            _standings_response(standings), {1: picks},
            transfers_by_entry={1: [{"event": 10, "element_in": 5, "element_out": 6}]},
        )
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=10,
            live_stats={}, player_map={5: incoming, 6: outgoing}, teams=self._TEAMS,
            is_live_gw=False, gameweek_clubs={5: 1, 6: 1},
        )
        transfer = data["managers"][0]["transfers"][0]
        assert transfer["player_in_team"] == "ALP"
        assert transfer["player_out_team"] == "ALP"
        assert transfer["player_in_team_name"] == "Alpha"

    async def _draft_data(self, **kwargs):
        # Draft club 1 is main-game club 2, BET (#423)
        draft_player = make_draft_player(id=900, code=555, web_name="Star", team=1, element_type=3)
        main_player = make_player(id=5, code=555, web_name="Star", team_id=2)
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 5, "total": 5}],
            "league_entries": [
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
            ],
        }
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(
            return_value={"elements": [draft_player], "teams": _SWAPPED_DRAFT_TEAMS},
        )
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks[entry_id])
        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            return await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15,
                live_stats={5: {"total_points": 5}}, players=[main_player],
                teams=self._TEAMS, is_live_gw=False, **kwargs,
            )

    async def test_draft_squad_records_the_club_the_gameweek_places_him_at(self):
        data = await self._draft_data(gameweek_clubs={5: 1})
        assert data["managers"][0]["squad"][0]["team"] == "ALP"

    async def test_draft_squad_keeps_todays_club_where_the_gameweek_is_silent(self):
        data = await self._draft_data(gameweek_clubs=None)
        assert data["managers"][0]["squad"][0]["team"] == "BET"

    async def test_draft_waiver_moves_are_stamped_from_the_gameweek_too(self):
        dp_in = make_draft_player(id=900, code=555, web_name="In", team=2, element_type=3)
        dp_out = make_draft_player(id=901, code=666, web_name="Out", team=2, element_type=3)
        main_in = make_player(id=5, code=555, web_name="In", team_id=2)
        main_out = make_player(id=6, code=666, web_name="Out", team_id=2)
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 5, "total": 5}],
            "league_entries": [
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
            ],
        }
        txns = {"transactions": [{
            "event": 15, "result": "a", "entry": 1,
            "element_in": 900, "element_out": 901, "kind": "w",
        }]}
        picks = {1: {"picks": [{"element": 900, "position": 1}], "subs": []}}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(return_value={"elements": [dp_in, dp_out]})
        client.get_league_transactions = AsyncMock(return_value=txns)
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks[entry_id])

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15,
                live_stats={5: {"total_points": 5}, 6: {"total_points": 2}},
                players=[main_in, main_out], teams=self._TEAMS, is_live_gw=False,
                gameweek_clubs={5: 1, 6: 1},
            )
        txn = data["managers"][0]["transactions"][0]
        assert txn["player_in_team"] == "ALP"
        assert txn["player_out_team"] == "ALP"

    async def test_draft_txn_kind_is_stored_verbatim(self):
        # #146: the free-agent kind "f" must survive collection unchanged,
        # not be collapsed into a waiver.
        data = await self._draft_data_with_txn_kind("f")
        assert data["managers"][0]["transactions"][0]["kind"] == "f"

    async def test_draft_txn_missing_kind_is_not_defaulted_to_waiver(self):
        # #146: a transaction with no `kind` at all must not silently become
        # "w" -- that would misclassify an unknown move as a waiver in the
        # award breakdown. It is stored as the empty string instead.
        data = await self._draft_data_with_txn_kind(None)
        assert data["managers"][0]["transactions"][0]["kind"] == ""

    async def _draft_data_with_txn_kind(self, kind: str | None):
        dp_in = make_draft_player(id=900, code=555, web_name="In", team=2, element_type=3)
        dp_out = make_draft_player(id=901, code=666, web_name="Out", team=2, element_type=3)
        main_in = make_player(id=5, code=555, web_name="In", team_id=2)
        main_out = make_player(id=6, code=666, web_name="Out", team_id=2)
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 5, "total": 5}],
            "league_entries": [
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
            ],
        }
        txn: dict[str, Any] = {
            "event": 15, "result": "a", "entry": 1,
            "element_in": 900, "element_out": 901,
        }
        if kind is not None:
            txn["kind"] = kind
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(return_value={"elements": [dp_in, dp_out]})
        client.get_league_transactions = AsyncMock(return_value={"transactions": [txn]})
        client.get_entry_picks = AsyncMock(
            side_effect=lambda entry_id, gw: {"picks": [{"element": 900, "position": 1}], "subs": []}
        )
        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            return await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15,
                live_stats={5: {"total_points": 5}, 6: {"total_points": 2}},
                players=[main_in, main_out], teams=self._TEAMS, is_live_gw=False,
                gameweek_clubs={5: 1, 6: 1},
            )

    async def test_a_matched_pick_keeps_his_club_when_the_draft_club_is_unlisted(self, caplog):
        """#423 review: at a rollover the draft list can carry a club whose
        code the main game no longer lists. The matched main-game player's
        club still answers, so the ledger row is not written club-less, and
        the unlisted club is logged where the ledger is written."""
        draft_player = make_draft_player(id=900, code=555, web_name="Star", team=30, element_type=3)
        main_player = make_player(id=5, code=555, web_name="Star", team_id=2)
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 5, "total": 5}],
            "league_entries": [
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
            ],
        }
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(return_value={
            "elements": [draft_player],
            "teams": [*_SWAPPED_DRAFT_TEAMS, make_draft_team(id=30, code=999, short_name="NOW")],
        })
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(
            return_value={"picks": [{"element": 900, "position": 1}], "subs": []},
        )

        with caplog.at_level(logging.WARNING), \
                patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={5: {"total_points": 5}},
                players=[main_player], teams=self._TEAMS, is_live_gw=False,
                gameweek_clubs=None,
            )
        squad_player = data["managers"][0]["squad"][0]
        assert (squad_player["team"], squad_player["team_name"]) == ("BET", "Beta")
        assert "Draft clubs with no main-game club of the same code (NOW)" in caplog.text

    async def test_an_unmatched_draft_player_keeps_the_draft_bootstraps_club(self):
        """No main-game id, so nothing to look the gameweek's answer up by.

        His draft club 1 is main-game club 2, BET (#423)."""
        stranger = make_draft_player(id=901, code=777, web_name="Nobody", team=1, element_type=3)
        main_player = make_player(id=5, code=555, web_name="Star", team_id=2)
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 0, "total": 0}],
            "league_entries": [
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
            ],
        }
        picks = {1: {"picks": [{"element": 901, "position": 1}], "subs": []}}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(
            return_value={"elements": [stranger], "teams": _SWAPPED_DRAFT_TEAMS},
        )
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks[entry_id])

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            data = await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=15, live_stats={},
                players=[main_player], teams=self._TEAMS, is_live_gw=False,
                gameweek_clubs={5: 1},
            )
        squad_player = data["managers"][0]["squad"][0]
        assert squad_player["unmatched"] is True
        assert squad_player["team"] == "BET"


class TestDraftLostClaims:
    """Issue #329: 42% of the feed's transaction rows were discarded before
    anything downstream could see them, so a manager who was outbid was
    indistinguishable from one who never tried."""

    _TEAMS = {
        1: make_team(id=1, name="Alpha", short_name="ALP"),
        2: make_team(id=2, name="Beta", short_name="BET"),
    }

    async def _collect(self, txns, *, rivals=0):
        """One manager (entry 1) by default. `rivals` adds further league
        members, so a test about another manager's winning claim exercises a
        row the bucketing actually keeps -- an entry absent from
        `league_entries` is dropped before any of this runs.
        """
        # Draft clubs swapped against the main game's (#423): Elanga's draft
        # club 2 is main-game club 1, ALP, and Savio's 1 is main-game 2, BET
        elanga = make_draft_player(id=900, code=555, web_name="Elanga", team=2, element_type=3)
        savio = make_draft_player(id=901, code=666, web_name="Savio", team=1, element_type=3)
        keeper = make_draft_player(id=902, code=777, web_name="Keeper", team=2, element_type=1)
        main_elanga = make_player(id=5, code=555, web_name="Elanga", team_id=1)
        main_savio = make_player(id=6, code=666, web_name="Savio", team_id=2)
        main_keeper = make_player(id=7, code=777, web_name="Keeper", team_id=1)
        entries = [{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}]
        entries += [
            {"id": 10 + n, "entry_id": 1 + n,
             "player_first_name": f"Rival{n}", "player_last_name": "R"}
            for n in range(1, rivals + 1)
        ]
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [
                {"league_entry": e["id"], "event_total": 0, "total": 0} for e in entries
            ],
            "league_entries": entries,
        }
        picks = {
            e["entry_id"]: {"picks": [{"element": 902, "position": 1}], "subs": []}
            for e in entries
        }
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(
            return_value={"elements": [elanga, savio, keeper], "teams": _SWAPPED_DRAFT_TEAMS}
        )
        client.get_league_transactions = AsyncMock(return_value={"transactions": txns})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks[entry_id])

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            return await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=3,
                live_stats={5: {"total_points": 9}, 6: {"total_points": 1}, 7: {"total_points": 0}},
                players=[main_elanga, main_savio, main_keeper],
                teams=self._TEAMS, is_live_gw=False,
            )

    async def test_a_lost_claim_is_carried_through_ingestion(self):
        """The reported case: a priority-1 claim for Elanga that a rival won.
        The manager's whole gameweek, and previously invisible."""
        data = await self._collect([{
            "event": 3, "result": "di", "entry": 1, "kind": "w", "priority": 1,
            "element_in": 900, "element_out": 901,
        }])
        manager = data["managers"][0]
        assert manager["transactions"] == []
        assert manager["lost_claims"] == [{
            "player_in": "Elanga", "player_in_team": "ALP", "player_in_team_name": "Alpha",
            "player_in_code": 555,
            "player_out": "Savio", "player_out_team": "BET", "player_out_team_name": "Beta",
            "player_out_code": 666,
            "kind": "w", "priority": 1,
        }]

    async def test_a_do_row_is_not_recorded_as_an_attempt(self):
        """A `do` row is the cascade behind a claim that succeeded -- the
        manager's own earlier accepted claim already used that drop. Counting
        it would report the winner of a race as having lost one."""
        data = await self._collect([
            {
                "event": 3, "result": "a", "entry": 1, "kind": "w", "priority": 1,
                "element_in": 900, "element_out": 901,
            },
            {
                "event": 3, "result": "do", "entry": 1, "kind": "w", "priority": 2,
                "element_in": 902, "element_out": 901,
            },
        ])
        manager = data["managers"][0]
        assert "lost_claims" not in manager
        assert [t["player_in"] for t in manager["transactions"]] == ["Elanga"]

    async def test_the_accepted_move_list_is_unchanged_by_the_widened_ingestion(self):
        """The ledger, the awards and every net figure read `transactions`.
        Widening what is fetched must leave that list exactly as it was."""
        data = await self._collect([
            {
                "event": 3, "result": "a", "entry": 1, "kind": "w", "priority": 2,
                "element_in": 900, "element_out": 901,
            },
            {
                "event": 3, "result": "di", "entry": 1, "kind": "w", "priority": 1,
                "element_in": 902, "element_out": 901,
            },
        ])
        manager = data["managers"][0]
        assert len(manager["transactions"]) == 1
        txn = manager["transactions"][0]
        assert (txn["player_in"], txn["player_out"], txn["net"]) == ("Elanga", "Savio", 8)
        assert [c["player_in"] for c in manager["lost_claims"]] == ["Keeper"]

    async def test_a_lost_claim_from_another_gameweek_is_not_picked_up(self):
        data = await self._collect([{
            "event": 2, "result": "di", "entry": 1, "kind": "w", "priority": 1,
            "element_in": 900, "element_out": 901,
        }])
        assert "lost_claims" not in data["managers"][0]

    async def test_a_claim_denied_on_a_player_the_manager_won_is_not_a_loss(self):
        """Issue #342: he took Elanga at priority 1, listed him again at
        priority 3 against a different drop, and the second was denied
        because he had just taken him himself. The stored claim reported a
        race he won as one he was beaten to."""
        data = await self._collect([
            {
                "event": 3, "result": "a", "entry": 1, "kind": "w", "priority": 1,
                "index": 2, "element_in": 900, "element_out": 901,
            },
            {
                "event": 3, "result": "di", "entry": 1, "kind": "w", "priority": 3,
                "index": 9, "element_in": 900, "element_out": 902,
            },
        ])
        manager = data["managers"][0]
        assert [t["player_in"] for t in manager["transactions"]] == ["Elanga"]
        assert "lost_claims" not in manager

    async def test_signing_the_player_as_a_free_agent_later_keeps_the_loss(self):
        """The rival won the waiver and dropped him; the manager signed him in
        free agency, which runs after the whole waiver batch and so cannot be
        what denied the claim. He lost the waiver and got him another way, and
        the roster has to carry both."""
        data = await self._collect([
            {
                "event": 3, "result": "di", "entry": 1, "kind": "w", "priority": 1,
                "index": 3, "element_in": 900, "element_out": 901,
            },
            {
                "event": 3, "result": "a", "entry": 1, "kind": "f", "priority": None,
                "index": None, "element_in": 900, "element_out": 902,
            },
        ])
        manager = data["managers"][0]
        assert [t["player_in"] for t in manager["transactions"]] == ["Elanga"]
        assert [c["player_in"] for c in manager["lost_claims"]] == ["Elanga"]

    async def test_one_player_claimed_twice_and_lost_is_recorded_once(self):
        """A conditional chain naming one target against two drops. He wanted
        Elanga once, so the roster names him once, at the better priority --
        the rule `contested_draft_claims` already works to."""
        data = await self._collect([
            {
                "event": 3, "result": "di", "entry": 1, "kind": "w", "priority": 2,
                "element_in": 900, "element_out": 901,
            },
            {
                "event": 3, "result": "di", "entry": 1, "kind": "w", "priority": 5,
                "element_in": 900, "element_out": 902,
            },
        ])
        claims = data["managers"][0]["lost_claims"]
        assert [(c["player_in"], c["priority"]) for c in claims] == [("Elanga", 2)]

    async def test_a_rivals_win_still_leaves_the_claim_recorded(self):
        """The self-cascade exclusion is per manager: the same accepted row
        that clears the winner's own claim must leave the loser's standing --
        otherwise the fix trades one misattribution for the reverse."""
        data = await self._collect([
            {
                "event": 3, "result": "a", "entry": 2, "kind": "w", "priority": 1,
                "element_in": 900, "element_out": 902,
            },
            {
                "event": 3, "result": "di", "entry": 1, "kind": "w", "priority": 1,
                "element_in": 900, "element_out": 901,
            },
        ], rivals=1)
        by_name = {m["manager_name"]: m for m in data["managers"]}
        winner, loser = by_name["Rival1 R"], by_name["A B"]
        # The rival's win is visible to the collector, not merely unbucketed.
        assert [t["player_in"] for t in winner["transactions"]] == ["Elanga"]
        assert "lost_claims" not in winner
        assert [c["player_in"] for c in loser["lost_claims"]] == ["Elanga"]

    async def test_a_free_agent_claim_records_no_priority(self):
        """Free agents are first-come-first-served and the feed sends a null
        priority; printing "priority None" would invent a spent claim."""
        data = await self._collect([{
            "event": 3, "result": "di", "entry": 1, "kind": "f", "priority": None,
            "element_in": 900, "element_out": 901,
        }])
        assert data["managers"][0]["lost_claims"][0]["priority"] is None


class TestDraftContestedClaimsCollection:
    """Issue #330: the race reaches the collector's awards from the raw
    feed, with the winner read off the accepted row."""

    _TEAMS = {
        1: make_team(id=1, name="Alpha", short_name="ALP"),
        2: make_team(id=2, name="Beta", short_name="BET"),
    }

    async def _collect(self, txns):
        elanga = make_draft_player(id=900, code=555, web_name="Elanga", team=1, element_type=3)
        savio = make_draft_player(id=901, code=666, web_name="Savio", team=2, element_type=3)
        keeper = make_draft_player(id=902, code=777, web_name="Keeper", team=1, element_type=1)
        main_players = [
            make_player(id=5, code=555, web_name="Elanga", team_id=1),
            make_player(id=6, code=666, web_name="Savio", team_id=2),
            make_player(id=7, code=777, web_name="Keeper", team_id=1),
        ]
        league_details = {
            "league": {"name": "Draft League"},
            "standings": [
                {"league_entry": 10, "event_total": 0, "total": 0},
                {"league_entry": 20, "event_total": 0, "total": 0},
                {"league_entry": 30, "event_total": 0, "total": 0},
            ],
            "league_entries": [
                {"id": 10, "entry_id": 1, "player_first_name": "Alice", "player_last_name": "A"},
                {"id": 20, "entry_id": 2, "player_first_name": "Bob", "player_last_name": "B"},
                {"id": 30, "entry_id": 3, "player_first_name": "Cam", "player_last_name": "C"},
            ],
        }
        picks = {
            1: {"picks": [{"element": 900, "position": 1}], "subs": []},
            2: {"picks": [{"element": 902, "position": 1}], "subs": []},
            3: {"picks": [{"element": 901, "position": 1}], "subs": []},
        }
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value=league_details)
        client.get_bootstrap_static = AsyncMock(return_value={"elements": [elanga, savio, keeper]})
        client.get_league_transactions = AsyncMock(return_value={"transactions": txns})
        client.get_entry_picks = AsyncMock(side_effect=lambda entry_id, gw: picks[entry_id])

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            return await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=3,
                live_stats={5: {"total_points": 9}, 6: {"total_points": 1}, 7: {"total_points": 0}},
                players=main_players, teams=self._TEAMS, is_live_gw=False,
            )

    _ACCEPTED = {
        "event": 3, "result": "a", "entry": 1, "kind": "w", "priority": 2,
        "element_in": 900, "element_out": 901,
    }
    _BOB_LOST = {
        "event": 3, "result": "di", "entry": 2, "kind": "w", "priority": 1,
        "element_in": 900, "element_out": 902,
    }
    _CAM_LOST = {
        "event": 3, "result": "di", "entry": 3, "kind": "w", "priority": 3,
        "element_in": 900, "element_out": 901,
    }

    async def test_a_pile_up_reaches_the_award(self):
        data = await self._collect([self._ACCEPTED, self._BOB_LOST, self._CAM_LOST])
        award = data["awards"]["most_contested"]
        assert (award["manager_name"], award["value"], award["detail"]) == (
            "Alice A", 3,
            "Elanga was claimed by 3 managers: Alice A won him; "
            "Bob B (priority 1) and Cam C (priority 3) were beaten to him.",
        )
        assert "contested_claims_lines" not in data

    async def test_a_two_way_race_is_no_award_but_still_reaches_the_editorial(self):
        data = await self._collect([self._ACCEPTED, self._BOB_LOST])
        assert "most_contested" not in data["awards"]
        assert (
            "- Elanga was claimed by 2 managers: Alice A won him; Bob B (priority 1) was beaten to him."
        ) in format_recap_waivers_context(data)

    async def test_nothing_contested_leaves_no_award(self):
        data = await self._collect([self._ACCEPTED])
        assert "most_contested" not in data["awards"]


class TestRecapPlayerClubs:
    """#150: the recap prompt carried no club data at all, so any club the
    narrative named came from training data a transfer window out of date."""

    @staticmethod
    def _data(**overrides):
        manager = {
            "manager_name": "Manager A",
            "captain": "Gyökeres",
            "captain_points": 9,
            "captain_played": True,
            "gw_points": 70,
            "squad": [
                {"name": "Gyökeres", "team": "ARS", "team_name": "Arsenal"},
                {"name": "Wissa", "team": "NEW", "team_name": "Newcastle"},
            ],
        }
        manager.update(overrides)
        return {"fpl_format": "classic", "managers": [manager]}

    def _roster(self, **overrides):
        return format_recap_player_clubs_context(collect_player_clubs(self._data(**overrides)))

    def test_roster_lists_every_squad_player_with_a_full_club_name(self):
        text = self._roster()
        assert "- Gyökeres: Arsenal" in text
        assert "- Wissa: Newcastle" in text

    def test_roster_covers_transfers_as_well_as_squads(self):
        text = self._roster(transfers=[{
            "player_in": "Semenyo", "player_in_team": "MUN", "player_in_team_name": "Man Utd",
            "player_out": "Watkins", "player_out_team": "AVL", "player_out_team_name": "Aston Villa",
        }])
        assert "- Semenyo: Man Utd" in text
        assert "- Watkins: Aston Villa" in text

    def test_roster_covers_draft_waiver_transactions(self):
        text = self._roster(transactions=[{
            "player_in": "Semenyo", "player_in_team": "MUN", "player_in_team_name": "Man Utd",
            "player_out": "Watkins", "player_out_team": "AVL", "player_out_team_name": "Aston Villa",
        }])
        assert "- Semenyo: Man Utd" in text

    def test_roster_covers_a_lost_claims_players(self):
        """A claim a rival won can name a player no squad and no accepted move
        does, and the prose is forbidden to state a club the roster omits."""
        text = self._roster(lost_claims=[{
            "player_in": "Elanga", "player_in_team": "NEW", "player_in_team_name": "Newcastle",
            "player_out": "Sávio", "player_out_team": "MCI", "player_out_team_name": "Man City",
        }])
        assert "- Elanga: Newcastle" in text
        assert "- Sávio: Man City" in text

    def test_roster_drops_a_name_two_clubs_claim(self):
        """Two players share a web_name most seasons. The recap names players by
        name alone, so neither club can be attributed - better absent than wrong."""
        text = self._roster(squad=[
            {"name": "Martínez", "team": "AVL", "team_name": "Aston Villa"},
            {"name": "Martínez", "team": "MUN", "team_name": "Man Utd"},
            {"name": "Wissa", "team": "NEW", "team_name": "Newcastle"},
        ])
        assert "Martínez" not in text
        assert "- Wissa: Newcastle" in text

    def test_ambiguity_survives_a_later_repeat_of_the_first_club(self):
        """A third sighting matching the first club must not resurrect the name."""
        clubs = collect_player_clubs(self._data(squad=[
            {"name": "Martínez", "team": "AVL", "team_name": "Aston Villa"},
            {"name": "Martínez", "team": "MUN", "team_name": "Man Utd"},
            {"name": "Martínez", "team": "AVL", "team_name": "Aston Villa"},
        ]))
        assert "Martínez" not in clubs

    def test_roster_drops_a_player_whose_club_never_resolved(self):
        """Collection sets team_name to None rather than a placeholder, so the
        player simply has no club to state."""
        assert self._roster(squad=[{"name": "Calvert-Lewin", "team": "???", "team_name": None}]) == ""

    def test_roster_empty_when_no_squads_carry_clubs(self):
        assert format_recap_player_clubs_context({}) == ""

    def test_roster_states_it_is_the_only_source(self):
        assert "only source for a player's club" in self._roster()

    def test_captain_group_header_carries_the_club_inline(self):
        data = self._data()
        text = format_recap_captains_context(data, collect_player_clubs(data))
        assert "- **Gyökeres (Arsenal)** (×1): Manager A (9 pts)" in text

    def test_captain_group_header_unchanged_without_a_roster(self):
        text = format_recap_captains_context(self._data(), None)
        assert "- **Gyökeres** (×1): Manager A (9 pts)" in text

    def test_captain_group_header_omits_an_ambiguous_club(self):
        data = self._data(
            captain="Martínez",
            squad=[
                {"name": "Martínez", "team": "AVL", "team_name": "Aston Villa"},
                {"name": "Martínez", "team": "MUN", "team_name": "Man Utd"},
            ],
        )
        text = format_recap_captains_context(data, collect_player_clubs(data))
        assert "- **Martínez** (×1):" in text
        assert "Aston Villa" not in text


# Every gameweek of the span ruled `last-place`, which is what lets an
# ordinal be asserted at all: a hole anywhere earlier could hide a fine the
# tally never counted.
_ALL_TEN = list(range(1, 11))


class TestFinePlacement:
    """This gameweek's fine, placed in the fined manager's season (issue #233).

    Two of three generated editorials called the week's loser's first
    last-place finish their second, having read a Season Fines section that
    listed a 1 against each of two different managers and a league-wide total
    of 2. The prompt now states the ordinal outright, so the model is handed
    the sentence rather than the sum.
    """

    @staticmethod
    def _tally(*managers: ManagerFineTally, through: int = 10) -> SeasonFinesTally:
        return SeasonFinesTally(
            season="2025-26",
            fpl_format="classic",
            league_id=1,
            through_gameweek=through,
            start_gameweek=1,
            rule_types=["last-place"],
            managers=list(managers),
            qualifiers=["Every gameweek from GW1 through GW10 was ruled."],
        )

    @staticmethod
    def _fine(name: str, key: int | None = 1, rule: str = "last-place") -> RecapFineResult:
        fine = RecapFineResult(
            manager_name=name, rule_type=rule, message=f"{name} finished last",
        )
        if key is not None:
            fine["manager_key"] = key
        return fine

    def test_a_managers_only_fine_is_called_their_first(self):
        """The exact failure: one fine this week, one all season, and the
        editorial reached for "twice in a row" anyway."""
        data = _make_recap_data(fines=[self._fine("Bob")])
        tally = self._tally(
            ManagerFineTally(
                manager_key=1, manager_name="Bob",
                counts={"last-place": 1}, fined_gameweeks=[10],
                ruled_gameweeks_by_rule={"last-place": _ALL_TEN},
            ),
            # The other manager's own single fine, in a different gameweek --
            # the pair of 1s the editorial merged into one running count.
            ManagerFineTally(
                manager_key=2, manager_name="Ada",
                counts={"last-place": 1}, fined_gameweeks=[9],
                ruled_gameweeks_by_rule={"last-place": _ALL_TEN},
            ),
        )

        text = format_recap_fines_context(data, tally)

        assert "this gameweek's last-place fine is Bob's first of the season" in text
        assert "No earlier gameweek carries a last-place fine against Bob" in text
        assert "Ada" not in text, "only managers fined this gameweek are placed"

    def test_a_repeat_offender_is_counted_including_this_one(self):
        data = _make_recap_data(fines=[self._fine("Bob")])
        tally = self._tally(ManagerFineTally(
            manager_key=1, manager_name="Bob",
            counts={"last-place": 3}, fined_gameweeks=[2, 6, 10],
            ruled_gameweeks_by_rule={"last-place": _ALL_TEN},
        ))

        text = format_recap_fines_context(data, tally)

        assert "is Bob's third of the season, this one included" in text

    def test_each_rule_type_is_placed_against_its_own_count(self):
        """A week carrying two fines against one manager places each against
        its own rule's count, never against their combined total."""
        data = _make_recap_data(fines=[
            self._fine("Bob"), self._fine("Bob", rule="red-card"),
        ])
        tally = self._tally(ManagerFineTally(
            manager_key=1, manager_name="Bob",
            counts={"last-place": 2, "red-card": 1}, fined_gameweeks=[4, 10],
            ruled_gameweeks_by_rule={"last-place": _ALL_TEN, "red-card": _ALL_TEN},
        ))

        text = format_recap_fines_context(data, tally)

        assert "this gameweek's last-place fine is Bob's second of the season" in text
        assert "this gameweek's red-card fine is Bob's first of the season" in text

    def test_nothing_is_placed_without_a_tally(self):
        """A store failure costs the tally, not the recap (R4) -- and an
        unplaced fine is the honest outcome, since the ordinal would have to
        be guessed."""
        data = _make_recap_data(fines=[self._fine("Bob")])

        text = format_recap_fines_context(data, None)

        assert "Bob finished last" in text
        assert "of the season" not in text

    def test_nothing_is_placed_when_the_tally_missed_this_gameweek(self):
        """The tally is a fold over written rows. If this gameweek's row never
        landed, its total excludes the fine being placed, and an ordinal read
        off it would be one short."""
        data = _make_recap_data(fines=[self._fine("Bob")])
        tally = self._tally(ManagerFineTally(
            manager_key=1, manager_name="Bob",
            counts={"last-place": 1}, fined_gameweeks=[4],
        ))

        text = format_recap_fines_context(data, tally)

        assert "of the season" not in text

    def test_a_keyless_fine_falls_back_to_a_unique_name(self):
        data = _make_recap_data(fines=[self._fine("Bob", key=None)])
        tally = self._tally(ManagerFineTally(
            manager_key=7, manager_name="Bob",
            counts={"last-place": 1}, fined_gameweeks=[10],
            ruled_gameweeks_by_rule={"last-place": _ALL_TEN},
        ))

        text = format_recap_fines_context(data, tally)

        assert "is Bob's first of the season" in text

    def test_a_keyless_fine_is_left_unplaced_when_the_name_is_shared(self):
        """Two managers can share a display name, which is why rulings are
        keyed. Placing the fine against the wrong one is worse than not
        placing it."""
        data = _make_recap_data(fines=[self._fine("Bob", key=None)])
        tally = self._tally(
            ManagerFineTally(
                manager_key=1, manager_name="Bob",
                counts={"last-place": 1}, fined_gameweeks=[10],
                ruled_gameweeks_by_rule={"last-place": _ALL_TEN},
            ),
            ManagerFineTally(
                manager_key=2, manager_name="Bob",
                counts={"last-place": 4}, fined_gameweeks=[1, 2, 3, 10],
                ruled_gameweeks_by_rule={"last-place": _ALL_TEN},
            ),
        )

        text = format_recap_fines_context(data, tally)

        assert "of the season" not in text

    def test_the_season_fines_section_leaves_out_each_managers_fined_gameweeks(self):
        tally = self._tally(ManagerFineTally(
            manager_key=1, manager_name="Bob",
            counts={"last-place": 2}, fined_gameweeks=[3, 10],
        ))

        text = format_recap_season_fines_context(tally)

        assert "- Bob: 2 (2 last-place)" in text
        assert "fined in" not in text

    def test_a_gap_in_the_span_forbids_the_ordinal_instead_of_asserting_it(self):
        """GW3 was never captured. If Bob was fined there, the tally never
        counted it, so "their first of the season" would be false -- exactly
        the claim this section exists to prevent, arrived at from the other
        direction."""
        data = _make_recap_data(fines=[self._fine("Bob")])
        tally = self._tally(ManagerFineTally(
            manager_key=1, manager_name="Bob",
            counts={"last-place": 1}, fined_gameweeks=[10],
            ruled_gameweeks_by_rule={"last-place": [1, 2, 4, 5, 6, 7, 8, 9, 10]},
        ))

        text = format_recap_fines_context(data, tally)

        assert "the ledger records 1 last-place fine against Bob" in text
        assert "GW3 never ruled last-place against Bob" in text
        assert "Do not number this fine." in text
        assert "first of the season" not in text

    def test_a_rule_its_own_gameweek_could_not_rule_is_a_gap_of_its_own(self):
        """The coarse backfill tier carries no squad, so it rules `last-place`
        and structurally cannot rule `red-card`. A span fully ruled for one is
        therefore not proof for the other, which `ruled_gameweeks` alone
        cannot tell them apart on."""
        data = _make_recap_data(fines=[
            self._fine("Bob"), self._fine("Bob", rule="red-card"),
        ])
        tally = self._tally(ManagerFineTally(
            manager_key=1, manager_name="Bob",
            counts={"last-place": 1, "red-card": 1}, fined_gameweeks=[10],
            ruled_gameweeks=_ALL_TEN,
            ruled_gameweeks_by_rule={
                "last-place": _ALL_TEN, "red-card": [4, 5, 6, 7, 8, 9, 10],
            },
        ))

        text = format_recap_fines_context(data, tally)

        assert "this gameweek's last-place fine is Bob's first of the season" in text
        assert "GW1-3 never ruled red-card against Bob" in text

    def test_a_ledger_starting_after_the_league_did_cannot_assert_a_first(self):
        """A league that adopted the tool mid-season has no recorded GW1-4.
        The tally cannot tell that from a clean one, so it says so rather than
        reading silence as innocence."""
        data = _make_recap_data(fines=[self._fine("Bob")])
        tally = self._tally(ManagerFineTally(
            manager_key=1, manager_name="Bob",
            counts={"last-place": 1}, fined_gameweeks=[10],
            first_recorded_gameweek=5,
            ruled_gameweeks_by_rule={"last-place": [5, 6, 7, 8, 9, 10]},
        ))

        text = format_recap_fines_context(data, tally)

        assert "GW1-4 never ruled last-place against Bob" in text
        assert "Do not number this fine." in text

    def test_the_system_prompt_forbids_an_unsupported_repeat(self):
        system, _ = get_recap_synthesis_prompt(
            gw=10, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="Bob fined",
        )

        assert 'their "another" or "again"' in system
        assert "use that placement and no other" in system
        assert '"second", "twice", "back-to-back", "in a row", "still"' in system
        assert "two managers each on 1 are two first offences, not a repeat" in system


# ---------------------------------------------------------------------------
# Prior seasons (issue #131)
# ---------------------------------------------------------------------------


def _season(name: str, points: int, rank: int, pct: str | None = None) -> RecapPriorSeason:
    season = RecapPriorSeason(season_name=name, total_points=points, rank=rank)
    if pct is not None:
        season["rank_percentage"] = pct
    return season


def _with_prior_seasons(
    name: str, entry_id: int, seasons: list[RecapPriorSeason] | None,
) -> RecapManagerEntry:
    """A manager row as the collector leaves it: `seasons` None means the
    fetch failed, `[]` means the API answered with no seasons. A manager the
    collector never reached is the bare `_make_manager`, with no key."""
    manager = _make_manager(name=name, entry_id=entry_id)
    manager["prior_seasons"] = seasons
    return manager


class TestLeagueOpeningGameweek:
    def test_gw1_opens_a_league_that_started_with_the_season(self):
        assert is_league_opening_gameweek(1) is True
        assert is_league_opening_gameweek(2) is False

    def test_a_late_starting_league_opens_on_its_own_start_event(self):
        assert is_league_opening_gameweek(3, start_event=3) is True
        assert is_league_opening_gameweek(1, start_event=3) is False
        assert is_league_opening_gameweek(4, start_event=3) is False

    def test_the_opener_is_the_one_gameweek_with_no_predecessor_from_the_start_on(self):
        """The two helpers share one definition of the league's first
        gameweek, so a league cannot open on one gameweek and gain a
        previous table on another."""
        assert not _has_previous_gameweek(3, 3) and is_league_opening_gameweek(3, 3)
        assert _has_previous_gameweek(4, 3) and not is_league_opening_gameweek(4, 3)


class TestShapePriorSeasons:
    def test_keeps_the_api_fields_and_the_percentage_as_a_string(self):
        rows = [{"season_name": "2024/25", "total_points": 2100, "rank": 150000, "rank_percentage": "2"}]

        assert shape_prior_seasons(rows) == [_season("2024/25", 2100, 150000, "2")]

    def test_a_row_without_a_percentage_omits_it_rather_than_inventing_one(self):
        rows = [{"season_name": "2024/25", "total_points": 2100, "rank": 150000}]

        assert "rank_percentage" not in shape_prior_seasons(rows)[0]

    def test_a_row_that_is_not_a_mapping_is_skipped_rather_than_fatal(self):
        rows = [None, 3, {"season_name": "2023/24", "total_points": 1900, "rank": 9}]

        assert [s["season_name"] for s in shape_prior_seasons(rows)] == ["2023/24"]

    def test_a_row_missing_a_finish_is_dropped_rather_than_zeroed(self):
        rows = [
            {"season_name": "2024/25", "total_points": 2100},
            {"season_name": "2023/24", "total_points": 1900, "rank": 9},
        ]

        assert [s["season_name"] for s in shape_prior_seasons(rows)] == ["2023/24"]


class TestCollectPriorSeasons:
    async def test_each_manager_gets_the_past_list_the_api_returns(self):
        client = _FakeClassicClient({}, {}, history_by_entry={
            1: {"current": [], "past": [
                {"season_name": "2024/25", "total_points": 2100, "rank": 150000, "rank_percentage": "2"},
            ]},
        })
        managers = [_make_manager(name="Alice", entry_id=1)]

        await collect_prior_seasons(client, managers)

        assert managers[0]["prior_seasons"] == [_season("2024/25", 2100, 150000, "2")]

    async def test_an_empty_past_is_recorded_as_empty_not_left_absent(self):
        """The API's own answer for an entry in its first season -- a real
        fact the report states, not a gap it hides."""
        client = _FakeClassicClient({}, {}, history_by_entry={1: {"current": [], "past": []}})
        managers = [_make_manager(name="Alice", entry_id=1)]

        await collect_prior_seasons(client, managers)

        assert managers[0]["prior_seasons"] == []

    async def test_a_history_without_a_past_key_reads_as_no_seasons(self):
        client = _FakeClassicClient({}, {})  # answers {"current": []} for everyone
        managers = [_make_manager(name="Alice", entry_id=1)]

        await collect_prior_seasons(client, managers)

        assert managers[0]["prior_seasons"] == []

    async def test_a_failed_fetch_records_none_rather_than_no_seasons(self):
        """None, not `[]` -- an empty list is the API saying "no seasons" --
        and not absent either, since absent means never asked: a gameweek
        where every fetch fails still has to say so."""
        class _FailingHistoryClient:
            async def get_manager_history(self, entry_id):
                raise RuntimeError("network blip")

        managers = [_make_manager(name="Alice", entry_id=1)]

        await collect_prior_seasons(_FailingHistoryClient(), managers)

        assert "prior_seasons" in managers[0]
        assert managers[0]["prior_seasons"] is None

    async def test_one_failure_does_not_disturb_the_others(self):
        class _PartialClient:
            async def get_manager_history(self, entry_id):
                if entry_id == 2:
                    raise RuntimeError("network blip")
                return {"current": [], "past": [
                    {"season_name": "2025/26", "total_points": 2000, "rank": 900000},
                ]}

        managers = [_make_manager(name="Alice", entry_id=1), _make_manager(name="Bob", entry_id=2)]

        await collect_prior_seasons(_PartialClient(), managers)

        assert managers[0]["prior_seasons"] == [_season("2025/26", 2000, 900000)]
        assert managers[1]["prior_seasons"] is None

    async def test_a_pre_fetched_history_map_is_used_without_a_request(self):
        class _NeverAsked:
            async def get_manager_history(self, entry_id):
                raise AssertionError("the map should have answered")

        managers = [_make_manager(name="Alice", entry_id=1)]

        await collect_prior_seasons(_NeverAsked(), managers, histories={
            1: {"past": [{"season_name": "2025/26", "total_points": 2000, "rank": 900000}]},
        })

        assert managers[0]["prior_seasons"] == [_season("2025/26", 2000, 900000)]

    async def test_a_row_that_is_not_a_mapping_is_skipped_rather_than_taking_the_run_down(self):
        managers = [_make_manager(name="Alice", entry_id=1)]

        await collect_prior_seasons(_FakeClassicClient({}, {}), managers, histories={
            1: {"past": [None, {"season_name": "2025/26", "total_points": 2000, "rank": 900000}]},
        })

        assert managers[0]["prior_seasons"] == [_season("2025/26", 2000, 900000)]

    async def test_a_past_that_is_not_a_list_reads_as_unavailable_not_as_no_seasons(self):
        managers = [_make_manager(name="Alice", entry_id=1)]

        await collect_prior_seasons(
            _FakeClassicClient({}, {}), managers, histories={1: {"past": "garbage"}},
        )

        assert managers[0]["prior_seasons"] is None


class TestFetchManagerHistories:
    async def test_a_failure_is_none_and_the_rest_are_intact(self):
        class _Partial:
            async def get_manager_history(self, entry_id):
                if entry_id == 2:
                    raise RuntimeError("boom")
                return {"current": [], "past": []}

        histories = await fetch_manager_histories(
            _Partial(), [_make_manager(name="Alice", entry_id=1), _make_manager(name="Bob", entry_id=2)],
        )

        assert histories == {1: {"current": [], "past": []}, 2: None}


class _CountingClassicClient(_FakeClassicClient):
    """`_FakeClassicClient` that records every manager-history request."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history_calls: list[int] = []

    async def get_manager_history(self, entry_id):
        self.history_calls.append(entry_id)
        return await super().get_manager_history(entry_id)


class TestClassicCollectorPriorSeasons:
    """The classic collector owns the opener gate and fetches each manager's
    history once, shared between the league-start offset and the
    prior-seasons roster (review on #304: a late-starting league's own
    opener used to fetch every manager twice)."""

    _STANDINGS = [
        {"entry": 1, "player_name": "Alice", "event_total": 60, "total": 60},
        {"entry": 2, "player_name": "Bob", "event_total": 50, "total": 50},
    ]
    _PICKS = {
        1: _picks_response(points=60, total_points=60),
        2: _picks_response(points=50, total_points=50),
    }
    _PAST = [{"season_name": "2025/26", "total_points": 2000, "rank": 900000, "rank_percentage": "20"}]
    _LATE_START_HISTORIES = {
        1: {"current": [{"event": 2, "total_points": 20}], "past": _PAST},
        2: {"current": [{"event": 2, "total_points": 10}], "past": []},
    }

    @classmethod
    def _client(cls, start_event=None, history_by_entry=None):
        return _CountingClassicClient(
            _standings_response(cls._STANDINGS, start_event), cls._PICKS,
            history_by_entry=history_by_entry,
        )

    @staticmethod
    async def _collect(client, gw, *, with_prior_seasons):
        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=gw,
            live_stats={}, player_map={}, teams={}, is_live_gw=False,
            with_prior_seasons=with_prior_seasons,
        )
        return {m["manager_name"]: m for m in data["managers"]}

    async def test_the_opener_fetches_once_per_manager_when_asked(self):
        client = self._client(history_by_entry={1: {"current": [], "past": self._PAST}})

        by_name = await self._collect(client, gw=1, with_prior_seasons=True)

        assert sorted(client.history_calls) == [1, 2]
        assert by_name["Alice"]["prior_seasons"] == [_season("2025/26", 2000, 900000, "20")]
        assert by_name["Bob"]["prior_seasons"] == []

    async def test_nothing_is_fetched_unless_asked(self):
        client = self._client(history_by_entry={1: {"current": [], "past": self._PAST}})

        by_name = await self._collect(client, gw=1, with_prior_seasons=False)

        assert client.history_calls == []
        assert all("prior_seasons" not in m for m in by_name.values())

    async def test_a_later_gameweek_fetches_nothing_even_when_asked(self):
        client = self._client(history_by_entry={1: {"current": [], "past": self._PAST}})

        by_name = await self._collect(client, gw=2, with_prior_seasons=True)

        assert client.history_calls == []
        assert all("prior_seasons" not in m for m in by_name.values())

    async def test_a_late_starting_league_shares_one_fetch_between_offset_and_roster(self):
        client = self._client(start_event=3, history_by_entry=self._LATE_START_HISTORIES)

        by_name = await self._collect(client, gw=3, with_prior_seasons=True)

        assert sorted(client.history_calls) == [1, 2]
        assert by_name["Alice"]["total_points"] == 40
        assert by_name["Bob"]["total_points"] == 40
        assert by_name["Alice"]["prior_seasons"] == [_season("2025/26", 2000, 900000, "20")]
        assert by_name["Bob"]["prior_seasons"] == []

    async def test_a_late_starting_league_past_its_opener_fetches_only_for_the_offset(self):
        client = self._client(start_event=3, history_by_entry=self._LATE_START_HISTORIES)

        by_name = await self._collect(client, gw=4, with_prior_seasons=True)

        assert sorted(client.history_calls) == [1, 2]
        assert by_name["Alice"]["total_points"] == 40
        assert all("prior_seasons" not in m for m in by_name.values())

    async def test_one_managers_failed_fetch_costs_both_readers_only_that_manager(self):
        class _Partial(_CountingClassicClient):
            async def get_manager_history(self, entry_id):
                if entry_id == 2:
                    raise RuntimeError("network blip")
                return await super().get_manager_history(entry_id)

        client = _Partial(
            _standings_response(self._STANDINGS, 3), self._PICKS,
            history_by_entry=self._LATE_START_HISTORIES,
        )

        by_name = await self._collect(client, gw=3, with_prior_seasons=True)

        assert by_name["Alice"]["total_points"] == 40
        assert by_name["Alice"]["prior_seasons"] == [_season("2025/26", 2000, 900000, "20")]
        assert "total_points" not in by_name["Bob"]
        assert by_name["Bob"]["prior_seasons"] is None


class TestFormatPriorSeasonsLine:
    def test_a_full_record_reads_as_one_sentence(self):
        seasons = [
            _season("2014/15", 1802, 1200345, "38"),
            _season("2023/24", 2512, 6780, "0.1"),
            _season("2025/26", 2301, 55120, "1"),
        ]

        line = format_prior_seasons_line("Alice", seasons, previous_season_name="2025/26")

        assert line == (
            "Alice: 3 prior FPL seasons played (2014/15 to 2025/26; 9 seasons missed in "
            "between), making this their fourth season of FPL. Last season (2025/26): 2,301 pts, "
            "rank 55,120 (top 1%). Best season: 2023/24, 2,512 pts, rank 6,780 (top 0.1%)."
        )

    def test_tenure_counts_seasons_played_not_the_span(self):
        """The issue's own correction: `past` lists only the seasons played,
        so the span can be longer than the count, and the gap is said."""
        seasons = [_season("2020/21", 1500, 3000000), _season("2025/26", 2000, 900000)]

        line = format_prior_seasons_line("Alice", seasons, previous_season_name="2025/26")

        assert line.startswith(
            "Alice: 2 prior FPL seasons played (2020/21 to 2025/26; 4 seasons missed in "
            "between), making this their third season of FPL."
        )

    def test_a_single_season_has_no_best_clause(self):
        line = format_prior_seasons_line(
            "Bob", [_season("2025/26", 1900, 2345678, "31")], previous_season_name="2025/26",
        )

        assert line == (
            "Bob: 1 prior FPL season played (2025/26), making this their second season of FPL. "
            "Last season (2025/26): 1,900 pts, rank 2,345,678 (top 31%)."
        )

    def test_a_most_recent_season_that_is_also_the_best_is_said_once(self):
        seasons = [_season("2023/24", 1500, 3000000, "60"), _season("2025/26", 2000, 900000, "20")]

        line = format_prior_seasons_line("Alice", seasons, previous_season_name="2025/26")

        assert line.endswith(
            "Last season (2025/26): 2,000 pts, rank 900,000 (top 20%), their best to date.",
        )
        assert "Best season" not in line

    def test_a_season_sat_out_is_not_called_last_season(self):
        line = format_prior_seasons_line(
            "Cam", [_season("2023/24", 1500, 3000000, "60")], previous_season_name="2025/26",
        )

        assert "Most recent season (2023/24; did not play 2025/26): 1,500 pts" in line
        assert "Last season" not in line

    def test_without_a_previous_season_name_the_most_recent_is_called_last_season(self):
        line = format_prior_seasons_line("Cam", [_season("2023/24", 1500, 3000000)])

        assert "Last season (2023/24)" in line

    def test_the_api_order_does_not_decide_which_season_is_most_recent(self):
        seasons = [_season("2025/26", 2000, 900000), _season("2020/21", 1500, 3000000)]

        line = format_prior_seasons_line("Dee", seasons, previous_season_name="2025/26")

        assert "(2020/21 to 2025/26;" in line
        assert "Last season (2025/26)" in line

    def test_a_percentage_the_api_did_not_send_is_not_printed(self):
        line = format_prior_seasons_line("Eve", [_season("2025/26", 2000, 900000)])

        assert "top" not in line
        assert "%" not in line

    def test_a_tie_on_points_and_rank_names_the_more_recent_season_as_best(self):
        seasons = [_season("2020/21", 2000, 900000), _season("2025/26", 2000, 900000)]

        line = format_prior_seasons_line("Fay", seasons, previous_season_name="2025/26")

        assert line.endswith("their best to date.")

    def test_a_best_season_is_the_one_with_most_points_then_the_better_rank(self):
        seasons = [
            _season("2020/21", 2000, 900000),
            _season("2021/22", 2000, 800000),
            _season("2025/26", 1500, 3000000),
        ]

        line = format_prior_seasons_line("Gus", seasons, previous_season_name="2025/26")

        assert line.endswith("Best season: 2021/22, 2,000 pts, rank 800,000.")


class TestSummarisePriorSeasons:
    def test_none_when_nothing_was_fetched(self):
        """Absent everywhere means the fetch was never made -- every
        gameweek but the opener -- which is not a roster of failures."""
        assert summarise_prior_seasons([_make_manager(name="Alice")]) is None

    def test_every_fetch_failing_is_not_the_same_as_no_fetch(self):
        """The whole cohort unreachable is a section naming them all as
        unfetched, not the silence a non-opener gets."""
        managers = [_with_prior_seasons("Alice", 1, None), _with_prior_seasons("Bob", 2, None)]

        summary = summarise_prior_seasons(managers)

        assert summary is not None
        assert summary.lines == []
        assert summary.unavailable == ["Alice", "Bob"]
        assert summary.coverage_lines == ["Prior seasons could not be fetched for Alice and Bob."]

    def test_sorts_managers_by_most_recent_finish_then_name(self):
        managers = [
            _with_prior_seasons("Cam", 3, [_season("2025/26", 1500, 3000000)]),
            _with_prior_seasons("Bob", 2, [_season("2025/26", 2000, 900000)]),
            _with_prior_seasons("Alice", 1, [_season("2025/26", 2000, 900000)]),
        ]

        summary = summarise_prior_seasons(managers, previous_season_name="2025/26")

        assert summary is not None
        assert [line.split(":")[0] for line in summary.lines] == ["Alice", "Bob", "Cam"]

    def test_separates_no_record_from_no_answer_and_counts_the_whole_cohort(self):
        managers = [
            _with_prior_seasons("Alice", 1, [_season("2025/26", 2000, 900000)]),
            _with_prior_seasons("Bob", 2, []),
            _with_prior_seasons("Cam", 3, None),
        ]

        summary = summarise_prior_seasons(managers, previous_season_name="2025/26")

        assert summary is not None
        assert len(summary.lines) == 1
        assert summary.new_to_fpl == ["Bob"]
        assert summary.unavailable == ["Cam"]
        assert summary.total_managers == 3
        assert summary.coverage_lines == [
            "No prior FPL seasons on record for Bob: this is their first season of FPL on record.",
            "Prior seasons could not be fetched for Cam.",
        ]

    def test_a_manager_never_asked_is_outside_the_summary_not_unfetched(self):
        """Absent key, not None: "could not be fetched" is a claim about a
        request that was made (review on #304)."""
        managers = [_with_prior_seasons("Alice", 1, []), _make_manager(name="Bob", entry_id=2)]

        summary = summarise_prior_seasons(managers)

        assert summary is not None
        assert summary.new_to_fpl == ["Alice"]
        assert summary.unavailable == []
        assert summary.total_managers == 1

    def test_coverage_lines_join_names_in_prose(self):
        managers = [_with_prior_seasons(n, i, []) for i, n in enumerate(["Cam", "Alice", "Bob"], 1)]

        summary = summarise_prior_seasons(managers)

        assert summary is not None
        assert summary.coverage_lines == [
            "No prior FPL seasons on record for Alice, Bob and Cam: this is their first "
            "season of FPL on record.",
        ]

    def test_the_previous_season_name_reaches_every_line(self):
        managers = [_with_prior_seasons("Alice", 1, [_season("2023/24", 1500, 3000000)])]

        summary = summarise_prior_seasons(managers, previous_season_name="2025/26")

        assert summary is not None
        assert "did not play 2025/26" in summary.lines[0]


class TestPriorSeasonsPromptSection:
    def test_empty_when_nothing_was_fetched(self):
        assert format_recap_prior_seasons_context(None) == ""

    def test_counts_the_managers_with_a_record_against_the_whole_cohort(self):
        summary = PriorSeasonsSummary(lines=["Alice: a record."], new_to_fpl=["Bob"], unavailable=["Cam"])

        text = format_recap_prior_seasons_context(summary)

        assert "Total managers with prior FPL seasons on record: 1 of 3" in text
        assert "- Alice: a record." in text

    def test_states_absence_rather_than_omitting_it(self):
        summary = PriorSeasonsSummary(lines=[], new_to_fpl=["Bob"], unavailable=["Cam", "Dee"])

        text = format_recap_prior_seasons_context(summary)

        assert "No prior FPL seasons on record for Bob: this is their first season of FPL on record." in text
        assert "Prior seasons could not be fetched for Cam and Dee." in text
        assert "say nothing about their earlier seasons either way" in text

    def test_the_absence_statements_are_the_reports_own_sentences(self):
        """One rendering for both surfaces (review on #304): the prompt
        repeats `coverage_lines` verbatim rather than rebuilding the name
        lists with its own joining."""
        summary = PriorSeasonsSummary(lines=[], new_to_fpl=["Bob", "Cam"], unavailable=["Dee"])

        text = format_recap_prior_seasons_context(summary)

        assert all(line in text.splitlines() for line in summary.coverage_lines)

    def test_no_instruction_about_absence_when_nobody_is_absent(self):
        summary = PriorSeasonsSummary(lines=["Alice: a record."], new_to_fpl=[], unavailable=[])

        text = format_recap_prior_seasons_context(summary)

        assert "say nothing about their earlier seasons" not in text

    def test_says_the_record_is_fpl_wide_and_silent_on_league_tenure(self):
        text = format_recap_prior_seasons_context(
            PriorSeasonsSummary(lines=[], new_to_fpl=["Bob"], unavailable=[]),
        )

        assert "FPL-wide" in text
        assert "say nothing about when anyone joined this league" in text

    def test_the_section_lands_after_league_history_and_before_research(self):
        _, user = get_recap_synthesis_prompt(
            gw=1, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
            league_history_text="Season phase: the season opener",
            prior_seasons_text="Total managers with prior FPL seasons on record: 1 of 1",
            research_summary="Some research.",
        )

        assert "## Prior Seasons" in user
        assert (
            user.index("## League History")
            < user.index("## Prior Seasons")
            < user.index("## GW Context (from research)")
        )

    def test_no_section_when_the_caller_supplies_no_text(self):
        _, user = get_recap_synthesis_prompt(
            gw=5, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )

        assert "## Prior Seasons" not in user

    def test_the_system_prompt_pins_prior_season_claims_to_the_section(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert '"## Prior Seasons" section is present' in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "FPL-wide, never this league's" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert 'never "their 11th season in this league"' in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "mention nobody's earlier seasons" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_the_system_prompt_forbids_deriving_a_trajectory(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "Never derive a trajectory the section does not state" in RECAP_SYNTHESIS_SYSTEM_PROMPT


class TestRecapTitleIsTheReportsNotTheModels:
    """Issue #349: every recap used to land under a different H1. The
    editorial invented one, restyled the gameweek, dropped or rewrote the
    league's name, or wrote none at all and let `# Awards` become the
    document's title. The title is the report writer's now, the prompt asks
    for a headline beneath it rather than a title, and the reply is held to
    that shape instead of trusted."""

    def test_the_title_is_one_string_with_the_league_name_verbatim(self):
        from fpl_cli.cli._league_recap_types import recap_title

        assert recap_title(4, "Sunday League") == "Gameweek 4 Recap: Sunday League"
        assert recap_title(4, "The Manager's Arms") == (
            "Gameweek 4 Recap: The Manager's Arms"
        )
        assert recap_title(1, "   ") == "Gameweek 1 Recap"
        # The collectors default the name only when the API omits it, not
        # when it sends null; a crash here is swallowed by the report agent
        # into a `--save` that exits 0 having written nothing (review).
        assert recap_title(1, None) == "Gameweek 1 Recap"

    def test_the_user_prompt_names_the_title_and_opens_with_no_heading(self):
        from fpl_cli.cli._league_recap_types import recap_title

        _, user = get_recap_synthesis_prompt(
            gw=4, league_name="Sunday League", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )

        assert f'Title: "{recap_title(4, "Sunday League")}"' in user
        assert "do not repeat it" in user
        assert "League: Sunday League" in user
        # The old opening line was a `# ` heading the model mirrored, restyled
        # or skipped -- context handed over as if it were an output contract.
        assert not user.startswith("#")
        assert "# Gameweek 4 Recap" not in user

    def test_the_system_prompt_asks_for_a_headline_and_forbids_a_title(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "<output_format>" in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert 'never write a title or a "# " heading of your own' in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert 'Open with one headline as a "## " heading' in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert "the league's name is not yours to restyle" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    @pytest.mark.parametrize(
        ("heading", "league_name", "expected"),
        [
            # The shapes the issue lists: one season of one league's recaps.
            ("# GW2 Recap: Sunday League", "Sunday League", None),
            ("# GW2 Recap: The Manager's Arms", "The Manager's Arms", None),
            ("# The Manager's Arms: GW4 Recap", "The Manager's Arms", None),
            (
                "# Gameweek 4 Recap: Chaos, Chips, and a Captain Called Isak", "Sunday League",
                "## Chaos, Chips, and a Captain Called Isak",
            ),
            (
                "# GW3 Recap: The Arms Digest", "The Manager's Arms",
                "## The Arms Digest",
            ),
            (
                "# Gameweek 3 Recap: The Sunday League, or The Great Purge", "Sunday League",
                "## The Sunday League, or The Great Purge",
            ),
            # And the shapes the guard has to hold beyond them.
            ("# **GW4 Recap: Sunday League**", "Sunday League", None),
            ("# The Sunday League", "Sunday League", None),
            ("# Gameweek 4 Recap", "Sunday League", None),
            ("# Gameweek 4 — Chaos", "Sunday League", "## Chaos"),
            ("## Chaos and chips", "Sunday League", "## Chaos and chips"),
            ("### Chaos", "Sunday League", "## Chaos"),
            # A headline that happens to open with the league's name is a
            # headline: only a colon, dash or pipe makes the name furniture.
            ("# Sunday League Falls Apart", "Sunday League", "## Sunday League Falls Apart"),
            # Emphasis of every kind, and emphasis on only part of the title,
            # is unwrapped before the title is recognised (review).
            ("# `GW4 Recap: Sunday League`", "Sunday League", None),
            ("# ~~GW4 Recap: Sunday League~~", "Sunday League", None),
            ("# GW4 Recap: **Sunday League**", "Sunday League", None),
            ("# **Chaos** - GW4 Recap", "Sunday League", "## Chaos"),
            # A gameweek callback in the hook is the hook's, a compound word
            # is not a join, and "Recap" alone is not the title (review).
            ("# Bob Never Learns - Gameweek 7", "Sunday League", "## Bob Never Learns - Gameweek 7"),
            ("# Recap-worthy chaos this week", "Sunday League", "## Recap-worthy chaos this week"),
            ("# Recap - Chaos this week", "Sunday League", "## Recap - Chaos this week"),
            ("# Chaos: Gameweek 4 Recap", "Sunday League", "## Chaos"),
        ],
    )
    def test_the_opening_heading_is_dropped_or_demoted(self, heading, league_name, expected):
        from fpl_cli.prompts.league_recap import normalise_recap_editorial

        result = normalise_recap_editorial(
            f"{heading}\n\nAlice ran away with it.", league_name=league_name,
        )

        if expected is None:
            assert result == "Alice ran away with it."
        else:
            assert result == f"{expected}\n\nAlice ran away with it."

    def test_a_title_followed_by_a_headline_keeps_only_the_headline(self):
        from fpl_cli.prompts.league_recap import normalise_recap_editorial

        result = normalise_recap_editorial(
            "# GW4 Recap: Sunday League\n\n## Chaos\n\nprose", league_name="Sunday League",
        )

        assert result == "## Chaos\n\nprose"

    def test_no_h1_survives_anywhere_in_the_editorial(self):
        from fpl_cli.prompts.league_recap import normalise_recap_editorial

        result = normalise_recap_editorial(
            "Alice ran away with it.\n\n# The bench\n\nBob benched 31.", league_name="Sunday League",
        )

        assert result == "Alice ran away with it.\n\n## The bench\n\nBob benched 31."

    def test_a_title_restated_later_in_the_editorial_is_dropped_too(self):
        """Review: the duplicate-title shape relocated one heading down is
        still the duplicate-title shape, at whatever level it was written."""
        from fpl_cli.prompts.league_recap import normalise_recap_editorial

        result = normalise_recap_editorial(
            "## Chaos and chips\n\nAlice ran away with it.\n\n"
            "# Gameweek 4 Recap: Sunday League\n\nThanks for reading!\n\n"
            "### Sunday League: GW4 Recap\n\nSee you next week.",
            league_name="Sunday League",
        )

        assert result == (
            "## Chaos and chips\n\nAlice ran away with it.\n\n"
            "Thanks for reading!\n\nSee you next week."
        )

    def test_a_later_heading_with_a_hook_keeps_its_level_below_h1(self):
        from fpl_cli.prompts.league_recap import normalise_recap_editorial

        result = normalise_recap_editorial(
            "## Chaos\n\nprose\n\n### Gameweek 4 - The bench\n\nmore",
            league_name="Sunday League",
        )

        assert result == "## Chaos\n\nprose\n\n### The bench\n\nmore"

    def test_a_hash_inside_a_fenced_block_is_code_not_a_heading(self):
        """Review: the same fence-awareness every other heading scanner in
        `fpl_cli.utils.markdown` keeps."""
        from fpl_cli.prompts.league_recap import normalise_recap_editorial

        text = "## Chaos\n\nprose\n\n```\n# Gameweek 4 Recap: Sunday League\n```\n\nmore"

        assert normalise_recap_editorial(text, league_name="Sunday League") == text

    def test_prose_with_no_heading_is_left_alone(self):
        from fpl_cli.prompts.league_recap import normalise_recap_editorial

        assert normalise_recap_editorial(
            "Just prose.\n\nMore prose.", league_name="Sunday League",
        ) == "Just prose.\n\nMore prose."
        assert normalise_recap_editorial("", league_name="Sunday League") == ""

    async def test_the_saved_report_opens_on_the_title(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = {
            "gameweek": 4, "league_name": "Sunday League", "fpl_format": "classic",
            "managers": [], "awards": {},
            "synthesis_summary": "## Chaos\n\nAlice ran away with it.",
        }

        result = await agent.run(context={"report_type": "league-recap", "gameweek": 4, "data": data})

        assert result.data is not None
        content = Path(result.data["report_path"]).read_text(encoding="utf-8")
        assert content.splitlines()[0] == "# Gameweek 4 Recap: Sunday League"
        # Title, the run's metadata, then the editorial under its own headline,
        # then the data sections: the block pasted into the group chat is
        # contiguous, and the line above it reads the same every week.
        assert content.index("*Generated:") < content.index("## Chaos") < content.index("# Awards")

    async def test_a_report_with_no_editorial_still_opens_on_the_title(self, tmp_path):
        """GW1's recaps opened on `# Awards`: with no editorial heading, the
        first section heading became the document's."""
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = {
            "gameweek": 1, "league_name": "Draft League", "fpl_format": "draft",
            "managers": [], "awards": {},
        }

        result = await agent.run(context={"report_type": "league-recap", "gameweek": 1, "data": data})

        assert result.data is not None
        content = Path(result.data["report_path"]).read_text(encoding="utf-8")
        assert content.splitlines()[0] == "# Gameweek 1 Recap: Draft League"
        assert result.data["report_path"].endswith("gw1-league-recap-draft.md")

    async def test_a_problems_dict_with_nothing_listed_opens_no_callout(self, tmp_path):
        """PR #462 review: the callout opens on the same lists it prints, so a
        problems dict that lists nothing cannot leave an empty warning block."""
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        data = {
            "gameweek": 4, "league_name": "Sunday League", "fpl_format": "draft",
            "managers": [], "awards": {},
            "synthesis_summary": "## Chaos\n\nAlice ran away with it.",
            "synthesis_problems": {"synthesis_contested_roll_call": []},
        }

        result = await agent.run(context={"report_type": "league-recap", "gameweek": 4, "data": data})

        assert result.data is not None
        content = Path(result.data["report_path"]).read_text(encoding="utf-8")
        assert "[!WARNING]" not in content
        assert "Alice ran away with it." in content


# ---------------------------------------------------------------------------
# Contested attribution check (issue #357)
# ---------------------------------------------------------------------------


def _reported_races() -> tuple[list, RecapNames]:
    """The reported gameweek's three races: Alice Moss won King over Bob Gale,
    Bob Gale won Bogle over Cam Hart, and Dan Ford won Giles over Cam Hart.
    Bob Gale is the winner of one race and beaten in another, which is what
    makes the folded sentence easy to get wrong."""
    managers = [
        _make_manager_with_txns("Alice Moss", [_txn("King", 6, "A", 1)], entry_id=1),
        _lost_by(
            _make_manager_with_txns("Bob Gale", [_txn("Bogle", 4, "B", 1)], entry_id=2),
            _lost("King", "C", priority=2),
        ),
        _lost_by(
            _make_manager(name="Cam Hart", entry_id=3),
            _lost("Bogle", "D", priority=3), _lost("Giles", "E", priority=4),
        ),
        _make_manager_with_txns("Dan Ford", [_txn("Giles", 2, "F", 1)], entry_id=4),
        _make_manager(name="Eve Ray", entry_id=5),
    ]
    return contested_draft_claims(managers), RecapNames.of(managers)


class TestContestedAttributionCheck:
    """Issue #357: the editorial folded two adjacent contested lines into one
    "respectively" clause and drew the second beaten manager from the next
    line's winner slot. The roles are known strings, so the check is
    mechanical rather than another prompt line the model may ignore."""

    def _check(self, summary: str) -> list[str]:
        contests, names = _reported_races()
        return check_contested_attributions(summary, contests, names)

    def test_the_reported_respectively_sentence_is_caught(self):
        problems = self._check(
            "Spare a thought too for Cam Hart and Bob Gale, both beaten to Bogle and Giles "
            "respectively in contested waiver scraps this week, alongside Alice Moss's "
            "tussle with Bob Gale over King, which Moss won."
        )
        assert len(problems) == 1
        assert problems[0].startswith("the editorial says Bob Gale was beaten to Giles")
        # The contested line is quoted so the reader can see the right roles.
        assert "Dan Ford won him; Cam Hart (priority 4) was beaten to him" in problems[0]

    def test_each_race_written_as_its_own_clause_passes(self):
        assert self._check(
            "Bob Gale was beaten to King by Alice Moss. Cam Hart was beaten to Bogle, "
            "and Cam Hart missed out on Giles too."
        ) == []

    def test_one_manager_beaten_in_two_races_may_be_named_once(self):
        assert self._check("Cam Hart was beaten to both Bogle and Giles.") == []

    def test_a_correct_respectively_pairing_is_not_flagged(self):
        # Discouraged by the prompt, but not wrong: the check reports
        # contradictions, not style.
        assert self._check("Bob Gale and Cam Hart were beaten to King and Giles respectively.") == []

    def test_lists_without_respectively_assert_every_pairing(self):
        problems = self._check("Cam Hart and Bob Gale missed out on Bogle and Giles.")
        assert [p.split(", but")[0] for p in problems] == [
            "the editorial says Bob Gale was beaten to Bogle",
            "the editorial says Bob Gale was beaten to Giles",
        ]

    def test_a_manager_who_claimed_nothing_cannot_be_beaten(self):
        problems = self._check("Eve Ray lost out on Giles.")
        assert len(problems) == 1
        assert "Eve Ray was beaten to Giles" in problems[0]

    def test_the_active_form_checks_winner_and_beaten_alike(self):
        assert self._check("Alice Moss beat Bob Gale to King.") == []
        problems = self._check("Dan Ford beat Bob Gale to King.")
        assert [p.split(", but")[0] for p in problems] == [
            "the editorial says Dan Ford won King",
        ]
        problems = self._check("Dan Ford pipped Bob Gale to Giles.")
        assert [p.split(", but")[0] for p in problems] == [
            "the editorial says Bob Gale was beaten to Giles",
        ]

    def test_a_unique_surname_or_first_name_counts_as_the_manager(self):
        problems = self._check("Gale was pipped to **Giles**.")
        assert len(problems) == 1
        assert "Bob Gale was beaten to Giles" in problems[0]

    def test_a_sentence_it_cannot_pin_to_a_role_is_left_alone(self):
        assert self._check(
            "Bob Gale lost 12 points to the bench, then won Bogle. "
            "Giles went to Dan Ford, and Bob Gale was beaten by Alice Moss to King."
        ) == []

    @pytest.mark.parametrize("summary", [
        "Bob Gale was pipped by Alice Moss to King.",
        "Bob Gale was edged out by Alice Moss for King.",
        "Bob Gale was outbid by Alice Moss for King.",
        "Bob Gale was beaten to King by Alice Moss.",
        "Bob Gale lost the race for King.",
    ])
    def test_a_passive_with_its_winner_named_is_read_the_right_way_round(self, summary):
        # PR #368 review: "pipped by" used to be read as the active voice,
        # inverting both roles on a correct sentence.
        assert self._check(summary) == []

    @pytest.mark.parametrize("summary", [
        "Bob Gale got beat to King.",
        "Bob Gale was beat to King by Alice Moss.",
        "Bob Gale was beat by Alice Moss to King.",
    ])
    def test_beat_as_a_participle_is_passive(self, summary):
        assert self._check(summary) == []

    def test_the_winner_named_after_by_is_checked_too(self):
        problems = self._check("Bob Gale was pipped by Dan Ford to King.")
        assert [p.split(", but")[0] for p in problems] == ["the editorial says Dan Ford won King"]

    @pytest.mark.parametrize("summary", [
        "Alice Moss lost 3 points on King.",
        "Alice Moss lost King to a hamstring injury in the swap.",
        "Alice Moss missed out on King's bonus.",
        "Cam Hart beat Dan Ford thanks to Giles.",
        "Bob Gale was never beaten to Bogle.",
        "Bob Gale was not beaten to Bogle; he won it.",
        "Bob Gale wasn't beaten to Bogle.",
    ])
    def test_a_sentence_that_is_not_a_race_claim_is_not_read_as_one(self, summary):
        assert self._check(summary) == []

    @pytest.mark.parametrize("summary", [
        "Bob Gale missed out on King, and Bogle was the consolation.",
        "Bob Gale was beaten to King and Bogle arrived as a consolation.",
    ])
    def test_a_player_opening_the_next_clause_is_not_in_the_list(self, summary):
        assert self._check(summary) == []

    @pytest.mark.parametrize("summary", [
        # Issue #499: the manager before the cue is the previous verb's
        # object, or a pronoun restarts the subject. Both sentences are true.
        "Cam Hart still lost the Bogle race to Bob Gale and was beaten to Giles by Dan Ford.",
        "Cam Hart had a busy week. He did lose the Bogle race to Bob Gale, though, and he also missed out on Giles,"
        " who went to Dan Ford.",
        "Cam Hart lost the Bogle race to Bob Gale, and was beaten to Giles.",
        "Cam Hart lost to Bob Gale but beat Dan Ford to Giles.",
        "Cam Hart lost the Bogle race to Bob Gale, and he, sadly, was beaten to Giles.",
    ])
    def test_a_verb_phrase_of_its_own_after_a_manager_is_not_read_as_theirs(self, summary):
        assert self._check(summary) == []

    @pytest.mark.parametrize(("summary", "beaten"), [
        ("Bob Gale, though, was beaten to Giles.", "Bob Gale"),
        # PR #500 review: a pronoun inside an aside on the manager is still
        # about them.
        ("Bob Gale, he of all people, was beaten to Giles.", "Bob Gale"),
        ("Eve Ray, she of the quiet week, missed out on Giles.", "Eve Ray"),
    ])
    def test_an_aside_between_a_manager_and_the_cue_keeps_them_the_subject(self, summary, beaten):
        problems = self._check(summary)
        assert [p.split(", but")[0] for p in problems] == [f"the editorial says {beaten} was beaten to Giles"]

    def test_a_manager_closing_the_previous_clause_is_not_a_subject(self):
        # Two names either side of ", and" are two clauses, not a list, and a
        # singular "was" takes one subject however the names are joined.
        assert self._check("Giles went to Dan Ford, and Cam Hart missed out on Bogle.") == []
        assert self._check("Giles went to Dan Ford and Cam Hart was beaten to Bogle.") == []
        problems = self._check("Dan Ford, Eve Ray, and Cam Hart were all beaten to Bogle.")
        assert [p.split(", but")[0] for p in problems] == [
            "the editorial says Dan Ford was beaten to Bogle",
            "the editorial says Eve Ray was beaten to Bogle",
        ]

    def test_winners_pair_positionally_with_respectively(self):
        assert self._check(
            "Alice Moss and Dan Ford beat Bob Gale and Cam Hart to King and Giles respectively."
        ) == []

    def test_a_claim_holds_when_any_race_under_a_shared_name_bears_it_out(self):
        managers = [
            _make_manager_with_txns("Alice Moss", [_txn("Silva", 4, "A", 1)], entry_id=1),
            _lost_by(_make_manager(name="Bob Gale", entry_id=2), _lost("Silva", "B")),
            _make_manager_with_txns("Cam Hart", [_txn("Silva", 4, "C", 1)], entry_id=3),
            _lost_by(_make_manager(name="Dan Ford", entry_id=4), _lost("Silva", "D")),
            _make_manager(name="Eve Ray", entry_id=5),
        ]
        # Two different players called Silva: the keys are their codes.
        for code, index in ((1, 0), (2, 2)):
            managers[index]["transactions"][0]["player_in_code"] = code
        managers[1]["lost_claims"][0]["player_in_code"] = 1
        managers[3]["lost_claims"][0]["player_in_code"] = 2
        contests = contested_draft_claims(managers)
        assert len(contests) == 2
        names = RecapNames.of(managers)

        assert check_contested_attributions(
            "Bob Gale was beaten to Silva. Alice Moss beat Bob Gale to Silva. "
            "Cam Hart beat Dan Ford to Silva.",
            contests, names,
        ) == []
        problems = check_contested_attributions("Eve Ray was beaten to Silva.", contests, names)
        assert len(problems) == 1
        # Both lines are quoted, since either could be the one meant.
        assert "Alice Moss won him" in problems[0]
        assert "Cam Hart won him" in problems[0]

    def test_a_bystander_surnamed_like_a_hyphenated_player_is_not_put_in_a_race(self):
        """Issue #379: "White" read inside "Gibbs-White" listed a manager who
        claimed nothing beside the one who lost, and a correct sentence was
        flagged as saying the bystander was beaten too."""
        managers = [
            _make_manager_with_txns("Alice Moss", [_txn("King", 6, "A", 1)], entry_id=1),
            _lost_by(_make_manager(name="Bob Gale", entry_id=2), _lost("King", "C")),
            _make_manager(name="Fay White", entry_id=3),
        ]
        managers[1]["squad"] = [_make_squad_player(name="Gibbs-White")]
        data = _make_recap_data(managers=managers)
        assert check_recap_editorial("After selling Gibbs-White, Bob Gale missed out on King.", data) == {}
        # The race itself is still read: the wrong manager is still caught.
        found = check_recap_editorial("After selling Gibbs-White, Alice Moss missed out on King.", data)
        assert [p.split(", but")[0] for p in found[RECAP_WARNING_CONTESTED_MISATTRIBUTION]] == [
            "the editorial says Alice Moss was beaten to King",
        ]

    def test_the_retry_prompt_names_each_wrong_claim(self):
        prompt = get_recap_editorial_retry_prompt(
            "ORIGINAL", {RECAP_WARNING_CONTESTED_MISATTRIBUTION: ["the editorial says Bob won King"]},
        )
        assert prompt.startswith("ORIGINAL\n\n")
        assert "- the editorial says Bob won King" in prompt
        assert '"respectively"' in prompt

    def test_the_retry_prompt_says_a_cut_off_draft_was_cut_off(self):
        # #372: a draft that stopped early has nothing wrong to list.
        prompt = get_recap_editorial_retry_prompt("ORIGINAL", {}, stop_reason="max_tokens")
        assert prompt.startswith("ORIGINAL\n\n")
        assert "cut off before it finished (the provider stopped with max_tokens)" in prompt
        assert "contradicted the data" not in prompt
        assert prompt.endswith("Write the recap again from scratch. Keep it to the length asked for and finish it.")

    def test_the_retry_prompt_without_a_stop_reason_says_nothing_of_one(self):
        prompt = get_recap_editorial_retry_prompt(
            "ORIGINAL", {RECAP_WARNING_CONTESTED_MISATTRIBUTION: ["the editorial says Bob won King"]},
        )
        assert "cut off" not in prompt
        assert "length asked for" not in prompt

    def test_nothing_is_checked_without_a_contested_race(self):
        assert check_contested_attributions("Bob was beaten to Isak.", [], RecapNames.of([_make_manager(name="Bob")])) == []

    def test_the_system_prompt_forbids_folding_two_races_into_one_clause(self):
        system, _ = get_recap_synthesis_prompt(
            gw=3, league_name="Test", fpl_format="draft",
            awards_text="x", standings_text="| t |", fines_text="",
        )
        assert "Each \"Contested players\" line is one race" in system
        assert "never fold two lines into one clause" in system
        assert '"X and Y were beaten to A and B respectively"' in system


# ---------------------------------------------------------------------------
# Roll call of the beaten (issue #459)
# ---------------------------------------------------------------------------


def _roll_call_week() -> list[RecapManagerEntry]:
    """One big race: Alice Moss won King over four beaten managers, Bob Gale,
    Cam Hart, Dan Ford and Eve Ray. Cam Lowe claimed nothing and shares Cam
    Hart's first name, so "Cam" alone names neither of them."""
    return [
        _make_manager_with_txns("Alice Moss", [_txn("King", 6, "A", 1)], entry_id=1),
        _lost_by(_make_manager(name="Bob Gale", entry_id=2), _lost("King", "B", priority=1)),
        _lost_by(_make_manager(name="Cam Hart", entry_id=3), _lost("King", "C", priority=2)),
        _lost_by(_make_manager(name="Dan Ford", entry_id=4), _lost("King", "D", priority=3)),
        _lost_by(_make_manager(name="Eve Ray", entry_id=5), _lost("King", "E", priority=4)),
        _make_manager(name="Cam Lowe", entry_id=6),
    ]


class TestContestedRollCallCheck:
    """Issue #459: five rounds of prompt and data changes never stopped the
    editorial reciting the week's biggest race as a list of the managers
    beaten. The check counts known names in one paragraph; it never judges
    tone, so it fires on a true sentence the contradiction checks would pass."""

    def _check(self, summary: str, managers: list[RecapManagerEntry] | None = None) -> list[str]:
        managers = managers or _roll_call_week()
        return check_contested_roll_calls(summary, contested_draft_claims(managers), RecapNames.of(managers))

    def test_three_beaten_managers_of_one_race_in_one_paragraph_is_one_problem(self):
        assert self._check(
            "Alice Moss took King, with Bob Gale, Cam Hart and Dan Ford all beaten to him."
        ) == ["one paragraph names 3 of the 4 managers beaten to King"]

    def test_the_full_roll_call_counts_every_beaten_manager(self):
        assert self._check(
            "Bob Gale, Cam Hart, Dan Ford and Eve Ray all wanted King, and Alice Moss got him."
        ) == ["one paragraph names 4 of the 4 managers beaten to King"]

    def test_two_beaten_managers_are_not_a_roll_call(self):
        assert self._check("Alice Moss took King, and Bob Gale and Cam Hart both missed out.") == []

    def test_the_same_names_spread_one_per_paragraph_are_not_a_roll_call(self):
        assert self._check(
            "Bob Gale missed out on King.\n\nCam Hart was beaten to him too.\n\nDan Ford rounded out the field."
        ) == []

    def test_a_single_line_break_does_not_end_a_paragraph(self):
        assert self._check(
            "Bob Gale missed out on King.\nCam Hart was beaten to him too.\nDan Ford rounded out the field."
        ) == ["one paragraph names 3 of the 4 managers beaten to King"]

    def test_blank_lines_with_stray_whitespace_still_end_a_paragraph(self):
        assert self._check("Bob Gale missed out on King.\n  \t\nCam Hart and Dan Ford did not.") == []

    def test_paragraphs_that_list_them_at_the_same_count_are_one_line_saying_so(self):
        """PR #462 review: a line per paragraph was the same sentence twice,
        with nothing to tell the paragraphs apart."""
        listed = "Bob Gale, Cam Hart and Dan Ford were beaten to King."
        assert self._check(f"{listed}\n\nAlice Moss enjoyed it.\n\n{listed}") == [
            "2 paragraphs each name 3 of the 4 managers beaten to King",
        ]

    def test_paragraphs_that_list_them_at_different_counts_are_a_line_each(self):
        assert self._check(
            "Bob Gale, Cam Hart and Dan Ford were beaten to King.\n\n"
            "Bob Gale, Cam Hart, Dan Ford and Eve Ray all lost King."
        ) == [
            "one paragraph names 3 of the 4 managers beaten to King",
            "one paragraph names 4 of the 4 managers beaten to King",
        ]

    def test_a_paragraph_that_never_names_the_race_s_player_is_not_about_the_race(self):
        """PR #462 review: three of a five-way race's losers in a standings
        paragraph is no roll call, and the retry would be told to drop names
        it was never shown as losers."""
        assert self._check(
            "Alice Moss took King.\n\n"
            "At the top of the table Bob Gale leads Cam Hart and Dan Ford by four points."
        ) == []

    def test_the_player_named_only_as_a_possessive_still_makes_it_the_races_paragraph(self):
        assert self._check("King's other suitors were Bob Gale, Cam Hart and Dan Ford.") == [
            "one paragraph names 3 of the 4 managers beaten to King",
        ]

    def test_names_in_the_paragraph_after_the_player_is_named_are_an_accepted_miss(self):
        """The player has to be in the same paragraph as the names. A roll call
        that leans on "him" across a blank line is missed, which is the price
        of not firing on every paragraph that happens to name three losers."""
        assert self._check("Alice Moss took King.\n\nBob Gale, Cam Hart and Dan Ford were beaten to him.") == []

    def test_a_heading_ends_the_paragraph_above_it_without_a_blank_line(self):
        """PR #462 review: a heading is a block of its own in the report, so
        two sections separated by one must not be counted together."""
        assert self._check(
            "King went to Alice Moss, and Bob Gale lost him.\n"
            "## Fines\n"
            "Cam Hart and Dan Ford were fined, King or no King."
        ) == []

    def test_a_headline_that_lists_them_is_a_roll_call_of_its_own(self):
        assert self._check("## Bob Gale, Cam Hart and Dan Ford miss out on King\n\nAlice Moss took him.") == [
            "one paragraph names 3 of the 4 managers beaten to King",
        ]

    def test_a_unique_surname_counts(self):
        assert self._check("King went to Alice Moss; Gale, Hart and Ford were all beaten to him.") == [
            "one paragraph names 3 of the 4 managers beaten to King",
        ]

    def test_a_first_name_another_manager_shares_does_not_count(self):
        # "Cam" is Cam Hart and Cam Lowe alike, so it names neither.
        assert self._check("Bob Gale, Cam and Dan Ford were beaten to King.") == []
        assert self._check("Bob Gale, Cam Hart and Dan Ford were beaten to King.") == [
            "one paragraph names 3 of the 4 managers beaten to King",
        ]

    def test_a_manager_named_twice_counts_once(self):
        assert self._check("Bob Gale, Gale again and Bob Gale a third time, then Cam Hart, lost King.") == []

    def test_the_winner_named_beside_two_losers_passes(self):
        # Three managers named, two of them beaten: the winner never counts.
        assert self._check("Alice Moss beat Bob Gale and Cam Hart to King.") == []

    def test_a_loser_named_for_an_unrelated_reason_still_counts_as_the_accepted_cost(self):
        """A paragraph that names three beaten managers reads as a roll call to
        a reader whatever the reason, so this is not a false alarm to avoid."""
        assert self._check(
            "Alice Moss beat Bob Gale to King. Cam Hart posted the week's worst captain, "
            "and Dan Ford took the biggest hit."
        ) == ["one paragraph names 3 of the 4 managers beaten to King"]

    def test_a_name_read_inside_a_longer_one_does_not_count(self):
        """The third beaten manager's surname is only found inside a hyphenated
        player, which is not a mention of her (#379)."""
        managers = [
            _make_manager_with_txns("Alice Moss", [_txn("King", 6, "A", 1)], entry_id=1),
            _lost_by(_make_manager(name="Bob Gale", entry_id=2), _lost("King", "B")),
            _lost_by(_make_manager(name="Cam Hart", entry_id=3), _lost("King", "C")),
            _lost_by(_make_manager(name="Dan White", entry_id=4), _lost("King", "D")),
        ]
        managers[0]["squad"] = [_make_squad_player(name="Gibbs-White")]
        summary = "Gibbs-White did nothing for Alice Moss, who beat Bob Gale and Cam Hart to King."
        assert self._check(summary, managers) == []
        assert self._check(f"{summary} Dan White was beaten too.", managers) == [
            "one paragraph names 3 of the 3 managers beaten to King",
        ]

    def test_a_short_form_that_is_also_a_player_is_not_read_as_the_manager(self):
        managers = _roll_call_week()
        managers[0]["squad"] = [_make_squad_player(name="Ford")]
        assert self._check("Ford fired in for Alice Moss; Bob Gale and Cam Hart missed out on King.", managers) == []

    def test_beaten_managers_of_different_races_do_not_add_up(self):
        managers = [
            _make_manager_with_txns("Alice Moss", [_txn("King", 6, "A", 1)], entry_id=1),
            _make_manager_with_txns("Fay Wood", [_txn("Bogle", 4, "F", 1)], entry_id=2),
            _lost_by(_make_manager(name="Bob Gale", entry_id=3), _lost("King", "B")),
            _lost_by(_make_manager(name="Cam Hart", entry_id=4), _lost("King", "C")),
            _lost_by(_make_manager(name="Dan Ford", entry_id=5), _lost("Bogle", "D")),
            _lost_by(_make_manager(name="Eve Ray", entry_id=6), _lost("Bogle", "E")),
        ]
        assert self._check("Bob Gale and Cam Hart lost King; Dan Ford and Eve Ray lost Bogle.", managers) == []

    def test_each_race_is_counted_against_its_own_beaten_managers(self):
        managers = [
            _make_manager_with_txns("Alice Moss", [_txn("King", 6, "A", 1)], entry_id=1),
            _make_manager_with_txns("Fay Wood", [_txn("Bogle", 4, "F", 1)], entry_id=2),
            _lost_by(_make_manager(name="Bob Gale", entry_id=3), _lost("King", "B"), _lost("Bogle", "B2")),
            _lost_by(_make_manager(name="Cam Hart", entry_id=4), _lost("King", "C"), _lost("Bogle", "C2")),
            _lost_by(_make_manager(name="Dan Ford", entry_id=5), _lost("King", "D"), _lost("Bogle", "D2")),
        ]
        # Level on claimants and priorities, the races rank by player name.
        assert self._check("Bob Gale, Cam Hart and Dan Ford lost both King and Bogle.", managers) == [
            "one paragraph names 3 of the 3 managers beaten to Bogle",
            "one paragraph names 3 of the 3 managers beaten to King",
        ]

    def test_classic_data_is_inert(self):
        """Classic has no claims, hence no races: no format switch needed."""
        managers = [
            _make_manager(name="Alice Moss", entry_id=1),
            _make_manager(name="Bob Gale", entry_id=2),
            _make_manager(name="Cam Hart", entry_id=3),
            _make_manager(name="Dan Ford", entry_id=4),
        ]
        data = _make_recap_data(managers=managers)
        assert data["fpl_format"] == "classic"
        assert check_recap_editorial("Bob Gale, Cam Hart and Dan Ford all had a week to forget.", data) == {}
        assert self._check("Bob Gale, Cam Hart and Dan Ford all had a week to forget.", managers) == []

    def test_nothing_is_checked_without_a_contested_race(self):
        names = RecapNames.of(_roll_call_week())
        assert check_contested_roll_calls("Bob Gale, Cam Hart and Dan Ford.", [], names) == []

    def test_a_race_nobody_won_is_still_a_list_of_the_beaten(self):
        managers = [
            _lost_by(_make_manager(name="Bob Gale", entry_id=2), _lost("King", "B")),
            _lost_by(_make_manager(name="Cam Hart", entry_id=3), _lost("King", "C")),
            _lost_by(_make_manager(name="Dan Ford", entry_id=4), _lost("King", "D")),
        ]
        assert contested_draft_claims(managers)[0]["winner"] is None
        assert self._check("Bob Gale, Cam Hart and Dan Ford all wanted King.", managers) == [
            "one paragraph names 3 of the 3 managers beaten to King",
        ]

    def test_it_answers_under_its_own_code_beside_the_other_checks(self):
        data = _make_recap_data(managers=_roll_call_week())
        data["fpl_format"] = "draft"
        found = check_recap_editorial("Alice Moss took King; Bob Gale, Cam Hart and Dan Ford missed out.", data)
        assert found == {
            RECAP_WARNING_CONTESTED_ROLL_CALL: ["one paragraph names 3 of the 4 managers beaten to King"],
        }

    def test_the_warning_message_opens_with_the_lead_and_states_the_count(self):
        assert editorial_warning_message(
            RECAP_WARNING_CONTESTED_ROLL_CALL, ["one paragraph names 5 of the 5 managers beaten to King"],
        ) == (
            "The editorial lists the managers beaten to one player. "
            "One paragraph names 5 of the 5 managers beaten to King."
        )


class TestBrokenBriefIsToldApartFromContradiction:
    """Issue #459: the roll call is the first problem that is a true sentence,
    so "contradicted the data it was given" would be a lie told to the retry,
    to the reader on stderr and in the saved report."""

    _ROLL = "one paragraph names 5 of the 5 managers beaten to King"
    _WRONG = "the editorial says Bob was beaten to Giles"

    def test_only_the_roll_call_and_the_repeated_forfeit_are_registered_as_broken_briefs(self):
        assert {check.code for check in EDITORIAL_CHECKS if check.broken_brief} == {
            RECAP_WARNING_CONTESTED_ROLL_CALL, RECAP_WARNING_PENALTY_REPEATED,
        }

    def test_the_retry_prompt_for_a_roll_call_carries_the_instruction_and_a_header_that_fits(self):
        prompt = get_recap_editorial_retry_prompt("ORIGINAL", {RECAP_WARNING_CONTESTED_ROLL_CALL: [self._ROLL]})

        assert prompt.startswith("ORIGINAL\n\nYour previous draft broke the brief it was given:\n")
        assert f"- {self._ROLL}\n" in prompt
        assert "contradicted the data" not in prompt
        assert "A race needs only its count and its winner" in prompt
        assert "name at most two of the managers beaten to any one player" in prompt
        assert "only where the loss is their story" in prompt
        assert prompt.endswith("never as a list.")

    def test_a_contradiction_alone_keeps_the_header_it_always_had(self):
        prompt = get_recap_editorial_retry_prompt("ORIGINAL", {RECAP_WARNING_CONTESTED_MISATTRIBUTION: [self._WRONG]})

        assert f"Your previous draft contradicted the data it was given:\n- {self._WRONG}\n" in prompt
        assert "broke the brief" not in prompt
        assert "name at most two" not in prompt

    def test_a_draft_with_both_gets_a_header_for_each_over_its_own_lines(self):
        prompt = get_recap_editorial_retry_prompt(
            "ORIGINAL",
            {RECAP_WARNING_CONTESTED_MISATTRIBUTION: [self._WRONG], RECAP_WARNING_CONTESTED_ROLL_CALL: [self._ROLL]},
        )

        contradicted = prompt.index("Your previous draft contradicted the data it was given:")
        broke = prompt.index("Your previous draft broke the brief it was given:")
        assert contradicted < prompt.index(f"- {self._WRONG}") < broke < prompt.index(f"- {self._ROLL}")
        assert '"respectively"' in prompt
        assert "name at most two" in prompt

    def test_a_cut_off_roll_call_is_told_both_without_the_word_contradicted(self):
        prompt = get_recap_editorial_retry_prompt(
            "ORIGINAL", {RECAP_WARNING_CONTESTED_ROLL_CALL: [self._ROLL]}, stop_reason="max_tokens",
        )

        assert "cut off before it finished" in prompt
        assert "Your previous draft broke the brief it was given:" in prompt
        assert "contradicted" not in prompt

    def test_a_code_the_registry_does_not_know_is_read_as_a_contradiction(self):
        contradicted, broken = split_recap_problems({"synthesis_from_a_newer_version": [self._WRONG]})

        assert (contradicted, broken) == ([self._WRONG], [])

    def test_the_flaw_names_what_is_wrong_in_the_words_it_fits(self):
        assert recap_editorial_flaw({}) is None
        assert recap_editorial_flaw({RECAP_WARNING_CONTESTED_MISATTRIBUTION: [self._WRONG]}) == (
            "contradicts the data it was given"
        )
        assert recap_editorial_flaw({RECAP_WARNING_CONTESTED_ROLL_CALL: [self._ROLL]}) == (
            "breaks the brief it was given"
        )
        assert recap_editorial_flaw({
            RECAP_WARNING_CONTESTED_MISATTRIBUTION: [self._WRONG], RECAP_WARNING_CONTESTED_ROLL_CALL: [self._ROLL],
        }) == "contradicts the data and breaks the brief it was given"


# ---------------------------------------------------------------------------
# Net figure and tie checks (issue #359)
# ---------------------------------------------------------------------------


def _reported_transfers() -> list[RecapManagerEntry]:
    """The reported gameweek's shape: Alice Lowe made two transfers on a -4
    hit -- Gibbs-White in for Wirtz (-4) and Ajayi in for Cash (+2), so -2
    across both and -6 net overall -- and Bob Gale made one, with no hit."""
    return [
        _make_manager(
            name="Alice Lowe", entry_id=1, gw_points=30, transfer_cost=4, transfers_made=2,
            transfers=[
                _make_transfer("Gibbs-White", "Wirtz", 2, 6, cost=4),
                _make_transfer("Ajayi", "Cash", 1, -1, cost=4),
            ],
        ),
        _make_manager(
            name="Bob Gale", entry_id=2, gw_points=29, transfers_made=1,
            transfers=[_make_transfer("Wood", "Watkins", 9, 3)],
        ),
        _make_manager(name="Cam Hart", entry_id=3, gw_points=43, transfers_made=0, transfers=[]),
    ]


class TestNetAttributionCheck:
    """Issue #359: the editorial pinned a manager's net for the whole
    gameweek on one named transfer -- "brought in Gibbs-White for Wirtz and
    watched it backfire to the tune of -6 net", where that move was -4 and
    the -6 only exists with the second transfer and the hit."""

    def _check(self, summary: str, managers: list[RecapManagerEntry] | None = None) -> list[str]:
        managers = managers or _reported_transfers()
        return check_net_attributions(summary, managers, RecapNames.of(managers))

    def test_the_reported_sentence_is_caught(self):
        problems = self._check(
            "Contrast that with Alice Lowe, who paid a 4-point hit to bring in Gibbs-White for "
            "Wirtz and watched it backfire to the tune of -6 net."
        )
        assert problems == [
            'the editorial says "-6 net" of Gibbs-White in for Wirtz, but -6 is Alice Lowe\'s net '
            "for the whole gameweek (2 transfers, -4 hit), not that move's own swing of -4"
        ]

    @pytest.mark.parametrize("bystander, squad_player, summary", [
        # The tail of the move's own player...
        ("Dee White", None,
         "Alice Lowe paid a 4-point hit to bring in Gibbs-White for Wirtz and watched it backfire "
         "to the tune of -6 net."),
        # ...or of any other player the data carries that the clause names.
        ("Dee Lewin", "Calvert-Lewin",
         "Alice Lowe brought in Gibbs-White for Wirtz and watched it sink to -6 net as Calvert-Lewin blanked."),
    ])
    def test_a_bystander_surnamed_like_a_hyphenated_player_does_not_hide_it(self, bystander, squad_player, summary):
        """Issue #379: a manager who made no move and is not in the sentence,
        surnamed like the tail of "Gibbs-White", was read inside the player's
        name as a second manager named, so the check stood down on the very
        sentence it was written for."""
        managers = [*_reported_transfers(), _make_manager(name=bystander, entry_id=4, transfers_made=0, transfers=[])]
        if squad_player:
            managers[-1]["squad"] = [_make_squad_player(name=squad_player)]
        problems = self._check(summary, managers)
        assert len(problems) == 1
        assert "-6 is Alice Lowe's net for the whole gameweek" in problems[0]

    @pytest.mark.parametrize("summary", [
        "Gibbs-White in for Wirtz went -4 net.",
        "Alice Lowe brought in Gibbs-White for Wirtz and finished -6 net overall.",
        "Alice Lowe's transfers went -6 net, Gibbs-White for Wirtz the worst of them.",
        "Gibbs-White for Wirtz and Ajayi for Cash left Alice Lowe -6 net.",
        "Alice Lowe ended the week -6 net after bringing in Gibbs-White for Wirtz.",
        "Gibbs-White for Wirtz went -4; Alice Lowe was -6 net once the hit landed.",
        "Alice Lowe brought in Gibbs-White for Wirtz, -6 across both transfers after the hit.",
        # PR #374 review: the manager named between the move and the figure,
        # the move's own swing stated beside it, or the roster line's own
        # "after the hit" makes the figure the manager's.
        "Gibbs-White for Wirtz went -4, and Alice Lowe was -6 net once the hit landed.",
        "Gibbs-White for Wirtz cost -4 and left Alice Lowe -6 net after the hit.",
        "Alice Lowe lost 6 net pts, her worst move being Gibbs-White in for Wirtz (-4).",
        "Alice Lowe took a hit to bring in Gibbs-White for Wirtz and ended -6 net after the hit.",
        "Gibbs-White for Wirtz (-4) dragged Alice Lowe down to -6 net.",
        # Issue #499: "a -4 move" is the move's swing, not the slang for a
        # hit, and the hit beside the figure may give its size.
        "Alice Lowe swapped Wirtz for Gibbs-White, a -4 move, and netted -6 after a 4-point hit.",
        "Alice Lowe swapped Wirtz for Gibbs-White, a -4 move, and netted -6.",
        "Alice Lowe swapped Wirtz for Gibbs-White and netted -6 after a 4-point hit.",
        "Alice Lowe swapped Wirtz for Gibbs-White and netted -6 after a -4 hit.",
        "Alice Lowe swapped Wirtz for Gibbs-White and netted -6 after a four-point hit.",
    ])
    def test_a_figure_the_clause_gives_its_own_subject_passes(self, summary):
        assert self._check(summary) == []

    @pytest.mark.parametrize("summary", [
        # The hit taken to make the move says nothing about whose the figure is.
        "Alice Lowe paid a hit to bring in Gibbs-White for Wirtz and watched it sink to -6 net.",
        # "A -4" is the hit in FPL slang, not the move's swing, even when they match.
        "Alice Lowe took a -4 to bring in Gibbs-White for Wirtz and watched it backfire to the tune of -6 net.",
        # PR #500 review: and with a verb of paying before it, a noun for a
        # move after it does not make it the move's.
        "Alice Lowe took a -4 swap to bring in Gibbs-White for Wirtz and watched it backfire to the tune of -6 net.",
        # A figure ahead of everything leads into the move it names.
        "A -6 net swing on Gibbs-White for Wirtz sank Alice Lowe.",
    ])
    def test_a_figure_still_pinned_on_the_move_is_caught(self, summary):
        problems = self._check(summary)
        assert len(problems) == 1
        assert "-6 is Alice Lowe's net for the whole gameweek" in problems[0]

    def test_another_move_s_swing_is_caught_and_named(self):
        problems = self._check("Gibbs-White in for Wirtz, +2 net, was a disaster.")
        # "+2" is read as the move that went +2, not the -2 the two came to.
        assert len(problems) == 1
        assert "+2 is the swing on Ajayi in for Cash" in problems[0]

    def test_the_swing_before_the_hit_is_caught(self):
        problems = self._check("Ajayi for Cash came out at a net -2.")
        assert len(problems) == 1
        assert "-2 is Alice Lowe's swing across all 2 transfers before the hit" in problems[0]

    @pytest.mark.parametrize("summary", [
        # Another manager's net may be the figure meant.
        "Unlike Cam Hart, Alice Lowe brought in Gibbs-White for Wirtz and went -6 net.",
        # A figure that is nobody's on the line is not this check's to place.
        "Gibbs-White for Wirtz went -9 net.",
        # Not a net figure at all.
        "Gibbs-White for Wirtz cost Alice Lowe a 4-point hit and 6 places.",
        "Gibbs-White for Wirtz gave Alice Lowe a net gain of 2 places.",
    ])
    def test_a_clause_it_cannot_pin_down_is_left_alone(self, summary):
        assert self._check(summary) == []

    def test_an_unsigned_figure_matches_the_move_whichever_way_round(self):
        # "Lost 2 net" on a +2 move is loose, not wrong in a way the data shows.
        assert self._check("Ajayi for Cash lost Alice Lowe 2 net points.") == []

    def test_a_line_with_one_move_and_no_hit_has_nothing_to_misplace(self):
        assert self._check("Wood in for Watkins went +6 net.") == []

    def test_a_draft_waiver_line_is_held_to_the_same_rule(self):
        managers = [
            _make_manager_with_txns("Dan Ford", [_txn("King", 9, "Dud", 1), _txn("Bogle", 1, "Wood", 5)]),
            _make_manager(name="Eve Ray", entry_id=2),
        ]
        problems = self._check("Dan Ford's swoop for King in place of Dud paid off, +4 net.", managers)
        assert len(problems) == 1
        assert "+4 is Dan Ford's net for the whole gameweek (2 moves)" in problems[0]

    def test_the_same_move_by_two_managers_is_read_only_when_the_clause_says_whose(self):
        managers = _reported_transfers()
        managers[2]["transfers"] = [_make_transfer("Gibbs-White", "Wirtz", 2, 6)]
        managers[2]["transfers_made"] = 1
        # Cam made the move alone, with no hit, so "-6" is not his figure;
        # unnamed, the clause cannot say which manager's move it means.
        assert self._check("Gibbs-White for Wirtz went -6 net.", managers) == []
        problems = self._check("Alice Lowe's Gibbs-White for Wirtz went -6 net.", managers)
        assert len(problems) == 1
        assert "Alice Lowe's net for the whole gameweek" in problems[0]

    def test_a_partially_captured_line_has_no_net_to_misplace(self):
        managers = _reported_transfers()
        managers[0]["transfers_made"] = 3
        # The gameweek's net is unknown, so "-6" is no figure of the line's;
        # the swing across the captured moves still is.
        assert self._check("Gibbs-White for Wirtz went -6 net.", managers) == []
        problems = self._check("Gibbs-White for Wirtz went -2 net.", managers)
        assert len(problems) == 1
        assert "swing across the captured 2 transfers before the hit" in problems[0]

    def test_netted_is_a_net_figure(self):
        """Issue #390: "netted" is the verb the model reaches for, and the
        pattern keyed on "net" alone never read it."""
        problems = self._check("Gibbs-White in for Wirtz netted -6.")
        assert len(problems) == 1
        assert "-6 is Alice Lowe's net for the whole gameweek" in problems[0]

    def test_a_player_who_netted_goals_is_no_net_figure(self):
        # Unsigned after the verb is goals, not points: Ajayi's +2 is not
        # what "netted 2" said of Gibbs-White.
        assert self._check("Gibbs-White in for Wirtz netted 2 against Spurs.") == []
        assert self._check("Gibbs-White in for Wirtz netted Alice Lowe 2 points.") == []
        # PR #392 review: nor is "points" -- that is the player's score, not
        # the -6 the line's net overall happens to match.
        assert self._check("Alice Lowe swapped Wirtz for Gibbs-White, who netted 6 points.") == []

    def test_a_kind_s_net_said_of_one_of_its_moves_is_caught(self):
        """Issue #390: a line that mixes kinds now prints each kind's net, so
        that is another figure the line holds."""
        problems = self._check("King in for Dud went +10 net.", _mixed_waivers())
        assert len(problems) == 1
        assert "+10 is the net across Fay Lund's 2 waivers" in problems[0]


def _mixed_waivers() -> list[RecapManagerEntry]:
    """The reported gameweek's shape (issue #390): Dan Ford made one waiver
    (-1) and one free-agent pickup (+4), +3 overall, and lost a claim on
    Kostoulas; Eve Ray made two waivers (+8); Fay Lund made two waivers
    (+10) and a free-agent pickup (-3), +7 overall; Gus Hill made none."""
    dan = _make_manager_with_txns("Dan Ford", [
        _txn("Hinshelwood", 0, "M.Sangaré", 1),
        _txn("Manzambi", 5, "Yalcouyé", 1, kind="f"),
    ], entry_id=1)
    dan["lost_claims"] = [_lost("Kostoulas", "Gray")]
    return [
        dan,
        _make_manager_with_txns("Eve Ray", [_txn("Wood", 6, "Watkins", 1), _txn("Bogle", 4, "Lamptey", 1)], entry_id=2),
        _make_manager_with_txns("Fay Lund", [
            _txn("King", 9, "Dud", 1), _txn("Isak", 4, "Cash", 2), _txn("Mateta", 1, "Ings", 4, kind="f"),
        ], entry_id=3),
        _make_manager(name="Gus Hill", entry_id=4),
    ]


class TestKindNetScopeCheck:
    """Issue #390: the editorial said of a manager who made one waiver (-1)
    and one free-agent pickup (+4) that "his waiver moves netted +3
    overall" -- the two moves' combined net, hiding a losing claim behind
    the pickup's gain."""

    _LEAD = "Dan Ford had a week to forget. "

    def _check(self, summary: str, managers: list[RecapManagerEntry] | None = None) -> list[str]:
        managers = managers or _mixed_waivers()
        return check_kind_net_scopes(summary, managers, RecapNames.of(managers))

    def test_the_reported_sentence_is_caught(self):
        problems = self._check(
            self._LEAD + "His Kostoulas claim failed, and his waiver moves netted +3 overall, "
            "which did nothing to rescue him."
        )
        assert problems == [
            'the editorial says "waiver moves netted +3" of Dan Ford, but +3 is Dan Ford\'s net for the '
            "whole gameweek (2 moves: 1 waiver, 1 free agent), not the waivers' own net of -1"
        ]

    @pytest.mark.parametrize("summary", [
        "Dan Ford's waiver moves netted +3 overall.",
        # A short form the manager goes by.
        "Ford's waivers came to +3.",
        # The figure ahead of the kind it is said of.
        "Dan Ford netted +3 overall from his waivers.",
        "Dan Ford's free-agent pickups netted +3 overall.",
        # PR #392 review: a relative or participle clause, and the furniture
        # of a figure.
        "Dan Ford's waiver moves, netting +3 overall, did little.",
        "Dan Ford's waiver moves, which netted +3 overall, did little.",
        "Dan Ford's waiver moves netted a mere +3 overall.",
        "Dan Ford's waiver work paid off to the tune of +3.",
        # A determiner after a bare comma opens a new phrase, not a list.
        "Dan Ford made two moves, his waivers netting +3 overall.",
    ])
    def test_the_net_overall_said_of_one_kind_is_caught(self, summary):
        problems = self._check(summary)
        assert len(problems) == 1
        assert "+3 is Dan Ford's net for the whole gameweek (2 moves: 1 waiver, 1 free agent)" in problems[0]

    def test_the_other_kind_s_figure_is_caught_and_named(self):
        problems = self._check("Dan Ford's waivers went +4.")
        assert problems == [
            'the editorial says "waivers went +4" of Dan Ford, but +4 is the swing on Manzambi in for '
            "Yalcouyé [free agent], not the waivers' own net of -1"
        ]
        problems = self._check("Fay Lund's free agents netted +10.")
        assert len(problems) == 1
        assert "+10 is the net across Fay Lund's 2 waivers, not the free agents' own net of -3" in problems[0]

    @pytest.mark.parametrize("summary", [
        # Kind-neutral scopes.
        "Dan Ford's moves netted +3 overall.",
        "Dan Ford's claims netted +3 overall.",
        "Dan Ford's pickups netted +3 overall.",
        "Dan Ford's waiver-wire business netted +3 overall.",
        "Dan Ford went +3 overall on the waiver wire.",
        "Dan Ford's waiver and free-agent moves netted +3 overall.",
        "Dan Ford's waivers and free agents came to +3.",
        "Dan Ford went +3 overall across his waivers and free agents.",
        # PR #392 review: both kinds named with a determiner each.
        "Dan Ford's one waiver and one free agent netted +3 overall.",
        "Dan Ford netted +3 from a waiver and a free agent.",
        "Dan Ford's waiver and his free agent pickup combined for +3.",
        # ...and a kind trailing after a figure that already has its subject,
        # the phrasing the retry asks for.
        "Dan Ford's moves netted +3 overall with a free agent doing the heavy lifting.",
        "Dan Ford's moves netted +3 overall with a free agent carrying a losing waiver.",
        "Dan Ford netted +3 overall with a free agent doing the heavy lifting.",
        # A kind joined to a neutral noun names every move.
        "Dan Ford netted +3 overall from a waiver and a pickup.",
        "Dan Ford's moves netted +3 overall from a waiver and a pickup.",
        # Each kind its own figure.
        "Dan Ford's waivers netted -1, his free agents +4.",
        "Dan Ford's waiver went -1 and his free-agent pickup +4, +3 overall.",
        "Dan Ford's free-agent pickup came to +4.",
        # The kind and the overall net in the same clause, kept apart.
        "Dan Ford's one waiver lost a point (-1), and his moves netted +3 overall.",
        "Manzambi in for Yalcouyé as a free agent (+4) left Dan Ford's moves +3 net overall.",
        "Dan Ford's waiver flopped but he still netted +3 overall.",
        # A figure that is nobody's on the line.
        "Dan Ford's waiver moves netted +9.",
        # Unsigned after the verb is a player's score, not a swing.
        "Dan Ford's waiver claims netted him 3 points.",
        # A comma that opens no clause about the figure.
        "Dan Ford's waiver flopped, he netted +3 overall.",
        "Dan Ford had no luck on waivers, overall he went +3.",
        # One of the kind's own moves' swings.
        "Fay Lund's waivers netted +8.",
    ])
    def test_a_figure_the_data_bears_out_or_cannot_place_passes(self, summary):
        assert self._check(summary) == []

    def test_a_manager_whose_moves_are_one_kind_is_not_read(self):
        # Eve Ray's waivers are every move she made: their net is the net.
        assert self._check("Eve Ray's waiver moves netted +8 overall.") == []
        assert self._check("Eve Ray's waiver moves netted +3 overall.") == []

    @pytest.mark.parametrize("summary", [
        # Two managers in the clause: the figure could be either's.
        "Unlike Eve Ray, Dan Ford's waiver moves netted +3 overall.",
        # Two in the clause before: "his" could be either's.
        "Dan Ford beat Eve Ray to Isak. His waiver moves netted +3 overall.",
        # A new paragraph: "his" is nobody's the check can see.
        "Dan Ford had a week to forget.\n\nHis waiver moves netted +3 overall.",
        # Nobody named at all.
        "His waiver moves netted +3 overall.",
    ])
    def test_a_clause_whose_manager_is_unclear_is_left_alone(self, summary):
        assert self._check(summary) == []

    def test_the_nearest_manager_named_earlier_in_the_paragraph_is_the_one_read(self):
        summary = "Dan Ford had a week to forget. Gus Hill sat it out. His waiver moves netted +3 overall."
        # Gus Hill made no moves, so the clause is about nobody it can check.
        assert self._check(summary) == []
        summary = "Gus Hill sat it out. Dan Ford had a week to forget: his waiver moves netted +3 overall."
        assert len(self._check(summary)) == 1

    def test_nothing_is_read_when_nobody_mixed_kinds(self):
        managers = [m for m in _mixed_waivers() if m["manager_name"] in ("Eve Ray", "Gus Hill")]
        assert self._check("Eve Ray's waiver moves netted +3 overall.", managers) == []

    def _aside_line(self) -> list[RecapManagerEntry]:
        # A free agent (+6) and a waiver (+1), +7 overall.
        return [
            _make_manager_with_txns("Dan Ford", [
                _txn("Belloumi", 6, "Kluivert", 0, kind="f"),
                _txn("Dunk", 2, "Van Hecke", 1),
            ], entry_id=1),
            _make_manager(name="Gus Hill", entry_id=2),
        ]

    _ASIDE = (
        "Dan Ford had a busy week. Belloumi came in on a free agent for Kluivert and scored 6, "
        "and with Dunk's +1 on waivers {subject} finished +7"
    )

    @pytest.mark.parametrize("subject", ["he", "she", "they"])
    @pytest.mark.parametrize("closer", ["", " overall", " in all", " over the week", " this week", " for GW6"])
    def test_the_week_s_net_said_after_a_kind_aside_passes(self, subject, closer):
        """Issue #465: the waiver already has its figure (+1), so it is an
        aside, and the +7 after a new subject is the manager's whole week,
        however the sentence ends."""
        assert self._check(self._ASIDE.format(subject=subject) + closer + ".", self._aside_line()) == []

    @pytest.mark.parametrize("summary", [
        # The kind is the subject: its "overall" is the waivers' (#390).
        "Dan Ford's waiver moves netted +7 overall.",
        "Dan Ford's waivers made him +7.",
        "Dan Ford's waivers made him +7 overall.",
        # PR #468 review: a new subject with no aside figure is still the
        # waivers' figure, closer or not.
        "Dan Ford's waivers he netted +7 overall.",
        "Dan Ford on waivers he went +7 in total.",
        "Dan Ford's waiver moves he made were +7 overall.",
        # ...and a relative clause's pronoun opens no new subject.
        "Dan Ford's waivers, which he netted +7 overall, did little.",
        "Dan Ford's waivers, which they netted +7 for the week, did little.",
        # The aside's own figure with no new subject carries on to the kind.
        "Dan Ford's +1 on waivers left him +7.",
    ])
    def test_the_week_s_net_said_of_the_kind_is_still_caught(self, summary):
        problems = self._check(summary, self._aside_line())
        assert len(problems) == 1
        assert "+7 is Dan Ford's net for the whole gameweek (2 moves: 1 waiver, 1 free agent)" in problems[0]

    def test_a_kind_named_after_the_week_s_net_is_still_read(self):
        # The aside frees the figure from the waivers before it, not from a
        # kind it is then said to come from.
        problems = self._check(
            "Dan Ford had a busy week. With Dunk's +1 on waivers he finished +7 from his free agents.",
            self._aside_line(),
        )
        assert len(problems) == 1
        assert "not the free agents' own net of +6" in problems[0]

    def test_each_kind_s_own_net_beside_the_aside_line_passes(self):
        assert self._check("Dan Ford went +6 on the free agent and +1 on waivers.", self._aside_line()) == []

    def test_the_registry_runs_it_on_a_draft_editorial(self):
        data = _make_recap_data(managers=_mixed_waivers())
        data["fpl_format"] = "draft"
        found = check_recap_editorial("Dan Ford's waiver moves netted +3 overall.", data)
        assert list(found) == [RECAP_WARNING_KIND_NET_MISATTRIBUTION]
        prompt = get_recap_editorial_retry_prompt("ORIGINAL", found)
        assert "never their waivers' or their free agents'" in prompt
        assert "NEVER to one named move" not in prompt


def _reported_table() -> list[RecapManagerEntry]:
    """The reported gameweek's shape: the outright lowest score (29) belongs
    to a manager in a table whose 7th place is shared three ways -- the tie
    that got carried onto the wrong column -- and 43 is the one shared
    gameweek score."""
    rows = [
        ("Ann Moss", 29, 7, 6, 340), ("Bea Gale", 33, 7, 7, 340), ("Cy Hart", 35, 7, 9, 340),
        ("Dee Ford", 43, 1, 1, 400), ("Eli Ray", 43, 2, 2, 390), ("Fay Lund", 58, 10, 10, 300),
    ]
    return [
        _make_manager(
            name=name, entry_id=i, gw_points=pts, overall_rank=rank, previous_rank=prev, total_points=total,
        )
        for i, (name, pts, rank, prev, total) in enumerate(rows, start=1)
    ]


class TestTieClaimCheck:
    """Issue #359: the editorial called an outright lowest gameweek score
    "the joint-lowest score of the week", carrying a tie that was live in
    the table's season totals onto a score nobody shared."""

    def _check(self, summary: str, managers: list[RecapManagerEntry] | None = None) -> list[str]:
        managers = managers or _reported_table()
        return check_tie_claims(summary, managers, RecapNames.of(managers))

    def test_the_reported_sentence_is_caught(self):
        problems = self._check("The previous leader posted the joint-lowest score of the week (29 pts).")
        assert problems == [
            'the editorial says "joint-lowest", but the lowest gameweek score, 29 pts, was Ann Moss\'s alone'
        ]

    @pytest.mark.parametrize("summary", [
        "Ann Moss posted the lowest score of the week.",
        "Ann Moss, Bea Gale and Cy Hart sit joint-seventh in the table.",
        "Ann Moss is now tied for 7th place.",
        # The table carries the tie on its Prev column.
        "Dee Ford, joint-top of the table last week, now leads alone.",
    ])
    def test_a_tie_the_data_states_passes(self, summary):
        managers = _reported_table()
        managers[4]["previous_rank"] = 1
        assert self._check(summary, managers) == []

    def test_a_shared_gameweek_score_bears_out_a_joint_highest(self):
        managers = _reported_table()
        managers[5]["gw_points"] = 43
        managers[4]["gw_points"] = 58
        managers[3]["gw_points"] = 58
        assert self._check("Dee Ford posted the joint-highest score of the week.", managers) == []
        problems = self._check("Fay Lund and Ann Moss were joint-lowest this week.", managers)
        assert len(problems) == 1

    def test_a_tie_in_the_table_is_not_a_tie_on_gameweek_scores(self):
        problems = self._check("Ann Moss finished joint-bottom of the gameweek scores.")
        assert len(problems) == 1
        assert "the lowest gameweek score, 29 pts, was Ann Moss's alone" in problems[0]

    def test_a_position_nobody_or_one_manager_holds_is_caught(self):
        assert self._check("Fay Lund sits joint-eighth in the table.") == [
            'the editorial says "joint-eighth", but nobody is 8th in the table'
        ]
        problems = self._check("Fay Lund is joint-tenth in the standings.")
        assert problems == [
            'the editorial says "joint-tenth", but 10th place in the table is Fay Lund\'s alone'
        ]

    def test_a_shared_season_record_bears_out_a_season_tie(self):
        """PR #495 review: the season-record line can state a shared record,
        and quoting it is no unsupported tie, though the week's highest score
        and the table's top spot are each one manager's."""
        summary = "Dee Ford's 43 was the joint-highest score of the season, level with Eli Ray's GW4."
        managers = _reported_table()
        shared = RecapSeasonRecord(points=43, scores=[("Eli Ray", 4), ("Dee Ford", 7)])

        assert check_tie_claims(
            summary, managers, RecapNames.of(managers), season_records={"top": shared},
        ) == []

    def test_a_season_record_scored_once_is_caught_on_all_three_readings(self):
        summary = "Dee Ford's 43 was the joint-highest score of the season."
        managers = _reported_table()
        alone = RecapSeasonRecord(points=72, scores=[("Eli Ray", 4)])

        assert check_tie_claims(
            summary, managers, RecapNames.of(managers), season_records={"top": alone},
        ) == [
            'the editorial says "joint-highest", but no reading holds: the highest gameweek score, '
            "58 pts, was Fay Lund's alone; 1st place in the table is Dee Ford's alone; the season's "
            "highest gameweek score, 72 pts, was Eli Ray's alone, in GW4"
        ]

    def test_a_season_record_never_answers_for_a_week_or_the_table(self):
        """The record is read only where the tie is said of the season."""
        managers = _reported_table()
        shared = RecapSeasonRecord(points=29, scores=[("Ann Moss", 2), ("Ann Moss", 7)])

        problems = check_tie_claims(
            "The previous leader posted the joint-lowest score of the week (29 pts).",
            managers, RecapNames.of(managers), season_records={"bottom": shared},
        )

        assert len(problems) == 1

    @pytest.mark.parametrize("summary", [
        "Fay Lund's 58 was the joint-highest score of the season.",
        "Fay Lund hit 58, the joint-highest score of the season.",
        "Fay Lund's 58 points is now the joint-highest score of the season.",
        "The joint-highest score of the season is 58, and it is Fay Lund's.",
    ])
    def test_a_shared_season_record_never_answers_for_another_score(self, summary):
        """PR #495 A/B: a 72 scored twice let "65 was the joint-highest score
        of the season" pass, the season superlative #494 is about."""
        managers = _reported_table()
        shared = RecapSeasonRecord(points=72, scores=[("Eli Ray", 2), ("Bea Gale", 4)])

        problems = check_tie_claims(summary, managers, RecapNames.of(managers), season_records={"top": shared})

        assert problems == [
            'the editorial says "joint-highest", but no reading holds: the highest gameweek score, '
            "58 pts, was Fay Lund's alone; 1st place in the table is Dee Ford's alone; the season's "
            "highest gameweek score is 72 pts, not 58 pts"
        ]

    @pytest.mark.parametrize("summary", [
        "Eli Ray's 72 was the joint-highest score of the season.",
        "Eli Ray's 72 in GW2 was the joint-highest score of the season.",
        "Eli Ray holds the joint-highest score of the season.",
        "The joint-highest score of the season is 72.",
        # A figure the tie is not said of.
        "Fay Lund's 58, short of the joint-highest score of the season, won the week.",
        "Fay Lund's 58 fell 14 short of the joint-highest score of the season.",
        "Bea Gale's score in gameweek 4 was the joint-highest of the season.",
    ])
    def test_a_shared_season_record_bears_out_its_own_score(self, summary):
        managers = _reported_table()
        shared = RecapSeasonRecord(points=72, scores=[("Eli Ray", 2), ("Bea Gale", 4)])

        assert check_tie_claims(summary, managers, RecapNames.of(managers), season_records={"top": shared}) == []

    def test_a_shared_season_low_never_answers_for_another_score(self):
        managers = _reported_table()
        shared = RecapSeasonRecord(points=21, scores=[("Cy Hart", 3), ("Ann Moss", 5)])

        problems = check_tie_claims(
            "Ann Moss's 29 was the joint-lowest score of the season.",
            managers, RecapNames.of(managers), season_records={"bottom": shared},
        )

        assert len(problems) == 1
        assert problems[0].endswith("the season's lowest gameweek score is 21 pts, not 29 pts")

    def test_the_registry_reads_the_records_off_the_recap_data(self):
        managers = _reported_table()
        data = _make_recap_data(managers=managers)
        data["season_highest_score"] = RecapSeasonRecord(points=43, scores=[("Eli Ray", 4), ("Dee Ford", 7)])

        found = check_recap_editorial("Dee Ford's 43 was the joint-highest score of the season.", data)

        assert RECAP_WARNING_UNSUPPORTED_TIE not in found

    def test_a_tie_with_no_subject_needs_both_readings_to_fail(self):
        # Last place in the table is Fay Lund's alone too, so neither holds.
        problems = self._check("Ann Moss finished joint-bottom.")
        assert len(problems) == 1
        assert problems[0].startswith('the editorial says "joint-bottom", but neither reading holds')
        managers = _reported_table()
        managers[0]["overall_rank"] = 10
        assert self._check("Ann Moss finished joint-bottom.", managers) == []

    @pytest.mark.parametrize("summary", [
        "Ann Moss was not joint-lowest this week.",
        "Ann Moss's captain was the joint-lowest scoring armband.",
        "Salah was joint-top scorer of the week.",
        "Ann Moss's bench was the joint-lowest.",
        "Bea Gale posted the joint-second lowest score of the week.",
        "Ann Moss did their level best to finish bottom.",
        # PR #374 review: a word that is not a rank where it stands.
        "Ann Moss and Bea Gale were tied last week at the foot of the table.",
        "Ann Moss and Bea Gale shared last week's wooden spoon.",
        "Ann Moss and Bea Gale shared first blood this week.",
        "Ann Moss and Bea Gale shared the best fixture.",
        # ...and a negation further back than the word before.
        "Ann Moss never really posted the joint-lowest score.",
    ])
    def test_a_tie_on_something_else_is_left_alone(self, summary):
        managers = _reported_table()
        managers[0]["squad"] = [_make_squad_player(name="Salah")]
        assert self._check(summary, managers) == []

    @pytest.mark.parametrize("summary", [
        "Ann Moss's captain blanked and the transfers flopped, so they posted the joint-lowest score of the week.",
        # PR #374 review: a player named in the explanation is not what the
        # tie is about, so he no longer hides it.
        "Ann Moss posted the joint-lowest score of the week as Haaland blanked.",
        "Ann Moss posted the joint-lowest score of the week, with Haaland to blame.",
    ])
    def test_a_subject_elsewhere_in_the_clause_does_not_hide_the_tie(self, summary):
        managers = _reported_table()
        managers[0]["squad"] = [_make_squad_player(name="Haaland")]
        assert len(self._check(summary, managers)) == 1

    def test_a_manager_whose_name_holds_a_players_is_still_the_subject(self):
        """Issue #379, the other way round: the player "White" was read inside
        "Ann White", so a tie said of the manager passed as one said of a
        player. The subject window reaches back only to "White" here, so the
        name has to be read across the whole clause, not inside the window."""
        managers = _reported_table()
        managers[0]["manager_name"] = "Ann White"
        managers[1]["squad"] = [_make_squad_player(name="White")]
        assert self._check("Ann White posted the joint-lowest score of the week.", managers) == [
            'the editorial says "joint-lowest", but the lowest gameweek score, 29 pts, was Ann White\'s alone'
        ]
        # "White" alone is the player's name before it is the manager's.
        assert self._check("White posted the joint-lowest score of the week.", managers) == []

    def test_a_player_only_partly_inside_the_subject_window_does_not_hide_the_tie(self):
        """PR #380 review: the window reaches two words past the tie, to "Mac",
        so "Mac Allister" only starts inside it. A player is the subject only
        when his whole name is in the window, as it was before #379."""
        managers = _reported_table()
        managers[1]["squad"] = [_make_squad_player(name="Mac Allister")]
        assert len(self._check("Ann Moss finished joint-lowest after Mac Allister blanked.", managers)) == 1

    def test_level_totals_the_tie_break_split_bear_out_a_tie_at_the_top(self):
        """PR #374 review: a head-to-head draft table ranks managers level on
        league points apart on points scored, so Pos reads 1/2 while Total
        reads 21/21 -- the tie the Total column shows is one the data states."""
        managers = [
            _make_manager(name="Ann Moss", entry_id=1, gw_points=50, overall_rank=1, previous_rank=None, total_points=21),
            _make_manager(name="Bea Gale", entry_id=2, gw_points=40, overall_rank=2, previous_rank=None, total_points=21),
            _make_manager(name="Cy Hart", entry_id=3, gw_points=30, overall_rank=3, previous_rank=None, total_points=18),
        ]
        assert self._check("Ann Moss and Bea Gale are level at the top of the table on 21 points.", managers) == []
        problems = self._check("Cy Hart is joint-bottom of the table.", managers)
        assert problems == ['the editorial says "joint-bottom", but 3rd place in the table is Cy Hart\'s alone']

    def test_a_lone_manager_has_nothing_to_tie_with(self):
        assert self._check("The joint-lowest score of the week.", _reported_table()[:1]) == []


class TestCompetitionRank:
    """Issue #486: one definition of a recap rank, read by the GW Rank column,
    the report's standings block, the tie check and the placing check."""

    def test_level_values_share_a_rank_and_the_next_one_down_skips_it(self):
        values = [70, 62, 62, 50]
        assert [competition_rank(value, values) for value in values] == [1, 2, 2, 4]


def _placings_table() -> list[RecapManagerEntry]:
    """#483's shape, in league order: the last row (Hal Penn, 8th) scored 31,
    seventh on the week and one clear of Gus Peel's 30, and the week's
    runner-up (Bea Gale, 62) is third in the table, the placing the editorial
    gave her on the week. The week's top scorer (Cy Hart, 70) is second."""
    rows = [
        ("Ann Moss", 39, 382), ("Cy Hart", 70, 340), ("Bea Gale", 62, 330), ("Dee Ford", 45, 290),
        ("Eli Ray", 41, 280), ("Fay Lund", 50, 270), ("Gus Peel", 30, 256), ("Hal Penn", 31, 252),
    ]
    return [
        _make_manager(
            name=name, entry_id=pos, gw_points=pts, overall_rank=pos, previous_rank=pos, total_points=total,
        )
        for pos, (name, pts, total) in enumerate(rows, start=1)
    ]


class TestGameweekPlacingCheck:
    """Issue #486: the editorial called a manager seventh on the week "eighth
    on 31", their league position (#483), and with the prompt's table
    carrying a GW Rank column it still called the week's runner-up "third on
    the week" (#488). Every manager's gameweek placing is known, so a placing
    said of the week is checked against it."""

    def _check(self, summary: str, managers: list[RecapManagerEntry] | None = None) -> list[str]:
        managers = managers or _placings_table()
        return check_gameweek_placings(summary, managers, RecapNames.of(managers))

    def test_a_league_position_beside_a_gameweek_score_is_caught(self):
        assert self._check("Hal Penn finished eighth on 31.") == [
            'the editorial places Hal Penn "eighth" beside their gameweek score, but Hal Penn\'s 31 pts was '
            "7th on the week; 8th is Hal Penn's place in the table"
        ]

    def test_the_week_s_runner_up_called_third_is_caught(self):
        assert self._check("Cy Hart won the week, and Bea Gale was third on the week with 62.") == [
            'the editorial places Bea Gale "third" for the week, but Bea Gale\'s 62 pts was 2nd on the week; '
            "3rd is Bea Gale's place in the table"
        ]

    def test_last_and_top_name_who_holds_the_week_s_extreme(self):
        assert self._check("Ann Moss came last on the week with 39.") == [
            'the editorial places Ann Moss "last" for the week, but Ann Moss\'s 39 pts was 6th on the week; '
            "the lowest gameweek score was 30 pts, held by Gus Peel"
        ]
        assert self._check("Ann Moss was top of the gameweek.") == [
            'the editorial places Ann Moss "top" for the week, but Ann Moss\'s 39 pts was 6th on the week; '
            "the highest gameweek score was 70 pts, held by Cy Hart, and Ann Moss is top of the table"
        ]

    @pytest.mark.parametrize(("summary", "placed"), [
        ("Bea Gale's 62 was the third-best score of the week.", "Bea Gale"),
        ("Fay Lund took a second-place finish in the week.", "Fay Lund"),
        ("Dee Ford put up 45 points - fifth in the gameweek.", "Dee Ford"),
        # "This week" only dates a placing, but the score beside it is the week's.
        ("Hal Penn finished eighth this week on 31.", "Hal Penn"),
        ("Bea Gale's 62 was this week's third-best.", "Bea Gale"),
        ("Ann Moss was rock-bottom of the gameweek.", "Ann Moss"),
        ("Hal Penn finished joint-last on 31.", "Hal Penn"),
        ("Eli Ray took 8th place for the week with 41.", "Eli Ray"),
        # The week named straight after the placing outweighs a move up the
        # table in the same breath: said of the week's top scorer, whose
        # league position it was.
        ("Cy Hart rocketed up five places to snatch second on the week with 70.", "Cy Hart"),
        # A pronoun goes to the one manager the clause before it named, the
        # object as much as the subject.
        ("Ann Moss had a shocker. She finished seventh on the week.", "Ann Moss"),
        ("Ann Moss had a shocker; her 39 was only seventh-best on the week.", "Ann Moss"),
        ("Ann Moss had a shocker. It only bought her a seventh-place finish in the week.", "Ann Moss"),
        # PR #489 review: a finishing verb and the manager's own score in the
        # clause are #483's shape, whatever comma or "and" sits between them.
        ("Hal Penn scored 31 and finished eighth.", "Hal Penn"),
        ("Hal Penn scored 31, finishing eighth.", "Hal Penn"),
        ("Hal Penn managed just 31, good for eighth.", "Hal Penn"),
        ("With 31 points, Hal Penn finished eighth.", "Hal Penn"),
        ("Dee Ford scored 45 for 5th place.", "Dee Ford"),
        # ...and "could" hedges only with "have", "no better than" is a finish,
        # and a hedge in another stretch of the clause is not this one's.
        ("Hal Penn could only manage eighth on 31.", "Hal Penn"),
        ("Hal Penn could do no better than eighth on the week with 31.", "Hal Penn"),
        ("Gus Peel beat Dee Ford, and Hal Penn finished eighth on 31.", "Hal Penn"),
        # The drafts replay: a holding verb before the placing gives way to
        # the week named after it, another subject counts only in the
        # placing's own stretch, and a negation on another verb does not
        # reach past the finishing verb.
        ("Fay Lund sits 6th on 50 for the week.", "Fay Lund"),
        ("Fay Lund made no moves and submitted no claims, finishing 6th on 50 for the week.", "Fay Lund"),
        (
            "Fay Lund made no moves and submitted no claims, finished 6th on 50, and was the only manager who sat"
            " the waiver wire out entirely.",
            "Fay Lund",
        ),
        ("Hal Penn didn't escape, finishing 8th on 31 with his Wildcard.", "Hal Penn"),
        ("Gus Peel fell two places to 7th, and Hal Penn finished 8th on 31 after his Wildcard.", "Hal Penn"),
        # Issue #499: "out of" leaves a placing only straight before it.
        ("Hal Penn ran out of steam and finished last on the week.", "Hal Penn"),
    ])
    def test_a_placing_the_week_does_not_bear_out_is_caught(self, summary, placed):
        problems = self._check(summary)
        assert len(problems) == 1
        assert problems[0].startswith(f"the editorial places {placed} ")

    @pytest.mark.parametrize("summary", [
        # True on the week, or the table's.
        "Hal Penn was seventh for the week on 31, one point clear of last, and eighth in the league.",
        "Bea Gale was second on the week with 62.",
        "Gus Peel finished last on the week with 30.",
        "Cy Hart was top of the gameweek on 70.",
        "Hal Penn finished eighth in the table.",
        "Ann Moss is still first overall on 382.",
        "Ann Moss went into this gameweek sat top of the pile.",
        "Ann Moss kept top spot this week.",
        "Ann Moss is still top this week.",
        # A numbered gameweek may be another than the one recapped.
        "Hal Penn came last in GW5 too.",
        "Hal Penn slipped to eighth on 31.",
        "Bea Gale climbed from 6th to 3rd with 62.",
        # Issue #499: a placing a move leaves, true of the manager one score
        # above the lowest.
        "Hal Penn slipped out of last place on the week by a point.",
        "Hal Penn climbed out of last place on the week.",
        "Hal Penn crept out of the bottom spot on the week.",
        # Escaped, hedged or negated, #487's own words for the manager one
        # score above the lowest among them.
        "Hal Penn avoided last place on the week by 1 pt.",
        "Ann Moss would have been second on the week with a better captain.",
        "Ann Moss could have been second on the week.",
        "Hal Penn didn't finish last on the week.",
        "Hal Penn wasn't, as some feared, last on the week.",
        "Hal Penn's 31 left him one better than last on the week.",
        # Not a placing where it stands.
        "Ann Moss's third win of the season came last week.",
        "Hal Penn picked up his second last-place fine of the season.",
        "Hal Penn finished second last on the week.",
        "Ann Moss won at last, with 39.",
        "Cy Hart's 70 was the top score of the week.",
        "Hal Penn has spent two gameweeks on top.",
        "Ann Moss and Bea Gale finished second and third on the week respectively.",
        # Not the manager's placing, or nobody the clause can name.
        "Hal Penn beat only the manager who came last on the week.",
        "Ann Moss's captain was the third-best on the week.",
        "Ann Moss's Haaland blanked, and she finished seventh on the week.",
        "Cy Hart outscored Bea Gale, finishing third on the week.",
        "Ann Moss and Bea Gale both struggled. She finished seventh on the week.",
        # Nothing says it is the week's: no week word, and no score of hers.
        "Ann Moss came last.",
        "Hal Penn finished eighth, 31 points behind Cy Hart.",
        "Bea Gale's 62 was the third-best.",
        # PR #489 review: a league position near a score it is not attached
        # to, and a clause that gives the season total, are the table's.
        "Hal Penn (8th) scored 31.",
        "Cy Hart (2nd) hit 70 and took the week.",
        "Hal Penn remains eighth after a 31-point week.",
        "Hal Penn's 31 left him eighth on 252.",
        "Hal Penn scored 31 and finished eighth on 252.",
        # ...a placing counted from the other end...
        "Hal Penn was second from the bottom on the week.",
        "Ann Moss was third from the bottom on the week.",
        # ...a "-best" over a span longer than the week...
        "Dee Ford's 45 was her second-best score this campaign.",
        "Dee Ford's 45 was her second-best score in six weeks.",
        "Dee Ford's 45 was her second-best of the year.",
        "Cy Hart's 70 was the second-best score of his career.",
        # ...and another person's placing.
        "Ann Moss, who finished sixth on the week, was beaten by the week's winner, who took top spot on the week.",
        "Ann Moss had a shocker. Her rival took third on the week.",
        "Ann Moss's brother finished last on the week.",
        "Ann Moss watched a rival take top spot on the week.",
        # The drafts replay: a score left beside a league position is the
        # table's, whatever moves the clause counts first, and a negation
        # with nothing of its own before the break still reaches the placing.
        "Hal Penn's three moves netted +0 overall, and his 31 left him 8th.",
        "Hal Penn wasn't, as some feared, last.",
        "Hal Penn didn't, in the end, finish last on the week.",
        # A holding word gives way only to a score and the week straight
        # after the placing, not the week alone or said of something else.
        "Ann Moss kept top spot for the week.",
        "Ann Moss stays top with the captaincy call of the week.",
    ])
    def test_a_placing_the_check_cannot_pin_to_the_week_is_left_alone(self, summary):
        managers = _placings_table()
        managers[0]["squad"] = [_make_squad_player(name="Haaland")]
        assert self._check(summary, managers) == []

    def test_last_holds_for_every_manager_level_on_the_lowest_score(self):
        managers = _placings_table()
        managers[7]["gw_points"] = 30
        assert self._check("Hal Penn finished last on the week, level with Gus Peel.", managers) == []

    def test_a_numbered_placing_is_the_rank_level_scores_share(self):
        managers = _placings_table()
        managers[5]["gw_points"] = 62
        assert self._check("Fay Lund was second on the week with 62.", managers) == []
        assert self._check("Fay Lund was third on the week with 62.", managers) == [
            'the editorial places Fay Lund "third" for the week, but Fay Lund\'s 62 pts was 2nd on the week, '
            "level with Bea Gale"
        ]

    def test_a_best_may_also_count_the_distinct_scores_above_it(self):
        """After a shared top score the next one is "the second-best score",
        whatever rank its manager holds."""
        managers = _placings_table()
        managers[2]["gw_points"] = 70
        assert self._check("Fay Lund's 50 was the second-best score of the week.", managers) == []
        assert len(self._check("Fay Lund finished second on the week with 50.", managers)) == 1

    def test_a_shared_extreme_names_everyone_who_held_it(self):
        managers = _placings_table()
        managers[7]["gw_points"] = 30
        assert self._check("Ann Moss came last on the week with 39.", managers) == [
            'the editorial places Ann Moss "last" for the week, but Ann Moss\'s 39 pts was 6th on the week; '
            "the lowest gameweek score was 30 pts, held by Gus Peel and Hal Penn"
        ]

    def test_a_name_the_data_pads_is_read_as_the_editorial_writes_it(self):
        """PR #489 review: the names are read stripped, so a padded one from
        the API raised a KeyError, which turned every check off for the run."""
        managers = _placings_table()
        managers[7]["manager_name"] = "Hal Penn "
        assert len(self._check("Hal Penn finished eighth on 31.", managers)) == 1

    def test_a_figure_that_is_also_the_season_total_is_no_gameweek_score(self):
        """A draft head-to-head table counts league points, which can match a
        gameweek score, so the figure says neither."""
        managers = _placings_table()
        managers[7]["total_points"] = 31
        assert self._check("Hal Penn finished eighth on 31.", managers) == []

    def test_in_a_league_s_first_gameweek_every_total_is_that_week_s_score(self):
        """PR #489 review: there the figure is both, so it is the score."""
        managers = _placings_table()
        for manager in managers:
            manager["total_points"] = manager["gw_points"]
        assert len(self._check("Hal Penn finished eighth on 31.", managers)) == 1

    def test_a_lone_manager_has_nothing_to_place(self):
        assert self._check("Ann Moss finished eighth on 39.", _placings_table()[:1]) == []

    def test_the_registry_runs_it_and_the_retry_names_both_columns(self):
        data = _make_recap_data(managers=_placings_table())
        found = check_recap_editorial("Hal Penn finished eighth on 31.", data)
        assert list(found) == [RECAP_WARNING_WRONG_PLACING]
        prompt = get_recap_editorial_retry_prompt("ORIGINAL", found)
        assert "only from the GW Standings section's GW Rank column" in prompt
        assert "never set a league position beside a gameweek score" in prompt
        assert editorial_warning_message(RECAP_WARNING_WRONG_PLACING, found[RECAP_WARNING_WRONG_PLACING]).startswith(
            "The editorial gives a manager a placing on the week their gameweek score does not earn. "
            'The editorial places Hal Penn "eighth"'
        )


class TestEscapeClaimCheck:
    """Issue #488: the GW Standings section states who avoided last place on
    the week (#487), and the editorial handed that escape to the manager who
    finished last, through a pronoun in the sentence after their fine, or set
    it against the foot of the table for a manager bottom of the league. In
    `_placings_table()` Gus Peel's 30 is the week's lowest and Hal Penn, a
    point clear on 31, is bottom of the table."""

    _FINE = "Gus Peel takes the fine with 30."

    def _check(self, summary: str, managers: list[RecapManagerEntry] | None = None) -> list[str]:
        managers = managers or _placings_table()
        return check_escape_claims(summary, managers, RecapNames.of(managers))

    def test_the_escape_given_to_the_manager_who_finished_last_is_caught(self):
        assert self._check(f"{self._FINE} He escaped the wooden spoon by one cruel point over Hal Penn (31).") == [
            'the editorial says Gus Peel avoided last place on the week ("escaped the wooden spoon"), but Gus '
            "Peel's 30 pts was the lowest gameweek score; Hal Penn avoided last place on the week by 1 pt"
        ]

    def test_the_escape_set_against_the_table_for_its_bottom_manager_is_caught(self):
        assert self._check("Hal Penn's 31 left him only a point clear of the foot of the table.") == [
            'the editorial says Hal Penn avoided the bottom of the table ("clear of the foot of the table"), but '
            "Hal Penn is bottom of the table in 8th; Hal Penn avoided last place on the week by 1 pt"
        ]

    @pytest.mark.parametrize(("summary", "escaped"), [
        # The issue's sentences, with the names put back.
        (f"{_FINE} He escaped the wooden spoon by one cruel point over Hal Penn (31), who will be sending"
         " thank-you notes.", "Gus Peel"),
        (f"{_FINE} He missed the dreaded last place by the thinnest of margins, as Hal Penn (31) sat 1 point"
         " ahead.", "Gus Peel"),
        (f"{_FINE} He missed the dubious honour of 'worst week' by one point's worth of fortune: Hal Penn, on"
         " 31, escaped last place by a single point.", "Gus Peel"),
        (f"{_FINE} He escaped the bottom spot by only a single point, though.", "Gus Peel"),
        ("Hal Penn's 31 left him only a point clear of the foot of the table.", "Hal Penn"),
        # Named, and the noun as well as the verb.
        ("Gus Peel only just dodged the wooden spoon.", "Gus Peel"),
        ("Gus Peel's narrow escape from last place came down to a single point.", "Gus Peel"),
        ("Gus Peel avoided finishing last on the week by a point.", "Gus Peel"),
        ("Gus Peel's 30 was just enough to avoid last place.", "Gus Peel"),
        ("Gus Peel, who escaped last place by a point, can thank his bench.", "Gus Peel"),
        ("Gus Peel had a shocker; he survived last place by a whisker all the same.", "Gus Peel"),
        ("Gus Peel was spared the lowest score of the week.", "Gus Peel"),
        # The table's, for the manager bottom of it.
        ("Hal Penn avoided last place in the league.", "Hal Penn"),
        # PR #490 review: a pronoun goes to the first manager the sentence
        # before named, the subject a reader takes it for, even where that
        # sentence named two -- the shape the GW Standings line invites.
        (f"{_FINE[:-1]}, a point behind Hal Penn. He escaped the wooden spoon by a point.", "Gus Peel"),
        # ...and the passive, its manager after "by".
        ("The wooden spoon was avoided by Gus Peel by a point.", "Gus Peel"),
        (f"{_FINE} Last place was narrowly avoided by him.", "Gus Peel"),
        ("Hal Penn sat out the drama. Gus Peel's 30 meant last place was avoided by him.", "Gus Peel"),
        # PR #490 A/B: the scope hyphenated into a compound reads as spaced.
        (f"{_FINE} He missed the bottom-of-the-week dishonour by a single point.", "Gus Peel"),
        ("Hal Penn dodged the bottom-of-the-table scrap by a point.", "Hal Penn"),
        # PR #500 review: a "missed" further back than the escape, and a way
        # out named further off than where from, are about something else.
        ("Gus Peel missed a penalty yet escaped last place on the week.", "Gus Peel"),
        ("Gus Peel missed the bus then escaped the wooden spoon.", "Gus Peel"),
        (f"{_FINE} He missed the wooden spoon by a point in what felt like a real let-off.", "Gus Peel"),
        (f"{_FINE} He missed last place by a point thanks to a late lifeline from Hal Penn.", "Gus Peel"),
    ])
    def test_an_escape_the_data_contradicts_is_caught(self, summary, escaped):
        problems = self._check(summary)
        assert len(problems) == 1
        assert problems[0].startswith(f"the editorial says {escaped} avoided ")

    @pytest.mark.parametrize("summary", [
        # The issue's negations: the manager failed to escape.
        f"{_FINE} He missed out on avoiding it by a single point.",
        f"{_FINE} He missed the bottom-of-the-week dodge by the narrowest margin.",
        f"{_FINE} He missed his chance to escape last place.",
        # Issue #499: the escape word after "missed" is what was missed, a
        # gerund with no preposition before it as much as a noun.
        f"{_FINE} He missed avoiding last place by a single point, as Hal Penn scored 31.",
        f"{_FINE} He narrowly missed escaping the wooden spoon.",
        f"{_FINE} He missed the escape from last place by a point.",
        f"{_FINE} He missed out on the escape from last place.",
        f"{_FINE} He missed last-place misery's get-out clause by a single point.",
        f"{_FINE} He missed the wooden-spoon let-off by a point.",
        f"{_FINE} He missed the last-place lifeline by a point.",
        # True: the GW Standings line's own words, and its manager named.
        "Hal Penn avoided last place on the week by 1 pt.",
        f"{_FINE} Hal Penn escaped the wooden spoon by one cruel point.",
        "Hal Penn escaped last place by a point despite sitting bottom of the table.",
        # Hedged or negated.
        "Gus Peel nearly escaped last place.",
        "Gus Peel came close to avoiding last place.",
        "Gus Peel would have escaped last place with a captain return.",
        "Gus Peel couldn't escape last place.",
        "Gus Peel didn't, in the end, escape last place.",
        "Gus Peel needed one more point to avoid last place.",
        "Gus Peel fell a point short of escaping last place.",
        "There was no escape from last place for Gus Peel.",
        # The table's, for a manager who is not bottom of it.
        "Gus Peel stays clear of the bottom.",
        "Gus Peel is comfortably clear of the bottom.",
        "Gus Peel avoided last place in the league.",
        "Gus Peel's 30 kept him clear of the foot of the table.",
        f"{_FINE} He remains well clear of the foot of the table, though.",
        # Another week, or a span longer than this one.
        "Gus Peel escaped last place for the first time this season.",
        "Gus Peel escaped last place in GW5.",
        "Gus Peel, who escaped last place last week, was not so lucky this time.",
        # Not an escape from last place where it stands.
        "Gus Peel avoided a hit but finished last.",
        "Gus Peel survived a red card to finish bottom.",
        "Gus Peel escaped the bottom half of the table.",
        "Gus Peel dodged the fine last week.",
        "Gus Peel missed the bottom corner from the spot.",
        "Gus Peel's Salah missed the bottom corner from the spot.",
        # Not Gus Peel's escape, or nobody the clause can name.
        "Gus Peel's rival escaped last place.",
        "Gus Peel watched Hal Penn escape last place by a point.",
        "Gus Peel and Hal Penn both struggled. He escaped last place by a point.",
        "Gus Peel and Hal Penn escaped last place and the bottom half respectively.",
        "Hal Penn pipped Gus Peel by a point. He escaped last place on the week.",
        "Last place was not avoided by Gus Peel.",
        # PR #490 review: the manager who finished last failing to escape, in
        # words that are no hedge on a finite verb -- a base form, an
        # infinitive nothing says was achieved, a gerund after a preposition.
        "Gus Peel cannot escape last place.",
        "Gus Peel struggled to avoid last place.",
        "Gus Peel was powerless to avoid the wooden spoon.",
        "Gus Peel fell a point shy of avoiding last place.",
        "Gus Peel was a point from escaping the wooden spoon.",
        "Gus Peel finished one point off avoiding last place.",
        "Gus Peel had a point too few to avoid the wooden spoon.",
        "Gus Peel found it impossible to avoid last place.",
        "Gus Peel could hardly avoid the wooden spoon.",
        "Did Gus Peel escape the wooden spoon? He did not.",
        # ...a holding verb or a copula lets the escape be the week's, true
        # of the manager bottom of the table who avoided last place on it...
        "Hal Penn stays clear of the bottom.",
        "Hal Penn is a point clear of the wooden spoon.",
        "Hal Penn held on to avoid the wooden spoon by a point.",
        "Hal Penn's narrow cushion clear of the wooden spoon was just one point.",
        "Hal Penn escaped the wooden spoon by a single league point.",
        # ..."last" and "bottom" before "of", and surviving the worst week...
        "Gus Peel missed the last of Haaland's points.",
        "Gus Peel avoided the bottom of the barrel with his captain.",
        "Gus Peel avoided the bottom-of-the-barrel captain pick.",
        "Gus Peel survived the worst week of his FPL life.",
        # ...and an escape from an earlier week.
        "Gus Peel took the fine a week after he escaped the wooden spoon.",
        "Gus Peel took the fine, a week after escaping the wooden spoon.",
    ])
    def test_an_escape_the_check_cannot_pin_to_the_wrong_manager_is_left_alone(self, summary):
        assert self._check(summary) == []

    def test_an_escape_read_both_ways_is_wrong_only_when_both_readings_are(self):
        """"Stays clear of last place this week" is the table's and the
        week's at once: true of a manager bottom only on the week."""
        assert self._check("Gus Peel stays clear of last place this week.") == []
        managers = _placings_table()
        managers[6]["overall_rank"], managers[7]["overall_rank"] = 8, 7
        assert self._check("Gus Peel stays clear of last place this week.", managers) == [
            'the editorial says Gus Peel avoided last place ("clear of last place"), but Gus Peel\'s 30 pts was '
            "the lowest gameweek score, and Gus Peel is bottom of the table in 8th; Hal Penn avoided last place "
            "on the week by 1 pt"
        ]

    def test_a_shared_lowest_score_names_everyone_level_on_it(self):
        managers = _placings_table()
        managers[0]["gw_points"] = 30
        assert self._check("Gus Peel dodged the wooden spoon.", managers) == [
            'the editorial says Gus Peel avoided last place on the week ("dodged the wooden spoon"), but Gus '
            "Peel's 30 pts was the lowest gameweek score, level with Ann Moss; Hal Penn avoided last place on "
            "the week by 1 pt"
        ]

    def test_a_shared_next_score_names_everyone_who_avoided_last_place(self):
        managers = _placings_table()
        managers[4]["gw_points"] = 31
        assert self._check("Gus Peel dodged the wooden spoon.", managers)[0].endswith(
            "; Eli Ray, Hal Penn each avoided last place on the week by 1 pt"
        )

    def test_the_retry_is_told_the_escape_in_the_prompt_s_own_words(self):
        """PR #490 review: the GW Standings line and the check each wrote the
        escape by hand, and a shared next score already read differently."""
        managers = _placings_table()
        managers[4]["gw_points"] = 31
        line = last_place_escape(managers)
        assert line == "Eli Ray, Hal Penn each avoided last place on the week by 1 pt"
        assert f"so {line}" in format_recap_standings_context(_make_recap_data(managers=managers))
        assert self._check("Gus Peel dodged the wooden spoon.", managers)[0].endswith(f"; {line}")

    def test_a_display_name_two_managers_share_hides_neither_score(self):
        """PR #490 review: scores read off the name-keyed rows dropped one of
        two managers sharing a name, and the retry was told a false lowest."""
        managers = [
            _make_manager(name="Ann Moss", entry_id=1, gw_points=30, overall_rank=1),
            _make_manager(name="Ann Moss", entry_id=2, gw_points=40, overall_rank=2),
            _make_manager(name="Hal Penn", entry_id=3, gw_points=35, overall_rank=3),
        ]
        assert self._check("Hal Penn dodged the wooden spoon.", managers) == []

    def test_with_every_score_level_nobody_avoided_last_place(self):
        managers = _placings_table()
        for manager in managers:
            manager["gw_points"] = 40
        assert self._check("Gus Peel dodged the wooden spoon.", managers)[0].endswith(
            "; every manager scored it, so nobody avoided last place on the week"
        )

    def test_the_table_is_read_only_where_every_manager_has_a_position(self):
        managers = _placings_table()
        managers[0]["overall_rank"] = 0
        assert self._check("Hal Penn avoided last place in the league.", managers) == []

    def test_a_lone_manager_has_no_last_place_to_escape(self):
        assert self._check("Gus Peel dodged the wooden spoon.", _placings_table()[6:7]) == []

    def test_the_registry_runs_it_and_the_retry_says_who_avoided_last_place(self):
        data = _make_recap_data(managers=_placings_table())
        found = check_recap_editorial(f"{self._FINE} He escaped the bottom spot by only a single point, though.", data)
        assert list(found) == [RECAP_WARNING_ESCAPE_MISATTRIBUTION]
        prompt = get_recap_editorial_retry_prompt("ORIGINAL", found)
        assert "Your previous draft contradicted the data it was given" in prompt
        assert "Hal Penn avoided last place on the week by 1 pt" in prompt
        assert "only if their gameweek score was above the lowest" in prompt
        message = editorial_warning_message(
            RECAP_WARNING_ESCAPE_MISATTRIBUTION, found[RECAP_WARNING_ESCAPE_MISATTRIBUTION],
        )
        assert message.startswith(
            "The editorial says a manager avoided a last place they finished in. "
            'The editorial says Gus Peel avoided last place on the week ("escaped the bottom spot")'
        )


class TestStandingsTieLines:
    """Issue #359: the prompt states which scores and positions are shared,
    so the tie rule has a line to hold "joint" to."""

    def _text(self, managers: list[RecapManagerEntry]) -> str:
        return format_recap_standings_context(_make_recap_data(managers=managers))

    def test_outright_extremes_and_every_shared_score_and_position_are_named(self):
        text = self._text(_reported_table())
        assert "Highest gameweek score: 58 pts, Fay Lund alone" in text
        assert "Lowest gameweek score: 29 pts, Ann Moss alone" in text
        assert (
            "Gameweek scores shared by more than one manager (1): 43 pts (Dee Ford, Eli Ray)"
            " - every other gameweek score was one manager's alone"
        ) in text
        assert "League positions shared in the Pos column (1): 7th (Ann Moss, Bea Gale, Cy Hart, all on 340)" in text
        assert "not on gameweek scores" in text
        assert text.index("Lowest gameweek score") < text.index("| Pos |")

    def test_a_shared_extreme_names_everyone_on_it(self):
        managers = _reported_table()
        managers[1]["gw_points"] = 29
        assert "Lowest gameweek score: 29 pts, shared by Ann Moss, Bea Gale" in self._text(managers)

    def test_nothing_shared_is_said_outright(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=60, overall_rank=1, previous_rank=2, total_points=500),
            _make_manager(name="Bob", entry_id=2, gw_points=40, overall_rank=2, previous_rank=1, total_points=490),
        ]
        text = self._text(managers)
        assert "Gameweek scores shared by more than one manager: none" in text
        assert "League positions shared in the Pos column: none" in text
        assert "Previous positions shared" not in text
        assert "tie-break" not in text

    def test_a_shared_prev_position_is_stated_too(self):
        """PR #374 review: the Pos line said "none" above a Prev column that
        showed a tie, which the tie check accepts."""
        managers = _reported_table()
        text = self._text(managers)
        # Prev is 6, 7, 9, 1, 2, 10: nothing shared, so no line.
        assert "Previous positions shared" not in text
        managers[1]["previous_rank"] = 6
        assert "Previous positions shared in the Prev column (1): 6th (Ann Moss, Bea Gale)" in self._text(managers)

    def test_level_totals_placed_apart_are_stated(self):
        managers = [
            _make_manager(name="Ann Moss", entry_id=1, gw_points=50, overall_rank=1, total_points=21),
            _make_manager(name="Bea Gale", entry_id=2, gw_points=40, overall_rank=2, total_points=21),
        ]
        assert (
            "Level on season total but placed apart by the league's tie-break (1): "
            "21 (Ann Moss 1st, Bea Gale 2nd) - they are level on points, not joint in position"
        ) in self._text(managers)

    def test_no_positions_line_without_positions(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, overall_rank=None, previous_rank=None),
            _make_manager(name="Bob", entry_id=2, overall_rank=None, previous_rank=None),
        ]
        assert "League positions shared" not in self._text(managers)


class TestStandingsMarginLines:
    """Issue #484: the table is in league order, so the week's runner-up sat
    four rows above a winner who had climbed four places, and the editorial
    wrote "won by four clear points" for an 84-81 week. Each extreme states
    its gap to the nearest score, so a margin is quoted rather than counted."""

    def _text(self, managers: list[RecapManagerEntry]) -> str:
        return format_recap_standings_context(_make_recap_data(managers=managers))

    def test_the_winners_margin_names_the_runner_up(self):
        rows = [
            ("Ann Moss", 81, 1, 1), ("Bea Gale", 73, 2, 2), ("Cy Hart", 68, 3, 3),
            ("Dee Ford", 70, 4, 4), ("Eli Ray", 84, 5, 9), ("Fay Lund", 50, 6, 5),
        ]
        managers = [
            _make_manager(name=name, entry_id=i, gw_points=pts, overall_rank=rank, previous_rank=prev)
            for i, (name, pts, rank, prev) in enumerate(rows, start=1)
        ]
        text = self._text(managers)
        assert (
            "Highest gameweek score: 84 pts, Eli Ray alone - 3 pts clear of the next-best score, 81 pts (Ann Moss)"
        ) in text
        assert (
            "Lowest gameweek score: 50 pts, Fay Lund alone - 18 pts behind the next-lowest score, 68 pts (Cy Hart)"
        ) in text
        assert "Eli Ray (↑4)" in text

    def test_a_shared_next_score_names_everyone_on_it(self):
        text = self._text(_reported_table())
        assert "58 pts, Fay Lund alone - 15 pts clear of the next-best score, 43 pts (Dee Ford, Eli Ray)" in text
        assert "29 pts, Ann Moss alone - 4 pts behind the next-lowest score, 33 pts (Bea Gale)" in text

    def test_a_shared_extreme_gives_each_holder_the_gap_not_one_over_another(self):
        """PR #487 review: "shared by A, B - 10 pts clear" read as A's margin
        and invited "A won by ten" over a manager level with them."""
        managers = [
            _make_manager(name="Ann Moss", entry_id=1, gw_points=80, overall_rank=1),
            _make_manager(name="Bea Gale", entry_id=2, gw_points=80, overall_rank=2),
            _make_manager(name="Cy Hart", entry_id=3, gw_points=70, overall_rank=3),
        ]
        assert (
            "Highest gameweek score: 80 pts, shared by Ann Moss, Bea Gale - level with each other, "
            "each 10 pts clear of the next-best score, 70 pts (Cy Hart)"
        ) in self._text(managers)

    def test_a_shared_lowest_gives_each_holder_the_gap(self):
        managers = _reported_table()
        managers[1]["gw_points"] = 29
        assert (
            "Lowest gameweek score: 29 pts, shared by Ann Moss, Bea Gale - level with each other, "
            "each 6 pts behind the next-lowest score, 35 pts (Cy Hart), so Cy Hart avoided last place on the week by 6 pts"
        ) in self._text(managers)

    def test_who_avoided_last_place_is_stated_by_name_and_as_the_weeks(self):
        """PR #487: with the bottom gap stated, the editorial told the
        next-lowest manager's escape and gave it to the manager who finished
        last ("He escaped the bottom spot"), or set it against the foot of the
        table where that manager was bottom of the league. The escape is
        stated by name and scoped to the week."""
        managers = [
            _make_manager(name="Ann Moss", entry_id=1, gw_points=30, overall_rank=6),
            _make_manager(name="Bea Gale", entry_id=2, gw_points=31, overall_rank=8),
            _make_manager(name="Cy Hart", entry_id=3, gw_points=65, overall_rank=1),
        ]
        assert (
            "Lowest gameweek score: 30 pts, Ann Moss alone - 1 pt behind the next-lowest score, 31 pts (Bea Gale), "
            "so Bea Gale avoided last place on the week by 1 pt"
        ) in self._text(managers)

    def test_a_shared_next_lowest_each_avoided_last_place(self):
        managers = _reported_table()
        managers[2]["gw_points"] = 33
        assert "33 pts (Bea Gale, Cy Hart), so Bea Gale, Cy Hart each avoided last place on the week by 4 pts" in (
            self._text(managers)
        )

    def test_the_highest_line_states_no_escape(self):
        assert "avoided" not in self._text(_reported_table()).split("Lowest gameweek score")[0]

    def test_a_one_point_margin(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=61, overall_rank=1),
            _make_manager(name="Bob", entry_id=2, gw_points=60, overall_rank=2),
        ]
        text = self._text(managers)
        assert "61 pts, Alice alone - 1 pt clear of the next-best score, 60 pts (Bob)" in text
        assert "60 pts, Bob alone - 1 pt behind the next-lowest score, 61 pts (Alice)" in text

    def test_no_gap_when_every_score_is_level(self):
        managers = [
            _make_manager(name="Alice", entry_id=1, gw_points=55, overall_rank=1),
            _make_manager(name="Bob", entry_id=2, gw_points=55, overall_rank=2),
        ]
        text = self._text(managers)
        assert "Highest gameweek score: 55 pts, shared by Alice, Bob\n" in text
        assert "clear of" not in text
        assert "behind the next-lowest" not in text

    def test_system_prompt_quotes_a_gameweek_margin_only_as_stated(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "Never work a gap out of the GW Pts column yourself" in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_the_rule_names_the_gaps_by_the_words_the_section_uses(self):
        """PR #487 review: the rule points at the section's two gaps, so
        rewording one without the other would leave it pointing at nothing."""
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        rule = next(line for line in RECAP_SYNTHESIS_SYSTEM_PROMPT.splitlines() if "Never work a gap out" in line)
        text = self._text(_reported_table())
        for gap in ("the next-best", "the next-lowest"):
            assert gap in rule
            assert f"{gap} score" in text

    def test_the_rule_gives_no_figure_a_gameweek_could_echo(self):
        """PR #487 review: examples carrying the A/B gameweek's own margins
        ("won by three") left reading the gap and echoing the example
        indistinguishable. Placeholders cannot be right by coincidence."""
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        rule = next(line for line in RECAP_SYNTHESIS_SYSTEM_PROMPT.splitlines() if "Never work a gap out" in line)
        examples = re.findall(r'"([^"]+)"', rule)
        assert examples
        number_words = r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
        assert not [e for e in examples if re.search(rf"\d|\b(?:{number_words})\b", e, re.IGNORECASE)]

    def test_the_section_states_no_gap_between_season_totals(self):
        """Issue #493: an editorial put third place "50 points behind" a
        leader 55 points clear, its own subtraction from the Total column.
        The rule tells the model no line states a gap between totals, so one
        that starts to has to come with a rule that quotes it. A gap stated
        in any words carries its figure, so the figures are what is looked
        for (PR #497 review: a word list let "leads by 5" through)."""
        managers = [
            _make_manager(name="Ann Moss", entry_id=1, gw_points=52, overall_rank=1, previous_rank=1, total_points=382),
            _make_manager(name="Bea Gale", entry_id=2, gw_points=61, overall_rank=2, previous_rank=2, total_points=377),
            _make_manager(name="Cy Hart", entry_id=3, gw_points=48, overall_rank=3, previous_rank=3, total_points=327),
        ]
        gaps = {abs(a["total_points"] - b["total_points"]) for a, b in combinations(managers, 2)}
        stated = {
            int(figure)
            for line in self._text(managers).splitlines() if not line.startswith("|")
            for figure in re.findall(r"\d+", line)
        }
        assert gaps == {5, 50, 55}
        assert not stated & gaps

    def test_the_rule_names_the_totals_column_by_its_header(self):
        """Issue #493: the rule points at the column a total gap would be
        worked out of, so renaming the header would leave it pointing at
        nothing."""
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        rule = next(line for line in RECAP_SYNTHESIS_SYSTEM_PROMPT.splitlines() if "Never work a gap out" in line)
        assert "never work one out of the Total column" in rule
        assert "| Total |" in self._text(_reported_table())


class TestEditorialChecks:
    """One entry point keyed by warning code, so the command reports each
    check on its own channel and the retry is told only about what failed."""

    def test_a_clean_editorial_has_no_keys(self):
        data = _make_recap_data(managers=_reported_transfers())
        assert check_recap_editorial("Alice Lowe finished -6 net overall.", data) == {}

    def test_each_check_answers_under_its_own_code(self):
        data = _make_recap_data(managers=_reported_transfers())
        found = check_recap_editorial(
            "Gibbs-White for Wirtz went -6 net. Bob Gale posted the joint-lowest score of the week.", data,
        )
        assert set(found) == {RECAP_WARNING_NET_MISATTRIBUTION, RECAP_WARNING_UNSUPPORTED_TIE}

    def test_every_check_is_registered_once_with_everything_that_names_it(self):
        """PR #374 review: one record per check, so a fourth is one edit."""
        codes = [check.code for check in EDITORIAL_CHECKS]
        assert sorted(codes) == sorted({
            RECAP_WARNING_CONTESTED_MISATTRIBUTION, RECAP_WARNING_NET_MISATTRIBUTION,
            RECAP_WARNING_KIND_NET_MISATTRIBUTION, RECAP_WARNING_UNSUPPORTED_TIE,
            RECAP_WARNING_WRONG_PLACING, RECAP_WARNING_ESCAPE_MISATTRIBUTION, RECAP_WARNING_CONTESTED_ROLL_CALL,
            RECAP_WARNING_PENALTY_REPEATED,
        })
        assert all(check.lead and check.retry_instruction for check in EDITORIAL_CHECKS)

    def test_the_warning_message_keeps_claims_apart_that_carry_semicolons(self):
        """PR #374 review: claims joined with "; " could not be split when a
        claim carried one itself, as a quoted contested line does."""
        message = editorial_warning_message(
            RECAP_WARNING_CONTESTED_MISATTRIBUTION,
            ['the editorial says Bob won King, but the line reads: "Alice won him; Bob was beaten to him"',
             "the editorial says Cam won Isak, but the line reads: \"Dan won him\""],
        )
        assert message.startswith("The editorial contradicts the contested waiver claims it was given. ")
        assert message.count(". The editorial says ") == 2
        assert message.endswith('"Dan won him".')

    def test_the_names_every_check_reads_take_in_every_player_the_data_carries(self):
        """PR #380 review: captains are names too, while an auto-sub line is a
        sentence about squad players already counted, not a name."""
        manager = _make_manager(
            name="Ann Moss", captain="Calvert-Lewin", vice_captain="Ward-Prowse",
            squad=[_make_squad_player(name="King")],
        )
        manager["auto_subs"] = ["King on for Wood (6 pts)"]
        names = RecapNames.of([manager])
        assert names.players == {"Calvert-Lewin", "Ward-Prowse", "King"}
        assert names.managers == ("Ann Moss",)
        assert [m.group(0) for m in names.find("Moss watched Calvert-Lewin and Ann Moss's King.")] == [
            "Moss", "Calvert-Lewin", "Ann Moss", "King",
        ]

    def test_a_check_that_crashes_is_not_the_editorial_s_problem(self):
        # The command treats the checks as advisory; see the CLI test.
        data = _make_recap_data(managers=_reported_transfers())
        del data["managers"][0]["manager_name"]  # type: ignore[misc]
        with pytest.raises(KeyError):
            check_recap_editorial("Gibbs-White for Wirtz went -6 net.", data)

    def test_the_retry_prompt_carries_an_instruction_only_for_what_failed(self):
        prompt = get_recap_editorial_retry_prompt(
            "ORIGINAL", {RECAP_WARNING_UNSUPPORTED_TIE: ['the editorial says "joint-lowest"']},
        )
        assert '- the editorial says "joint-lowest"' in prompt
        assert "GW Standings section lists it as shared" in prompt
        assert '"respectively"' not in prompt
        assert "net overall" not in prompt

    def test_the_system_prompt_pins_figures_and_ties_to_their_subjects(self):
        system, _ = get_recap_synthesis_prompt(
            gw=3, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="",
        )
        assert "they are never interchangeable" in system
        assert "never to one named move" in system
        assert "never their waivers' or their free agents'" in system
        assert 'never that they took it "to bring in"' in system
        assert "may only be used where the data states the tie" in system
        assert 'never "the joint-lowest"' in system


# ---------------------------------------------------------------------------
# Standout players (issue #455)
# ---------------------------------------------------------------------------


def _standout_row(
    name: str,
    points: int,
    code: int,
    *,
    contributed: bool = True,
    club: str | None = "Arsenal",
    had_fixture: bool = True,
    minutes: int | None = None,
    draft_rank: int | None = None,
    ppg: float | None = None,
    appearances: int | None = None,
    **kwargs: Any,
) -> RecapManagerPlayer:
    """A squad row as a collector records it, its main id 1,000 above its
    code so a row that emitted one for the other would show it. Minutes and
    the draft-only fields are set only when given, as a hand-built row
    without them arrives."""
    player = _make_squad_player(name=name, points=points, contributed=contributed, code=code, **kwargs)
    player["team_name"] = club
    player["main_id"] = code + 1000
    player["had_fixture"] = had_fixture
    for key, value in (
        ("minutes", minutes), ("draft_rank", draft_rank),
        ("points_per_game", ppg), ("appearances", appearances),
    ):
        if value is not None:
            player[key] = value  # type: ignore[literal-required]
    return player


def _standouts(
    managers: list[RecapManagerEntry], *, fpl_format: str = "classic", gameweek: int = 7,
    live: bool = True, whole_league: bool = True,
):
    """The section as the command builds it: a live classic GW7 recap of the
    whole league unless told otherwise."""
    return find_standout_players(
        managers, fpl_format=fpl_format, gameweek=gameweek, live=live, whole_league=whole_league,
    )


def _standout_league(*squads: tuple[str, list[RecapManagerPlayer]]) -> list[RecapManagerEntry]:
    """One manager per (name, squad), each with an entry id of their own."""
    return [
        _make_manager(name=name, entry_id=i, squad=squad)
        for i, (name, squad) in enumerate(squads, start=1)
    ]


_EIGHT = ("Alice", "Bob", "Cam", "Dan", "Eve", "Fay", "Gus", "Hal")


def _haaland_league(counted: set[str], benched: frozenset[str] = frozenset()) -> list[RecapManagerEntry]:
    """Eight managers, Haaland's 17 counting for `counted` and sat on the
    bench of `benched`; nobody else owns anyone."""
    def squad(name: str) -> list[RecapManagerPlayer]:
        if name in counted or name in benched:
            return [_standout_row("Haaland", 17, 223, contributed=name in counted, club="Manchester City")]
        return []

    return _standout_league(*((name, squad(name)) for name in _EIGHT))


class TestFindStandoutPlayers:
    def test_top_scorers_rank_on_points_and_say_whom_they_scored_for(self):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Haaland", 17, 223, club="Manchester City")]),
            ("Bob", [_standout_row("Saka", 12, 118)]),
            ("Cam", [_standout_row("Mbeumo", 15, 450, club="Manchester United")]),
        ))

        assert [p["name"] for p in standouts["top_scorers"]] == ["Haaland", "Mbeumo", "Saka"]
        assert standouts["top_scorers"][0]["text"] == "Haaland (Manchester City) scored 17 points for Alice."
        assert standouts["managers"] == 3

    def test_a_tie_on_points_goes_to_the_player_who_counted_for_more_managers(self):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Aaronson", 10, 1)]),
            ("Bob", [_standout_row("Zirkzee", 10, 2)]),
            ("Cam", [_standout_row("Zirkzee", 10, 2)]),
        ))

        assert [p["name"] for p in standouts["top_scorers"]] == ["Zirkzee", "Aaronson"]

    def test_the_cap_counts_the_players_level_with_the_last_one_listed(self):
        squads = [(f"M{i}", [_standout_row(f"P{i}", 9, 10 + i)]) for i in range(7)]
        standouts = _standouts(_standout_league(*squads, ("Low", [_standout_row("Low", 8, 99)])))

        assert len(standouts["top_scorers"]) == 5
        assert standouts["top_scorers_omitted"] == 2
        scorers, _ = standout_players_lines(standouts)
        assert scorers[-1] == "2 more players also scored 9 points."

    def test_a_player_the_last_one_listed_outscored_was_not_left_out(self):
        squads = [(f"M{i}", [_standout_row(f"P{i}", 10, 10 + i)]) for i in range(5)]
        standouts = _standouts(_standout_league(*squads, ("Low", [_standout_row("Low", 9, 99)])))

        assert standouts["top_scorers_omitted"] == 0
        assert len(standout_players_lines(standouts)[0]) == 5

    def test_a_haul_left_on_a_bench_is_named_with_whoever_benched_him(self):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Groß", 17, 1, contributed=False, club="Brighton")]),
            ("Bob", [_standout_row("Haaland", 12, 2, club="Manchester City")]),
            ("Cam", [_standout_row("Haaland", 12, 2, contributed=False, club="Manchester City")]),
        ))

        gross, haaland = standouts["top_scorers"]
        assert gross["text"] == "Groß (Brighton) scored 17 points, but Alice left him on the bench."
        assert gross["counted_for"] == []
        assert haaland["text"] == "Haaland (Manchester City) scored 12 points for Bob; Cam left him on the bench."

    def test_a_sub_who_came_on_counted_and_the_starter_he_replaced_counted_for_nobody(self):
        """The starter is recorded as having contributed -- the draft
        collector's shape -- and the sub-out flag still keeps him out: he
        never played, so he was nobody's blank."""
        standouts = _standouts(_standout_league(
            ("Alice", [
                _standout_row("Raya", 0, 1, auto_sub_out=True),
                _standout_row("Kelleher", 6, 2, club="Brentford", auto_sub_in=True),
            ]),
        ))

        assert [p["text"] for p in standouts["top_scorers"]] == ["Kelleher (Brentford) scored 6 points for Alice."]
        assert standouts["blanks"] == []

    def test_an_unmatched_draft_pick_is_neither_a_scorer_nor_a_blank(self):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Ghost", 0, 1, unmatched=True), _standout_row("Saka", 7, 2)]),
        ))

        assert [p["name"] for p in standouts["top_scorers"]] == ["Saka"]
        assert standouts["blanks"] == []

    def test_blanks_that_counted_for_the_most_managers_come_first_then_the_lowest(self):
        def salah() -> RecapManagerPlayer:
            return _standout_row("Salah", 2, 1, club="Liverpool")

        standouts = _standouts(_standout_league(
            ("Alice", [salah(), _standout_row("Wirtz", 1, 2, club="Liverpool")]),
            ("Bob", [salah(), _standout_row("Palmer", 0, 3, club="Chelsea")]),
            ("Cam", [salah(), _standout_row("Saka", 3, 4)]),
        ))

        assert [p["name"] for p in standouts["blanks"]] == ["Salah", "Palmer", "Wirtz"]
        assert standouts["blanks"][0]["text"] == "Salah (Liverpool) managed 2 points for Alice, Bob and Cam."
        # Three points is a return, and a blank is never a top scorer.
        assert [p["name"] for p in standouts["top_scorers"]] == ["Saka"]

    def test_a_blank_left_on_the_bench_cost_nobody_and_is_not_listed(self):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Salah", 1, 1, contributed=False)]),
        ))

        assert standouts["blanks"] == []

    def test_a_player_whose_club_had_no_fixture_is_no_blank(self):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Saliba", 0, 1, had_fixture=False)]),
        ))

        assert standouts["blanks"] == []

    @pytest.mark.parametrize(("owners", "tail"), [
        (1, "2 more players also managed 1 point."),
        (2, "2 more players also managed 1 point for 2 managers each."),
    ])
    def test_the_cap_on_blanks_counts_the_ones_level_with_the_last(self, owners: int, tail: str):
        squads = [
            (f"M{i}", [_standout_row(f"P{j}", 1, 10 + j) for j in range(5)])
            for i in range(owners)
        ]
        standouts = _standouts(_standout_league(*squads))

        assert len(standouts["blanks"]) == 3
        assert standouts["blanks_omitted"] == 2
        assert standout_players_lines(standouts)[1][-1] == tail

    def test_a_red_card_is_said(self):
        standouts = _standouts(_standout_league(
            ("Hal", [_standout_row("Wirtz", -1, 1, club="Liverpool", red_cards=1)]),
        ))

        assert standouts["blanks"][0]["text"] == "Wirtz (Liverpool) was sent off and managed -1 point for Hal."

    def test_a_player_with_no_club_is_named_alone(self):
        standouts = _standouts(_standout_league(("Alice", [_standout_row("Saka", 9, 1, club=None)])))

        assert standouts["top_scorers"][0]["text"] == "Saka scored 9 points for Alice."

    def test_rows_carry_the_main_game_id_and_each_managers_ledger_key(self):
        """Two managers sharing a display name stay two: each is keyed as
        the ledger keys them."""
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Haaland", 17, 223)]),
            ("Alice", [_standout_row("Haaland", 17, 223)]),
        ))

        row = standouts["top_scorers"][0]
        assert (row["id"], row["code"]) == (1223, 223)
        assert row["counted_for"] == [
            {"manager_key": 1, "manager_name": "Alice"},
            {"manager_key": 2, "manager_name": "Alice"},
        ]

    def test_a_league_with_nobody_to_name_has_an_empty_section(self):
        standouts = _standouts(_standout_league(("Alice", []), ("Bob", [])))

        assert standouts == {
            "managers": 2, "whole_league": True, "top_scorers": [], "top_scorers_omitted": 0,
            "blanks": [], "blanks_omitted": 0, "blanks_order": "managers",
        }
        assert standout_players_lines(standouts) == ([], [])


class TestStandoutPlayerSentences:
    """Who a player counted for, named where the list is short enough to be
    the story and counted where it is not, so a template player in a big
    league is never a roll call."""

    @pytest.mark.parametrize(("counted", "benched", "expected"), [
        ({"Alice", "Bob", "Cam"}, frozenset(), "for Alice, Bob and Cam."),
        (set(_EIGHT), frozenset(), "for all 8 managers."),
        (set(_EIGHT) - {"Gus", "Hal"}, frozenset(), "for 6 of the 8 managers, everyone but Gus and Hal."),
        ({"Alice", "Bob", "Cam", "Dan"}, frozenset(), "for 4 of the 8 managers."),
        (
            set(_EIGHT) - {"Hal"}, frozenset({"Hal"}),
            "for 7 of the 8 managers, everyone but Hal; Hal left him on the bench.",
        ),
        ({"Alice"}, frozenset({"Bob", "Cam", "Dan", "Eve"}), "for Alice; 4 managers left him on the bench."),
    ])
    def test_whom_he_counted_for(self, counted: set[str], benched: frozenset[str], expected: str):
        standouts = _standouts(_haaland_league(counted, benched))

        assert standouts["top_scorers"][0]["text"] == f"Haaland (Manchester City) scored 17 points {expected}"


class TestStandoutPlayersPromptSection:
    def _data(self, fpl_format: str = "classic"):
        managers = _standout_league(
            ("Alice", [
                _standout_row("Haaland", 17, 1, club="Manchester City"),
                _standout_row("Salah", 2, 2, club="Liverpool", draft_rank=40),
            ]),
            ("Bob", [_standout_row("Salah", 2, 2, club="Liverpool", draft_rank=40)]),
        )
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = fpl_format
        data["standout_players"] = _standouts(managers, fpl_format=fpl_format)
        return data

    def test_the_classic_section_reads_as_sentences(self):
        assert format_recap_standout_players_context(self._data()).splitlines() == [
            "Each player's own points this gameweek, before any captaincy, among the players in this "
            "league's squads. A blank is 2 points or fewer.",
            "Top scorers:",
            "- Haaland (Manchester City) scored 17 points for Alice.",
            "Blanks that counted for the most managers:",
            "- Salah (Liverpool) managed 2 points for Alice and Bob.",
        ]

    def test_a_draft_section_has_no_captaincy_and_orders_blanks_by_draft_rank(self):
        text = format_recap_standout_players_context(self._data("draft"))

        assert "captaincy" not in text
        assert "Blanks among the best-ranked players, by FPL's pre-season player ranking:" in text

    def test_the_section_states_facts_and_gives_no_instructions(self):
        """It owns its facts like every other section, so the model may use
        them as they stand; a rule about them would belong to the system
        prompt, which this section leaves alone."""
        text = format_recap_standout_players_context(self._data())

        assert not re.search(r"(?i)\b(?:never|do not|don't|must|only)\b", text)

    def test_no_section_without_the_data_or_without_anyone_in_it(self):
        assert format_recap_standout_players_context(_make_recap_data()) == ""
        empty = _make_recap_data(managers=_standout_league(("Alice", [])))
        empty["standout_players"] = _standouts(empty["managers"])
        assert format_recap_standout_players_context(empty) == ""

    def test_the_section_follows_the_standings(self):
        _, user = get_recap_synthesis_prompt(
            gw=7, league_name="Test League", fpl_format="classic",
            awards_text="- **Gw Winner:** Alice", standings_text="| Pos |", fines_text="",
            captains_text="Total captains: 1",
            standout_players_text=format_recap_standout_players_context(self._data()),
        )

        assert user.index("## GW Standings") < user.index("## Standout Players") < user.index("## Captains")
        assert "- Haaland (Manchester City) scored 17 points for Alice." in user

    def test_no_heading_without_the_section(self):
        _, user = get_recap_synthesis_prompt(
            gw=7, league_name="Test League", fpl_format="classic",
            awards_text="", standings_text="", fines_text="",
        )

        assert "Standout Players" not in user


class TestStandoutPlayersReport:
    async def _render(self, tmp_path: Path, data: dict[str, Any]) -> str:
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run(context={"report_type": "league-recap", "gameweek": 7, "data": data})
        assert result.data is not None
        return Path(result.data["report_path"]).read_text(encoding="utf-8")

    def _data(self, fpl_format: str = "classic") -> dict[str, Any]:
        managers = _standout_league(
            ("Alice", [
                _standout_row("Haaland", 17, 1, club="Manchester City"),
                _standout_row("Salah", 2, 2, club="Liverpool", draft_rank=40),
            ]),
            ("Bob", [
                _standout_row("Salah", 2, 2, club="Liverpool", draft_rank=40),
                _standout_row("Groß", 13, 3, contributed=False, club="Brighton"),
            ]),
        )
        return {
            "gameweek": 7, "league_name": "Test League", "fpl_format": fpl_format,
            "managers": managers, "awards": {}, "standout_players": _standouts(managers, fpl_format=fpl_format),
        }

    @pytest.mark.parametrize("fpl_format", ["classic", "draft"])
    async def test_the_report_leaves_the_section_to_the_editorial(self, tmp_path: Path, fpl_format: str):
        """#482: the prompt carries the section, the shared report never does."""
        data = self._data(fpl_format)
        content = await self._render(tmp_path, data)

        assert "Standout Players" not in content
        assert "Top Scorers" not in content
        assert "Haaland" not in content
        assert "# GW Standings" in content
        # Rendering never strips the rows the JSON payload reads.
        assert data["standout_players"]["top_scorers"]


class TestCollectorsFindStandoutPlayers:
    """Both collectors record what the section is built from -- the main
    game's id, minutes, and in draft the rank and season average -- and
    leave building it to the command, so a backfill's replays never pay
    for it. Every row carries the main game's id, a draft recap's too, whose
    draft ids run 900 above the main ones here (#418)."""

    _SLOTS = TestCollectorBestXIGain._SLOTS
    # The #350 bench's Groß: 17 points on the bench, the week's top score.
    _BENCHED_HAUL = 13

    async def test_the_classic_collector_records_what_the_section_reads(self):
        player_map = {
            i: make_player(id=i, code=100 + i, web_name=f"P{i}", team_id=1, position=position)
            for i, (position, _) in enumerate(self._SLOTS, start=1)
        }
        live_stats = {
            i: {"total_points": points, "minutes": 90 if points else 0}
            for i, (_, points) in enumerate(self._SLOTS, start=1)
        }
        picks = _picks_response(points=63, total_points=63)
        picks["picks"] = [
            {"element": i, "position": i, "multiplier": 1 if i <= 11 else 0, "is_captain": False, "is_vice_captain": False}
            for i in range(1, 16)
        ]
        standings = [{"entry": 1, "player_name": "Alice", "event_total": 63, "total": 63}]
        client = _FakeClassicClient(_standings_response(standings), {1: picks})

        data = await collect_classic_recap_data(
            client, {"fpl": {"classic_league_id": 1}}, gw=4,
            live_stats=live_stats, player_map=player_map, teams={}, is_live_gw=False,
        )

        assert "standout_players" not in data
        standouts = find_standout_players(
            data["managers"], fpl_format="classic", gameweek=4, live=False, whole_league=True,
        )
        top = standouts["top_scorers"][0]
        assert (top["id"], top["code"], top["points"]) == (self._BENCHED_HAUL, 100 + self._BENCHED_HAUL, 17)
        assert top["text"] == f"P{self._BENCHED_HAUL} scored 17 points, but Alice left him on the bench."
        # The 2-point defender is the one starter who blanked, a full 90 played.
        assert standouts["blanks_order"] == "managers"
        assert [(b["name"], b["minutes"], b["draft_rank"]) for b in standouts["blanks"]] == [("P5", 90, None)]
        assert standouts["blanks"][0]["text"] == "P5 managed 2 points for Alice."

    async def _collect_draft(self, rank_of: Any = lambda i: 600 - i) -> Any:
        draft_players = [
            make_draft_player(
                id=900 + i, code=100 + i, web_name=f"P{i}", team=1, element_type=position.value,
                draft_rank=rank_of(i),
            )
            for i, (position, _) in enumerate(self._SLOTS, start=1)
        ]
        main_players = [
            make_player(
                id=i, code=100 + i, web_name=f"P{i}", team_id=1, position=position,
                total_points=10 * i, points_per_game=float(i),
            )
            for i, (position, _) in enumerate(self._SLOTS, start=1)
        ]
        live_stats = {
            i: {"total_points": points, "minutes": 90 if points else 0}
            for i, (_, points) in enumerate(self._SLOTS, start=1)
        }
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value={
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 55, "total": 55}],
            "league_entries": [{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        })
        client.get_bootstrap_static = AsyncMock(return_value={"elements": draft_players})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        client.get_entry_picks = AsyncMock(return_value={
            "picks": [{"element": 900 + i, "position": i} for i in range(1, 16)],
            "subs": [],
        })
        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            return await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=4, live_stats=live_stats,
                players=main_players, teams={}, is_live_gw=False,
            )

    async def test_the_draft_collector_records_main_game_ids_rank_and_average(self):
        data = await self._collect_draft()

        assert "standout_players" not in data
        standouts = find_standout_players(
            data["managers"], fpl_format="draft", gameweek=4, live=False, whole_league=True,
        )
        top = standouts["top_scorers"][0]
        assert (top["id"], top["code"]) == (self._BENCHED_HAUL, 100 + self._BENCHED_HAUL)
        assert top["benched_by"] == [{"manager_key": 10, "manager_name": "A B"}]
        assert all(p["id"] < 900 for p in standouts["top_scorers"] + standouts["blanks"])
        # The rank is the draft game's; the average and appearances are the
        # matched main-game player's.
        blank = standouts["blanks"][0]
        assert (blank["name"], blank["draft_rank"], blank["points_per_game"], blank["minutes"]) == ("P5", 595, 5.0, 90)
        assert blank["text"] == "P5 managed 2 points for A B."
        assert standouts["blanks_order"] == "draft_rank"
        row = data["managers"][0]["squad"][4]
        assert (row["draft_rank"], row["points_per_game"], row["appearances"]) == (595, 5.0, 10)

    @pytest.mark.parametrize("placeholder", [0, -3, True, "2"])
    async def test_a_rank_that_is_not_a_positive_int_is_no_rank(self, placeholder: Any):
        """A 0 or a bool would sort ahead of rank 1 and print as "pre-season
        draft rank 0"; it is treated as the missing rank it stands for."""
        data = await self._collect_draft(rank_of=lambda i: placeholder if i == 5 else 600 - i)

        assert data["managers"][0]["squad"][4]["draft_rank"] is None


class TestDraftStandoutBlanks:
    """Draft blanks are ordered by what the manager could expect of the
    player, not by the lowest score: FPL's pre-season draft rank before GW19,
    the season's points per game from it (#455). On GW6's real data the
    lowest scores were squad fillers (Bentancur, Struijk, Aina) and the
    best-ranked blanks were Fernandes, Saka and Gabriel."""

    def _league(self, gameweek: int):
        managers = _standout_league(
            ("Alice", [_standout_row(
                "B.Fernandes", 1, 1, club="Manchester United", minutes=90,
                draft_rank=2, ppg=5.3, appearances=18,
            )]),
            ("Bob", [_standout_row(
                "Saka", 2, 2, minutes=90, draft_rank=3, ppg=5.7, appearances=18,
            )]),
            ("Cam", [_standout_row(
                "Bentancur", -2, 3, club="Tottenham Hotspur", minutes=11,
                draft_rank=155, ppg=2.1, appearances=15,
            )]),
            ("Dan", [_standout_row(
                "Gabriel", 2, 4, minutes=90, draft_rank=7, ppg=4.5, appearances=18,
            )]),
            # A cameo with a big average and too few games behind it.
            ("Eve", [_standout_row(
                "Nwaneri", 1, 5, minutes=20, draft_rank=None, ppg=8.0, appearances=2,
            )]),
        )
        return _standouts(managers, fpl_format="draft", gameweek=gameweek)

    def test_before_gw19_the_best_ranked_blanks_come_first(self):
        standouts = self._league(STANDOUT_PPG_FROM_GW - 1)

        assert standouts["blanks_order"] == "draft_rank"
        assert [p["name"] for p in standouts["blanks"]] == ["B.Fernandes", "Saka", "Gabriel"]
        assert standouts["blanks"][0]["text"] == (
            "B.Fernandes (Manchester United) managed 1 point for Alice."
        )
        assert standouts["blanks_omitted"] == 0

    def test_an_unranked_blank_is_not_listed_under_the_ranked_heading(self):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Unranked", 0, 1, minutes=90)]),
            ("Bob", [_standout_row("Ranked", 2, 2, minutes=90, draft_rank=600)]),
        ), fpl_format="draft")

        assert [p["name"] for p in standouts["blanks"]] == ["Ranked"]

    def test_from_gw19_the_season_average_takes_over(self):
        standouts = self._league(STANDOUT_PPG_FROM_GW)

        assert standouts["blanks_order"] == "points_per_game"
        assert [p["name"] for p in standouts["blanks"]] == ["Saka", "B.Fernandes", "Gabriel"]
        assert standouts["blanks"][0]["text"] == (
            "Saka (Arsenal) managed 2 points for Bob."
        )

    def test_an_average_behind_too_few_appearances_is_not_listed(self):
        """With one qualifying blank and two without, the list runs short
        rather than fill up with cameos under a heading that calls it the
        season's best, the cameo's 8.0 printed among them."""
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Saka", 2, 1, minutes=90, ppg=5.7, appearances=18)]),
            ("Bob", [_standout_row("Cameo", 1, 2, minutes=20, ppg=8.0, appearances=2)]),
            ("Cam", [_standout_row("Fringe", 1, 3, minutes=30, ppg=3.0, appearances=4)]),
        ), fpl_format="draft", gameweek=STANDOUT_PPG_FROM_GW)

        assert [p["name"] for p in standouts["blanks"]] == ["Saka"]
        assert not any("8.0" in line for line in standout_players_lines(standouts)[1])

    @pytest.mark.parametrize(("fpl_format", "gameweek", "live", "order"), [
        ("classic", 1, True, "managers"),
        ("classic", STANDOUT_PPG_FROM_GW, True, "managers"),
        ("draft", 1, True, "draft_rank"),
        ("draft", STANDOUT_PPG_FROM_GW - 1, True, "draft_rank"),
        ("draft", STANDOUT_PPG_FROM_GW, True, "points_per_game"),
        ("draft", 38, True, "points_per_game"),
        # A replay reads today's average, which counts gameweeks after the
        # one recapped, so it keeps the rank that never changes.
        ("draft", 22, False, "draft_rank"),
    ])
    def test_the_switch_falls_on_the_halfway_gameweek_of_a_live_recap(
        self, fpl_format: str, gameweek: int, live: bool, order: str,
    ):
        assert standout_blanks_order(fpl_format, gameweek, live=live) == order

    def test_the_halfway_gameweek_is_its_own_policy(self):
        """Pinned as a number, not as the chip split it happens to equal:
        draft has no chips, and a move of the chip rule must not move this."""
        assert STANDOUT_PPG_FROM_GW == 19

    @pytest.mark.parametrize(("gameweek", "heading"), [
        (STANDOUT_PPG_FROM_GW - 1, "Blanks among the best-ranked players, by FPL's pre-season player ranking"),
        (STANDOUT_PPG_FROM_GW, "Blanks among this season's best performers, by points per game"),
    ])
    def test_the_heading_says_what_the_blanks_are_ordered_by(self, gameweek: int, heading: str):
        assert standout_blanks_heading(self._league(gameweek)) == heading

    @pytest.mark.parametrize("gameweek", [STANDOUT_PPG_FROM_GW - 1, STANDOUT_PPG_FROM_GW])
    def test_a_draft_blank_reads_like_a_classic_one(self, gameweek: int):
        """The ordering stays, in the heading and the payload, but no line
        states it: given "pre-season draft rank 2", the editorial wrote it up
        as this league's "number 2 draft pick" (#455, vault A/B round 1)."""
        standouts = self._league(gameweek)
        _, blank_lines = standout_players_lines(standouts)

        assert blank_lines
        for line in blank_lines:
            assert not re.search(r"(?i)\b(?:draft|rank\w*|averag\w*|pick)\b", line), line
        assert (standouts["blanks"][0]["draft_rank"], standouts["blanks"][0]["points_per_game"]) != (None, None)

    @pytest.mark.parametrize("gameweek", [STANDOUT_PPG_FROM_GW - 1, STANDOUT_PPG_FROM_GW])
    def test_no_heading_calls_the_ranking_a_draft(self, gameweek: int):
        """One FPL ranking shared by every draft league, never this one's draft."""
        assert "draft" not in standout_blanks_heading(self._league(gameweek)).lower()

    def test_a_top_scorer_carries_no_rank_or_average(self):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Semenyo", 15, 1, club="Bournemouth", minutes=90, draft_rank=40, ppg=6.0)]),
        ), fpl_format="draft")

        assert standouts["top_scorers"][0]["text"] == "Semenyo (Bournemouth) scored 15 points for Alice."


class TestStandoutBlankMinutes:
    """A blank says how long he played when it was under the hour, the line
    FPL's appearance points step at, and that he did not play at all when he
    did not -- a starter nobody could cover, which is the manager's story."""

    @pytest.mark.parametrize(("points", "minutes", "expected"), [
        (0, 0, "Salah (Liverpool) did not play for Alice."),
        (1, 1, "Salah (Liverpool) managed 1 point in 1 minute for Alice."),
        (1, 27, "Salah (Liverpool) managed 1 point in 27 minutes for Alice."),
        (1, 59, "Salah (Liverpool) managed 1 point in 59 minutes for Alice."),
        (2, 60, "Salah (Liverpool) managed 2 points for Alice."),
        (2, None, "Salah (Liverpool) managed 2 points for Alice."),
    ])
    def test_minutes_on_a_blank(self, points: int, minutes: int | None, expected: str):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Salah", points, 1, club="Liverpool", minutes=minutes)]),
        ))

        assert standouts["blanks"][0]["text"] == expected

    def test_a_red_card_keeps_its_minutes(self):
        standouts = _standouts(_standout_league(
            ("Hal", [_standout_row("Wirtz", -1, 1, club="Liverpool", minutes=56, red_cards=1)]),
        ))

        assert standouts["blanks"][0]["text"] == "Wirtz (Liverpool) was sent off and managed -1 point in 56 minutes for Hal."

    def test_a_top_scorer_never_carries_minutes(self):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Isak", 6, 1, club="Liverpool", minutes=30)]),
        ))

        assert standouts["top_scorers"][0]["text"] == "Isak (Liverpool) scored 6 points for Alice."


class TestStandoutPartialCohort:
    """A line only says "all" or "everyone but" of a whole league: a manager
    the recap could not fetch may have had the player."""

    @pytest.mark.parametrize(("counted", "expected"), [
        (set(_EIGHT), "for 8 managers."),
        (set(_EIGHT) - {"Gus", "Hal"}, "for 6 managers."),
        ({"Alice", "Bob"}, "for Alice and Bob."),
    ])
    def test_a_partial_league_counts_only_whom_it_can_see(self, counted: set[str], expected: str):
        standouts = _standouts(_haaland_league(counted), whole_league=False)

        assert standouts["whole_league"] is False
        assert standouts["top_scorers"][0]["text"] == f"Haaland (Manchester City) scored 17 points {expected}"


class TestStandoutManagerIdentity:
    def test_managers_sharing_a_display_name_are_counted_not_named_twice(self):
        standouts = _standouts(_standout_league(
            ("Alex", [_standout_row("Haaland", 17, 223)]),
            ("Alex", [_standout_row("Haaland", 17, 223)]),
        ))

        row = standouts["top_scorers"][0]
        assert row["text"] == "Haaland (Arsenal) scored 17 points for 2 managers."
        # Kept apart, and in key order, where the payload can say who.
        assert [m["manager_key"] for m in row["counted_for"]] == [1, 2]

    def test_a_manager_is_named_once_however_many_rows_share_a_code_less_key(self):
        standouts = _standouts(_standout_league(
            ("Alice", [_standout_row("Dupe", 12, 1), _standout_row("Dupe", 12, 1)]),
        ))
        for row in standouts["top_scorers"]:
            row["code"] = None

        assert standouts["top_scorers"][0]["text"] == "Dupe (Arsenal) scored 12 points for Alice."

    def test_code_less_players_sharing_a_name_and_club_stay_two(self):
        alice_row = _standout_row("Smith", 12, 1)
        bob_row = _standout_row("Smith", 7, 2)
        for row, main_id in ((alice_row, 501), (bob_row, 502)):
            row["code"] = None
            row["main_id"] = main_id

        standouts = _standouts(_standout_league(("Alice", [alice_row]), ("Bob", [bob_row])))

        assert [(p["points"], p["id"]) for p in standouts["top_scorers"]] == [(12, 501), (7, 502)]


# ---------------------------------------------------------------------------
# How a standout draft player came to his manager (issue #458)
# ---------------------------------------------------------------------------


def _acquired(kind: str, gameweek: int = 1, **kwargs: Any) -> RecapAcquisition:
    """How a player came to his manager, as the draft collector records it."""
    pick = kind == "draft"
    base: dict[str, Any] = {
        "kind": kind, "gameweek": gameweek,
        "draft_id": 7 if pick else None, "draft_gameweek": gameweek if pick else None, "redraft": False,
        "round_unknown": False, "round": 1 if pick else None, "pick": 1 if pick else None, "overall_pick": 1 if pick else None,
        "auto_pick": False if pick else None, "drafter": None, "from_manager": None,
    }
    return RecapAcquisition(**{**base, **kwargs})  # type: ignore[typeddict-item]


def _came_by(row: RecapManagerPlayer, acquisition: RecapAcquisition | None) -> RecapManagerPlayer:
    row["acquisition"] = acquisition
    return row


class TestStandoutAcquisitionSentences:
    """Every draft line says how the player came to the manager he scored
    for, as a clause on the manager's name -- the data holds no one's gender,
    so no possessive pronoun -- and reads exactly as it did where the league's
    history could not say."""

    def _scorer(self, acquisition: RecapAcquisition | None, **kwargs: Any) -> str:
        managers = _standout_league(("Alice", [
            _came_by(_standout_row("Haaland", 17, 223, club="Manchester City", **kwargs), acquisition),
        ]))
        return _standouts(managers, fpl_format="draft")["top_scorers"][0]["text"]

    @pytest.mark.parametrize(("acquisition", "clause"), [
        (_acquired("draft"), "who drafted him in the first round"),
        (_acquired("draft", round=12), "who drafted him in the 12th round"),
        (_acquired("draft", round=1, auto_pick=True), "who had him as an auto-pick in the first round"),
        (
            _acquired("draft", gameweek=20, redraft=True, draft_gameweek=20),
            "who drafted him in the first round of the GW20 redraft",
        ),
        # The picks are lost, so the round is not guessed at.
        (
            _acquired("draft", round_unknown=True, round=None, pick=None, overall_pick=None, auto_pick=None),
            "who drafted him",
        ),
        (
            _acquired(
                "draft", gameweek=20, redraft=True, draft_gameweek=20,
                round_unknown=True, round=None, pick=None, overall_pick=None, auto_pick=None,
            ),
            "who drafted him in the GW20 redraft",
        ),
        (_acquired("waiver", 4), "who picked him up on waivers in GW4"),
        (_acquired("free_agent", 9), "who signed him as a free agent in GW9"),
        (
            _acquired("trade", 7, from_manager={"manager_key": 2, "manager_name": "Cam"}),
            "who acquired him from Cam in a GW7 trade",
        ),
        (_acquired("trade", 7), "who acquired him in a GW7 trade"),
    ], ids=[
        "pick", "late pick", "auto pick", "redraft pick", "pick, round lost", "redraft pick, round lost",
        "waiver", "free agent", "trade", "trade from an unnamed entry",
    ])
    def test_a_scorer_says_how_he_came_to_the_manager_he_scored_for(
        self, acquisition: RecapAcquisition, clause: str,
    ):
        assert self._scorer(acquisition) == f"Haaland (Manchester City) scored 17 points for Alice, {clause}."

    def test_a_blank_reads_the_same_way(self):
        managers = _standout_league(("Dan", [
            _came_by(
                _standout_row("B.Fernandes", 1, 8, club="Manchester United", minutes=90, draft_rank=2),
                _acquired("draft"),
            ),
            _came_by(
                _standout_row("Salah", 0, 9, club="Liverpool", minutes=0, draft_rank=3),
                _acquired("waiver", 4),
            ),
        ]))

        blanks = _standouts(managers, fpl_format="draft")["blanks"]

        assert [p["text"] for p in blanks] == [
            "B.Fernandes (Manchester United) managed 1 point for Dan, who drafted him in the first round.",
            "Salah (Liverpool) did not play for Dan, who picked him up on waivers in GW4.",
        ]

    def test_a_player_on_the_bench_names_the_owner_who_left_him_there(self):
        managers = _standout_league(("Bob", [
            _came_by(
                _standout_row("Groß", 13, 3, contributed=False, club="Brighton"), _acquired("free_agent", 9),
            ),
        ]))

        row = _standouts(managers, fpl_format="draft")["top_scorers"][0]

        assert row["text"] == (
            "Groß (Brighton) scored 13 points, but Bob, who signed him as a free agent in GW9, left him on the bench."
        )

    def test_a_trade_does_not_name_a_manager_whose_name_another_shares(self):
        """"From Alex" tells a reader nothing when two managers are Alex."""
        alex = {"manager_key": 2, "manager_name": "Alex"}
        managers = _standout_league(
            ("Dan", [_came_by(_standout_row("Wissa", 11, 5, club="Newcastle United"), _acquired("trade", 7, from_manager=alex))]),
            ("Alex", []),
            ("Alex", []),
        )

        text = _standouts(managers, fpl_format="draft")["top_scorers"][0]["text"]

        assert text == "Wissa (Newcastle United) scored 11 points for Dan, who acquired him in a GW7 trade."

    def test_a_namesake_whose_picks_were_not_fetched_still_keeps_the_name_out_of_the_line(self):
        """The counterparty comes from the league's entries, so the names that
        make "from Alex" ambiguous are the league's, not just the fetched ones."""
        alex = {"manager_key": 2, "manager_name": "Alex"}
        managers = _standout_league(
            ("Dan", [_came_by(_standout_row("Wissa", 11, 5, club="Newcastle United"), _acquired("trade", 7, from_manager=alex))]),
            ("Alex", []),
        )

        names = ["Dan", "Alex", "Alex"]
        unaware = _standouts(managers, fpl_format="draft")["top_scorers"][0]["text"]
        aware = find_standout_players(
            managers, fpl_format="draft", gameweek=7, live=True, whole_league=False, league_names=names,
        )["top_scorers"][0]["text"]

        assert unaware == "Wissa (Newcastle United) scored 11 points for Dan, who acquired him from Alex in a GW7 trade."
        assert aware == "Wissa (Newcastle United) scored 11 points for Dan, who acquired him in a GW7 trade."

    def test_a_line_the_history_could_not_label_reads_as_it_always_has(self):
        assert self._scorer(None) == "Haaland (Manchester City) scored 17 points for Alice."

    def test_every_draft_row_carries_the_acquisition_key_and_a_classic_row_never_does(self):
        draft = _standouts(_standout_league(("Alice", [_standout_row("Haaland", 17, 223)])), fpl_format="draft")
        classic = _standouts(_standout_league(("Alice", [
            _came_by(_standout_row("Haaland", 17, 223), _acquired("draft")),
        ])))

        assert draft["top_scorers"][0]["acquisition"] is None
        assert "acquisition" not in classic["top_scorers"][0]
        assert classic["top_scorers"][0]["text"] == "Haaland (Arsenal) scored 17 points for Alice."

    def test_the_json_row_carries_the_acquisition_beside_the_player_ids(self):
        picked = _acquired(
            "draft", 1, round=3, pick=2, overall_pick=17, draft_id=7, draft_gameweek=1,
            drafter={"manager_key": 1, "manager_name": "Alice"},
        )
        row = _standouts(
            _standout_league(("Alice", [_came_by(_standout_row("Haaland", 17, 223), picked)])), fpl_format="draft",
        )["top_scorers"][0]

        assert (row["id"], row["code"]) == (1223, 223)
        assert row["acquisition"] == {
            "kind": "draft", "gameweek": 1, "draft_id": 7, "draft_gameweek": 1, "redraft": False,
            "round_unknown": False, "round": 3, "pick": 2, "overall_pick": 17, "auto_pick": False,
            "drafter": {"manager_key": 1, "manager_name": "Alice"}, "from_manager": None,
        }

    def test_the_ordering_is_unchanged_and_the_rank_still_stays_out_of_the_prose(self):
        managers = _standout_league(
            ("Alice", [_came_by(_standout_row("Saka", 2, 1, minutes=90, draft_rank=3), _acquired("draft"))]),
            ("Bob", [_came_by(_standout_row("Fernandes", 1, 2, minutes=90, draft_rank=2), _acquired("waiver", 3))]),
        )

        standouts = _standouts(managers, fpl_format="draft")

        assert [p["name"] for p in standouts["blanks"]] == ["Fernandes", "Saka"]
        for line in standout_players_lines(standouts)[1]:
            assert not re.search(r"(?i)\b(?:rank\w*|averag\w*)\b", line), line


class TestStandoutAcquisitionNote:
    def _data(self, acquisition: RecapAcquisition | None, fpl_format: str = "draft"):
        managers = _standout_league(("Alice", [
            _came_by(_standout_row("Haaland", 17, 1, club="Manchester City"), acquisition),
        ]))
        data = _make_recap_data(managers=managers)
        data["fpl_format"] = fpl_format
        data["standout_players"] = _standouts(managers, fpl_format=fpl_format)
        return data

    def test_the_note_says_what_the_provenance_is_when_a_line_carries_it(self):
        text = format_recap_standout_players_context(self._data(_acquired("draft")))

        assert text.splitlines()[0] == (
            "Each player's own points this gameweek, among the players in this league's squads. "
            "A blank is 2 points or fewer. Where a line says how a player came to the manager it names, "
            "that is the latest way he joined that manager's squad as of this gameweek: a draft pick, "
            "a waiver or free-agent claim, or a trade."
        )
        assert "- Haaland (Manchester City) scored 17 points for Alice, who drafted him in the first round." in text

    def test_the_section_still_gives_no_instructions(self):
        text = format_recap_standout_players_context(self._data(_acquired("waiver", 4)))

        assert not re.search(r"(?i)\b(?:never|do not|don't|must|only)\b", text)

    def test_the_note_is_as_it_was_where_no_line_says_how(self):
        text = format_recap_standout_players_context(self._data(None))

        assert text.splitlines()[0] == (
            "Each player's own points this gameweek, among the players in this league's squads. "
            "A blank is 2 points or fewer."
        )

    def test_the_system_prompt_is_not_touched_by_the_section(self):
        """The section owns its facts, so the model's rules need no entry for it."""
        system, _ = get_recap_synthesis_prompt(
            gw=7, league_name="Test League", fpl_format="draft",
            awards_text="", standings_text="", fines_text="",
            standout_players_text=format_recap_standout_players_context(self._data(_acquired("draft"))),
        )

        assert "Standout" not in system and "provenance" not in system.lower()


_SEASON = "2026-27"


class TestCollectorDraftAcquisitions:
    """The draft collector, asked for it, sets each squad row's acquisition
    from the league's own history and holds that history to the squads it
    fielded; unasked, as for a backfill's replays, it reads nothing extra."""

    _DRAFT = 70
    _DRAFTED_AT = "2026-08-10T12:00:00Z"

    def _rows(self) -> list[dict[str, Any]]:
        return [
            {"element": 900 + i, "entry": 1, "round": i, "pick": 1, "index": i, "draft": self._DRAFT, "was_auto": False}
            for i in range(1, 16)
        ]

    def _client(
        self,
        *,
        owners: dict[int, int | None] | None = None,
        transactions: list[dict[str, Any]] | None = None,
        trades: Any = (),
        picks: list[int] | None = None,
    ) -> MagicMock:
        """One manager (league entry 10, entry 1) who drafted P1..P15, the
        draft's id 70, drafted in order: pick i in round i."""
        draft_players = [
            make_draft_player(id=900 + i, code=100 + i, web_name=f"P{i}", team=1, element_type=3)
            for i in range(1, 17)
        ]
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value={
            "league": {"name": "Draft League", "drafts": [
                {"id": self._DRAFT, "event": 1, "draft_completed": self._DRAFTED_AT},
            ]},
            "standings": [{"league_entry": 10, "event_total": 0, "total": 0}],
            "league_entries": [{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        })
        client.get_bootstrap_static = AsyncMock(return_value={"elements": draft_players})
        client.get_game_state = AsyncMock(return_value={"current_event": 5, "next_event": 6})
        client.get_league_transactions = AsyncMock(return_value={"transactions": transactions or []})
        held = owners if owners is not None else {900 + i: 1 for i in range(1, 16)}
        client.get_draft_choices = AsyncMock(return_value={
            "choices": self._rows(),
            "element_status": [{"element": 900 + i, "owner": held.get(900 + i)} for i in range(1, 17)],
            "idle": [],
        })
        if isinstance(trades, Exception):
            client.get_league_trades = AsyncMock(side_effect=trades)
        else:
            client.get_league_trades = AsyncMock(return_value={"trades": list(trades)})
        in_squad = picks or [900 + i for i in range(1, 16)]
        client.get_entry_picks = AsyncMock(return_value={
            "picks": [{"element": element, "position": n} for n, element in enumerate(in_squad, start=1)],
            "subs": [],
        })
        return client

    async def _collect(self, client: MagicMock, *, gw: int = 4, provenance_season: str | None = _SEASON):
        main_players = [make_player(id=i, code=100 + i, web_name=f"P{i}", team_id=1) for i in range(1, 17)]
        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            return await collect_draft_recap_data(
                {"fpl": {"draft_league_id": 1}}, gw=gw, live_stats={}, players=main_players, teams={},
                is_live_gw=False, provenance_season=provenance_season,
            )

    async def test_asked_for_it_sets_every_squad_row_from_the_drafts_picks(self):
        data = await self._collect(self._client())

        assert "draft_provenance_problem" not in data
        squad = data["managers"][0]["squad"]
        fifth = next(p for p in squad if p["name"] == "P5")
        assert fifth["draft_element"] == 905
        assert fifth["acquisition"] == {
            "kind": "draft", "gameweek": 1, "draft_id": self._DRAFT, "draft_gameweek": 1, "redraft": False,
            "round_unknown": False, "round": 5, "pick": 1, "overall_pick": 5, "auto_pick": False,
            "drafter": {"manager_key": 10, "manager_name": "A B"}, "from_manager": None,
        }
        assert all(p["acquisition"] is not None for p in squad)

    async def test_the_standout_lines_then_say_where_the_player_came_from(self):
        data = await self._collect(self._client())
        for row in data["managers"][0]["squad"]:
            row["points"] = 9 if row["name"] == "P5" else 0

        standouts = find_standout_players(
            data["managers"], fpl_format="draft", gameweek=4, live=False, whole_league=True,
        )

        assert standouts["top_scorers"][0]["text"] == (
            "P5 (???) scored 9 points for A B, who drafted him in the fifth round."
        ) or standouts["top_scorers"][0]["text"].endswith("who drafted him in the fifth round.")

    async def test_unasked_it_reads_nothing_extra_and_sets_nothing(self):
        """A backfill's replays are never given the flag, so they never pay for it."""
        client = self._client()

        data = await self._collect(client, provenance_season=None)

        client.get_draft_choices.assert_not_awaited()
        client.get_league_trades.assert_not_awaited()
        assert "draft_provenance_problem" not in data
        assert all("acquisition" not in p and "draft_element" not in p for p in data["managers"][0]["squad"])

    async def test_a_later_pickup_is_the_waiver_with_its_gameweek_and_an_earlier_week_shows_its_own_owner(self):
        """P15 is released for P16 on waivers in GW4: a GW4 recap labels the
        pickup, and a GW3 replay of the same league still shows P15 as the pick."""
        waiver = {
            "id": 1, "entry": 1, "element_in": 916, "element_out": 915, "event": 4,
            "added": "2026-09-01T10:00:00Z", "kind": "w", "result": "a", "index": 1, "priority": 1,
        }
        owners = {**{900 + i: 1 for i in range(1, 15)}, 916: 1}
        in_gw4 = [900 + i for i in range(1, 15)] + [916]

        at_gw4 = await self._collect(self._client(owners=owners, transactions=[waiver], picks=in_gw4), gw=4)
        at_gw3 = await self._collect(self._client(owners=owners, transactions=[waiver]), gw=3)

        picked_up = next(p for p in at_gw4["managers"][0]["squad"] if p["name"] == "P16")
        assert picked_up["acquisition"] is not None
        assert (picked_up["acquisition"]["kind"], picked_up["acquisition"]["gameweek"]) == ("waiver", 4)
        earlier = next(p for p in at_gw3["managers"][0]["squad"] if p["name"] == "P15")
        assert earlier["acquisition"] is not None
        assert (earlier["acquisition"]["kind"], earlier["acquisition"]["round"]) == ("draft", 15)

    async def test_a_history_that_disagrees_with_the_ownership_list_labels_nobody(self):
        owners = {**{900 + i: 1 for i in range(1, 15)}, 915: 2}

        data = await self._collect(self._client(owners=owners))

        assert "owned differently from FPL's own ownership list" in data["draft_provenance_problem"]
        assert all("acquisition" not in p for p in data["managers"][0]["squad"])

    async def test_a_failed_trades_read_labels_nobody_whatever_the_ownership_list_says(self):
        """Not only when a trade shows up as a disagreement (P15 traded away):
        a trade and its reversal leave no trace, so an unread log cannot be
        vouched for either way."""
        owners = {**{900 + i: 1 for i in range(1, 15)}, 915: 2}

        for held in (owners, None):
            data = await self._collect(self._client(owners=held, trades=httpx.ConnectError("down")))

            assert "trades could not be read" in data["draft_provenance_problem"]
            assert all("acquisition" not in p for p in data["managers"][0]["squad"])

    async def test_the_choices_the_replay_read_ride_on_the_data_for_the_snapshot(self):
        client = self._client()

        data = await self._collect(client)

        assert data["draft_choices"] == await client.get_draft_choices(1)

    async def test_no_choices_ride_along_when_provenance_was_not_collected_or_the_read_failed(self):
        unasked = await self._collect(self._client(), provenance_season=None)
        failed_client = self._client()
        failed_client.get_draft_choices = AsyncMock(side_effect=httpx.ConnectError("down"))
        failed = await self._collect(failed_client)

        assert "draft_choices" not in unasked
        assert "draft_choices" not in failed

    async def test_league_details_without_a_drafts_list_say_so_rather_than_that_no_draft_was_held(self):
        client = self._client()
        client.get_league_details = AsyncMock(return_value={
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 0, "total": 0}],
            "league_entries": [{"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"}],
        })

        data = await self._collect(client)

        assert "list of drafts could not be read" in data["draft_provenance_problem"]
        assert "no draft had been held" not in data["draft_provenance_problem"]

    def _picks_by_gameweek(self, unreadable_at: int | None = None) -> Any:
        """get_entry_picks that answers every gameweek alike, bar one that fails."""
        squad = [900 + i for i in range(1, 16)]

        async def picks(entry: int, gameweek: int, *_: Any) -> dict[str, Any]:
            if gameweek == unreadable_at:
                raise httpx.ConnectError("down")
            return {"picks": [{"element": e, "position": n} for n, e in enumerate(squad, start=1)], "subs": []}

        return AsyncMock(side_effect=picks)

    def _feed_without_the_picks(self) -> MagicMock:
        """What a league with a later draft scheduled gets: the ownership list,
        and no picks for the draft that was held."""
        client = self._client()
        client.get_draft_choices = AsyncMock(return_value={
            "choices": [],
            "element_status": [{"element": 900 + i, "owner": 1 if i < 16 else None} for i in range(1, 17)],
            "idle": [],
        })
        return client

    async def test_picks_held_by_neither_source_are_rebuilt_from_the_squads_without_rounds(self):
        """The configured league's case: a redraft was scheduled before the first
        snapshot, so the feed moved on and nothing kept the picks."""
        data = await self._collect(self._feed_without_the_picks())

        assert "draft_provenance_problem" not in data
        assert "held by neither the draft API nor a saved snapshot" in data["draft_provenance_notice"]
        assert "not in which round" in data["draft_provenance_notice"]
        assert "draft-70.json" in data["draft_provenance_notice"]
        fifth = next(p for p in data["managers"][0]["squad"] if p["name"] == "P5")
        assert fifth["acquisition"] == {
            "kind": "draft", "gameweek": 1, "draft_id": self._DRAFT, "draft_gameweek": 1, "redraft": False,
            "round_unknown": True, "round": None, "pick": None, "overall_pick": None, "auto_pick": None,
            "drafter": {"manager_key": 10, "manager_name": "A B"}, "from_manager": None,
        }

    async def test_the_lines_then_say_who_drafted_him_with_no_round(self):
        data = await self._collect(self._feed_without_the_picks())
        for row in data["managers"][0]["squad"]:
            row["points"] = 9 if row["name"] == "P5" else 0

        standouts = find_standout_players(
            data["managers"], fpl_format="draft", gameweek=4, live=False, whole_league=True,
        )

        assert standouts["top_scorers"][0]["text"].endswith("for A B, who drafted him.")

    async def test_a_pre_gameweek_waiver_is_not_drafted_by_the_squad_it_joined(self):
        """P16 came in on a GW1 waiver for P15, so P16 was claimed and P15 drafted."""
        waiver = {
            "id": 1, "entry": 1, "element_in": 916, "element_out": 915, "event": 1,
            "added": "2026-08-11T10:00:00Z", "kind": "w", "result": "a", "index": 1, "priority": 1,
        }
        owners = {**{900 + i: 1 for i in range(1, 15)}, 916: 1}
        in_squad = [900 + i for i in range(1, 15)] + [916]
        client = self._feed_without_the_picks()
        client.get_league_transactions = AsyncMock(return_value={"transactions": [waiver]})
        client.get_draft_choices = AsyncMock(return_value={
            "choices": [], "element_status": [{"element": 900 + i, "owner": owners.get(900 + i)} for i in range(1, 17)],
            "idle": [],
        })
        client.get_entry_picks = AsyncMock(return_value={
            "picks": [{"element": e, "position": n} for n, e in enumerate(in_squad, start=1)], "subs": [],
        })

        data = await self._collect(client)

        assert "draft_provenance_problem" not in data
        pickup = next(p for p in data["managers"][0]["squad"] if p["name"] == "P16")
        assert pickup["acquisition"] is not None
        assert (pickup["acquisition"]["kind"], pickup["acquisition"]["gameweek"]) == ("waiver", 1)
        assert pickup["acquisition"]["round_unknown"] is False

    async def test_squads_that_cannot_be_read_at_the_drafts_gameweek_leave_the_lines_as_they_were(self):
        client = self._feed_without_the_picks()
        client.get_entry_picks = self._picks_by_gameweek(unreadable_at=1)

        data = await self._collect(client)

        assert "squads of 1 manager could not be read" in data["draft_provenance_problem"]
        assert "draft_provenance_notice" not in data
        assert all("acquisition" not in p for p in data["managers"][0]["squad"])

    async def test_a_failed_trades_read_refuses_the_rebuild(self):
        """The two checks cannot catch a missing GW1 trade when the start is derived
        from the same logs, so a trades log that did not come back refuses it."""
        client = self._feed_without_the_picks()
        client.get_league_trades = AsyncMock(side_effect=httpx.ConnectError("down"))

        data = await self._collect(client)

        assert "the league's trades could not be read" in data["draft_provenance_problem"]
        assert all("acquisition" not in p for p in data["managers"][0]["squad"])

    async def test_a_snapshot_appearing_later_promotes_the_same_draft_to_known_rounds(self):
        from fpl_cli.services.league_history_drafts import completed_drafts, snapshot_draft_choices

        before = await self._collect(self._feed_without_the_picks())
        held = {"id": self._DRAFT, "event": 1, "draft_completed": self._DRAFTED_AT}
        snapshot_draft_choices(_SEASON, 1, completed_drafts([held]), {"choices": self._rows(), "idle": []})

        after = await self._collect(self._feed_without_the_picks())

        fifth = next(p for p in after["managers"][0]["squad"] if p["name"] == "P5")
        assert "draft_provenance_notice" not in after and "draft_provenance_problem" not in after
        assert fifth["acquisition"] is not None
        assert (fifth["acquisition"]["round_unknown"], fifth["acquisition"]["round"]) == (False, 5)
        assert "draft_provenance_notice" in before

    async def test_a_later_recap_rebuilds_from_the_kept_squads_without_reading_them(self):
        """#472: the GW1 squads are read once, and every recap after rebuilds from that copy."""
        clients = [self._feed_without_the_picks() for _ in range(2)]
        for client in clients:
            client.get_entry_picks = self._picks_by_gameweek()

        for client in clients:
            data = await self._collect(client)
            assert "draft_provenance_problem" not in data
            assert "not in which round" in data["draft_provenance_notice"]

        reads_at_the_draft = [
            sum(1 for call in client.get_entry_picks.await_args_list if call.args[1] == 1) for client in clients
        ]
        assert reads_at_the_draft == [1, 0]

    async def test_a_squad_the_replay_puts_elsewhere_labels_nobody(self):
        """P16 is in the fetched squad but, by the history, nobody's."""
        in_squad = [900 + i for i in range(1, 15)] + [916]

        data = await self._collect(self._client(picks=in_squad))

        assert "stopped at GW4" in data["draft_provenance_problem"]

    async def test_a_draft_the_feed_no_longer_serves_is_read_from_its_snapshot(self):
        """A redraft is scheduled: the feed serves its (empty) picks, and the held
        draft's come from the file the recap saved while the feed had them."""
        from fpl_cli.services.league_history_drafts import completed_drafts, snapshot_draft_choices

        held = {"id": self._DRAFT, "event": 1, "draft_completed": self._DRAFTED_AT}
        snapshot_draft_choices(_SEASON, 1, completed_drafts([held]), {"choices": self._rows(), "idle": []})
        client = self._client()
        client.get_draft_choices = AsyncMock(return_value={
            "choices": [],
            "element_status": [{"element": 900 + i, "owner": 1 if i < 16 else None} for i in range(1, 17)],
            "idle": [],
        })

        data = await self._collect(client)

        assert "draft_provenance_problem" not in data
        fifth = next(p for p in data["managers"][0]["squad"] if p["name"] == "P5")
        assert fifth["acquisition"] is not None
        assert (fifth["acquisition"]["kind"], fifth["acquisition"]["round"]) == ("draft", 5)


# ---------------------------------------------------------------------------
# Naming the forfeit (#503)
# ---------------------------------------------------------------------------

_PINT = "Pint on video"
_PHRASINGS = ["pint of shame", "penance pint", "one for the group chat"]


class TestRulingNamesTheForfeitsThatFired:
    """Which forfeits the editorial is told about, and in whose words."""

    @staticmethod
    def _managers(*points):
        squad = [_make_squad_player(name=f"P{i}") for i in range(11)]
        return [
            _make_manager(name=name, gw_points=pts, entry_id=i + 1, squad=squad)
            for i, (name, pts) in enumerate(zip(("Alice", "Bob", "Cam"), points))
        ]

    def test_nothing_fined_is_nothing_to_name(self):
        settings = {"fines": {"classic": [{"type": "last-place", "penalty": _PINT}]}}
        assert evaluate_league_fines([], settings, "classic").penalties == []

    def test_a_fired_rule_s_forfeit_carries_the_block_phrasings(self):
        settings = {"fines": {
            "penalty_phrasings": _PHRASINGS,
            "classic": [{"type": "last-place", "penalty": _PINT}],
        }}
        ruling = evaluate_league_fines(self._managers(80, 20), settings, "classic")
        assert ruling.penalties == [{"penalty": _PINT, "phrasings": _PHRASINGS}]

    def test_without_phrasings_the_forfeit_is_still_named(self):
        settings = {"fines": {"classic": [{"type": "last-place", "penalty": _PINT}]}}
        ruling = evaluate_league_fines(self._managers(80, 20), settings, "classic")
        assert ruling.penalties == [{"penalty": _PINT, "phrasings": []}]

    def test_only_the_rule_that_fired_is_named_not_every_rule_of_its_type(self):
        """Review: two below-threshold rules, a manager under only the higher
        one, used to name both forfeits."""
        settings = {"fines": {"classic": [
            {"type": "below-threshold", "threshold": 30, "penalty": _PINT, "penalty_phrasings": ["pint of shame"]},
            {"type": "below-threshold", "threshold": 20, "penalty": "Pay the pot", "penalty_phrasings": ["the pot"]},
        ]}}
        ruling = evaluate_league_fines(self._managers(80, 25), settings, "classic")
        assert ruling.penalties == [{"penalty": _PINT, "phrasings": ["pint of shame"]}]

    def test_rules_sharing_a_forfeit_share_one_entry(self):
        settings = {"fines": {"classic": [
            {"type": "last-place", "penalty": _PINT, "penalty_phrasings": ["pint of shame", "penance pint"]},
            {"type": "below-threshold", "threshold": 30, "penalty": _PINT, "penalty_phrasings": ["the pint tax"]},
        ]}}
        ruling = evaluate_league_fines(self._managers(80, 20), settings, "classic")
        assert ruling.penalties == [
            {"penalty": _PINT, "phrasings": ["pint of shame", "penance pint", "the pint tax"]},
        ]

    @pytest.mark.parametrize("rule", [{"type": "last-place"}, {"type": "last-place", "penalty": "  "}])
    def test_a_rule_naming_no_forfeit_carries_none(self, rule):
        """Review: a blank penalty used to reach the prompt as `- "": write
        these exact words...`."""
        settings = {"fines": {"penalty_phrasings": _PHRASINGS, "classic": [rule]}}
        ruling = evaluate_league_fines(self._managers(80, 20), settings, "classic")
        assert ruling.fines
        assert ruling.penalties == []

    def test_a_phrasing_that_writes_the_forfeit_out_is_dropped(self):
        """Review: listed as a phrasing, the forfeit itself would be the
        editorial's to repeat and the check's to flag."""
        settings = {"fines": {
            "penalty_phrasings": ["pint on video", "Pints on video for the lads", "penance pint"],
            "classic": [{"type": "last-place", "penalty": _PINT}],
        }}
        ruling = evaluate_league_fines(self._managers(80, 20), settings, "classic")
        assert ruling.penalties == [{"penalty": _PINT, "phrasings": ["penance pint"]}]

    def test_the_format_s_own_rules_are_read(self):
        settings = {"fines": {
            "classic": [{"type": "last-place", "penalty": _PINT}],
            "draft": [{"type": "last-place", "penalty": "Wear the shirt"}],
        }}
        ruling = evaluate_league_fines(self._managers(80, 20), settings, "draft")
        assert [entry["penalty"] for entry in ruling.penalties] == ["Wear the shirt"]

    def test_unconfigured_fines_name_nothing(self):
        assert evaluate_league_fines(self._managers(80, 20), {}, "classic").penalties == []


class TestFinesContextCountsTheUnfined:
    """The Fines section says how many managers were fined and names the
    rest (#502). Handed only the fined, the editorial counted the rest itself,
    got it wrong with 11 of 19 fined, and called one of the 8 left "the only
    one in the league" without a forfeit."""

    @staticmethod
    def _fine(name: str, key: int | None, rule: str = "red-card") -> RecapFineResult:
        fine = RecapFineResult(manager_name=name, rule_type=rule, message=f"{name} was fined")
        if key is not None:
            fine["manager_key"] = key
        return fine

    @staticmethod
    def _managers(*names: str) -> list[RecapManagerEntry]:
        return [_make_manager(name=name, entry_id=i) for i, name in enumerate(names, start=1)]

    def test_the_count_opens_and_the_unfined_are_named(self):
        data = _make_recap_data(
            managers=self._managers("Ada", "Bob", "Cy", "Dee", "Eve"),
            # Bob's two fines are one manager fined, not two.
            fines=[
                self._fine("Bob", 2), self._fine("Bob", 2, rule="last-place"),
                self._fine("Dee", 4), self._fine("Ada", 1),
            ],
        )

        lines = format_recap_fines_context(data).splitlines()

        assert lines[0] == "Total managers fined this gameweek: 3 of 5"
        assert "Not fined this gameweek (2): Cy, Eve" in lines
        assert lines.index("Not fined this gameweek (2): Cy, Eve") > lines.index("- Ada: Ada was fined")

    def test_a_week_with_rules_and_no_fines_says_nobody_was_fined(self):
        data = _make_recap_data(managers=self._managers("Ada", "Bob", "Cy"))
        data["fine_rules_evaluated"] = ["last-place"]

        assert format_recap_fines_context(data) == "Nobody was fined this gameweek (0 of 3 managers)"

    def test_a_league_without_fine_rules_still_gets_no_section(self):
        data = _make_recap_data(managers=self._managers("Ada", "Bob"))
        data["fine_rules_evaluated"] = []

        assert format_recap_fines_context(data) == ""

    def test_a_manager_never_ruled_on_is_neither_fined_nor_cleared(self):
        """Their evaluation raised, so they may have earned a fine nobody
        recorded: listing them as clean would be the false acquittal the
        ledger already refuses (#136)."""
        data = _make_recap_data(
            managers=self._managers("Ada", "Bob", "Cy"),
            fines=[self._fine("Ada", 1)],
        )
        data["fines_ruled_manager_keys"] = [1, 2]

        text = format_recap_fines_context(data)

        assert "Total managers fined this gameweek: 1 of the 2 ruled on" in text
        assert "Not fined this gameweek (1): Bob" in text
        assert "Not ruled on this gameweek, so neither fined nor cleared (1): Cy" in text

    def test_a_week_nobody_could_be_ruled_on_acquits_nobody(self):
        data = _make_recap_data(managers=self._managers("Ada", "Bob"))
        data["fine_rules_evaluated"] = ["last-place"]
        data["fines_ruled_manager_keys"] = []

        assert format_recap_fines_context(data).splitlines() == [
            "No manager could be ruled on this gameweek, so nobody is fined or cleared",
            "Not ruled on this gameweek, so neither fined nor cleared (2): Ada, Bob",
        ]

    def test_a_fine_against_a_manager_not_collected_leaves_the_count_out(self):
        """A replayed or hand-built ruling can name a manager the recap never
        collected. Counting without them would open "nobody was fined" above
        their fine."""
        data = _make_recap_data(
            managers=self._managers("Ada", "Bob"), fines=[self._fine("Zed", 99)],
        )

        text = format_recap_fines_context(data)

        assert text.splitlines()[0] == "- Zed: Zed was fined"
        assert "Nobody" not in text
        assert "Not fined" not in text

    def test_nobody_fined_among_the_ruled_leaves_out_the_unruled(self):
        data = _make_recap_data(managers=self._managers("Ada", "Bob", "Cy"))
        data["fine_rules_evaluated"] = ["last-place"]
        data["fines_ruled_manager_keys"] = [1, 2]

        assert format_recap_fines_context(data).splitlines() == [
            "No manager ruled on was fined this gameweek (0 of 2)",
            "Not ruled on this gameweek, so neither fined nor cleared (1): Cy",
        ]

    def test_a_league_read_one_page_short_never_passes_for_the_whole_league(self):
        """Classic standings stop at one page of 50. In a bigger league the
        count covers the managers read, and says so, rather than reading as
        the league's own."""
        data = _make_recap_data(
            managers=self._managers("Ada", "Bob", "Cy"), fines=[self._fine("Ada", 1)],
        )
        data["standings_truncated"] = True
        data["league_size"] = 120

        lines = format_recap_fines_context(data).splitlines()

        assert lines[0] == "Total managers fined this gameweek: 1 of the 3 this recap read"
        assert lines[-1] == (
            "This recap read 3 of the league's 120 members, so these counts cover "
            "those 3 alone and say nothing of the other 117"
        )

    def test_nobody_fined_in_a_partly_read_league_acquits_only_those_read(self):
        data = _make_recap_data(managers=self._managers("Ada", "Bob"))
        data["fine_rules_evaluated"] = ["last-place"]
        data["standings_truncated"] = True

        assert format_recap_fines_context(data).splitlines() == [
            "Nobody this recap read was fined this gameweek (0 of 2)",
            "This recap read only 2 of the league's members, so these counts cover "
            "those 2 alone and say nothing of the rest",
        ]

    def test_a_member_whose_picks_never_came_back_is_outside_the_count(self):
        data = _make_recap_data(
            managers=self._managers("Ada", "Bob"), fines=[self._fine("Ada", 1)],
        )
        data["standings_cohort"] = [{"manager_name": n} for n in ("Ada", "Bob", "Cy")]  # type: ignore[typeddict-item]

        text = format_recap_fines_context(data)

        assert "1 of the 2 this recap read" in text
        assert "say nothing of the other 1" in text

    def test_a_whole_league_read_carries_no_scope_line(self):
        data = _make_recap_data(
            managers=self._managers("Ada", "Bob"), fines=[self._fine("Ada", 1)],
        )
        data["standings_cohort"] = [{"manager_name": n} for n in ("Ada", "Bob")]  # type: ignore[typeddict-item]

        text = format_recap_fines_context(data)

        assert "Total managers fined this gameweek: 1 of 2" in text
        assert "this recap read" not in text

    def test_a_keyless_fine_is_matched_by_name(self):
        data = _make_recap_data(
            managers=self._managers("Ada", "Bob"), fines=[self._fine("Bob", None)],
        )

        text = format_recap_fines_context(data)

        assert "Total managers fined this gameweek: 1 of 2" in text
        assert "Not fined this gameweek (1): Ada" in text

    @staticmethod
    def _season(*managers: tuple[str, int], ruled: list[int] | None = None) -> SeasonFinesTally:
        """A GW1-8 tally; every manager ruled every gameweek unless `ruled`
        narrows it."""
        gameweeks = list(range(1, 9)) if ruled is None else ruled
        return SeasonFinesTally(
            season="2025-26", fpl_format="classic", league_id=1,
            through_gameweek=8, start_gameweek=1, rule_types=["last-place"],
            managers=[
                ManagerFineTally(
                    manager_key=i, manager_name=name,
                    counts={"last-place": total} if total else {},
                    ruled_gameweeks_by_rule={"last-place": gameweeks},
                )
                for i, (name, total) in enumerate(managers, start=1)
            ],
        )

    def test_the_season_section_names_the_most_fines_and_who_holds_them(self):
        """2 of 10 drafts in the A/B for #507 called one of two managers level
        on three fines "the only manager with three fines"."""
        tally = self._season(("Kim", 3), ("Ada", 3), ("Bob", 1), ("Cy", 0), ("Dee", 0))

        text = format_recap_season_fines_context(tally)

        assert "Most fines this season: 3, shared by 2 managers, Kim and Ada" in text
        assert "Not fined so far (2): Cy, Dee" in text
        assert "may be short" not in text

    def test_a_single_top_total_is_named_as_one_managers_alone(self):
        tally = self._season(("Kim", 3), ("Ada", 1))

        assert "Most fines this season: 3, Kim alone" in format_recap_season_fines_context(tally)

    def test_a_record_with_unruled_gameweeks_says_no_total_is_anyones_alone(self):
        """GW4 was never ruled, so Ada may be on 4 too. The Coverage lines say
        so, but too far below "Kim alone" to be read with it."""
        tally = self._season(("Kim", 3), ("Ada", 1), ruled=[1, 2, 3, 5, 6, 7, 8])

        text = format_recap_season_fines_context(tally)

        assert "Most fines this season: 3, Kim alone" in text
        assert (
            "so a total may be short: neither line above makes anyone the only manager on "
            "a total or the only one not fined"
        ) in text

    def test_the_system_prompt_holds_sole_holder_claims_to_a_section(self):
        system, _ = get_recap_synthesis_prompt(
            gw=8, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="Bob fined",
        )

        assert '"the only one", "the only manager", "alone in" or "the one exception"' in system
        assert "Never count a section's list yourself to make a manager the only one in it" in system
        assert "no line names who holds a lower season total" in system

    def test_the_system_prompt_holds_universal_claims_and_forbids_reciting_the_unfined(self):
        """The pr512-gw8 A/B: one treatment editorial named three unfined
        managers as a group, and one in each arm said "nobody is spared this
        week" with 8 of 19 unfined."""
        system, _ = get_recap_synthesis_prompt(
            gw=8, league_name="Test", fpl_format="classic",
            awards_text="x", standings_text="| t |", fines_text="Bob fined",
        )

        assert '"nobody was spared", "everyone paid" or "no one got away" holds only where' in system
        assert "The not-fined lists are there to count from, never to recite" in system
        assert "never as one of a group" in system


class TestFinesContextNamesTheForfeit:
    @staticmethod
    def _data(phrasings):
        from fpl_cli.cli._league_recap_types import RecapFineResult

        data = _make_recap_data(fines=[RecapFineResult(
            manager_name="Bob", rule_type="last-place", message=f"Finished last in the gameweek. {_PINT}",
        )])
        data["fine_penalties"] = [{"penalty": _PINT, "phrasings": phrasings}]
        return data

    def test_the_league_s_phrasings_are_the_only_others_allowed(self):
        text = format_recap_fines_context(self._data(_PHRASINGS))

        assert "\n\nNaming the forfeit:\n" in text
        assert f'- "{_PINT}": write these exact words at most once in the editorial' in text
        assert "Every other mention in the prose that names it uses one of this league's own phrasings" in text
        assert 'never into more of it or a different act: ["pint of shame", "penance pint", ' in text
        assert text.endswith('"one for the group chat"]')

    def test_the_phrasings_line_counts_a_bare_forfeit_allows_asides_and_frees_only_the_headline(self):
        """Round pr507-gw8: a bare "the forfeit" went once it was named a
        mention; short asides held at 5 in 20 over two rounds and never
        changed the forfeit, so the league accepts them."""
        text = format_recap_fines_context(self._data(_PHRASINGS))

        assert "Every other mention in the prose that names it uses one of this league's own phrasings" in text
        assert 'a bare "the forfeit" counts; a short aside alluding to it may be in your own words' in text
        assert "only the headline may play on it in words of its own" in text

    def test_without_phrasings_there_is_no_list_to_free_the_headline_from(self):
        text = format_recap_fines_context(self._data([]))
        assert "headline" not in text

    def test_quotes_and_semicolons_in_the_league_s_text_stay_inside_their_entry(self):
        """Review: `; `-joined, one phrasing with a semicolon read as two."""
        data = self._data(["one for the lads; no questions", "penance pint"])
        data["fine_penalties"][0]["penalty"] = 'Wear the "Spoon" shirt'
        text = format_recap_fines_context(data)

        assert '- "Wear the \\"Spoon\\" shirt": write these exact words' in text
        assert 'or a different act: ["one for the lads; no questions", "penance pint"]' in text

    def test_without_phrasings_the_model_words_it_itself(self):
        text = format_recap_fines_context(self._data([]))

        assert f'- "{_PINT}": write these exact words at most once in the editorial' in text
        assert "Every other mention says the same forfeit in your own words" in text
        assert "no others" not in text

    def test_the_fine_lines_themselves_are_unchanged(self):
        text = format_recap_fines_context(self._data(_PHRASINGS))
        assert text.startswith(f"- Bob: Finished last in the gameweek. {_PINT}\n")

    def test_no_penalties_no_block(self):
        from fpl_cli.cli._league_recap_types import RecapFineResult

        data = _make_recap_data(fines=[RecapFineResult(
            manager_name="Bob", rule_type="last-place", message="Finished last in the gameweek.",
        )])
        assert "Naming the forfeit" not in format_recap_fines_context(data)

    def test_the_system_prompt_holds_the_forfeit_to_what_it_is(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert "Write a forfeit word for word at most once in the whole editorial, headline included" in (
            RECAP_SYNTHESIS_SYSTEM_PROMPT
        )
        assert "never more of it, a different drink or forfeit, or a part of it dropped" in (
            RECAP_SYNTHESIS_SYSTEM_PROMPT
        )

    def test_the_system_prompt_counts_a_bare_forfeit_and_allows_short_asides(self):
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert (
            "every other mention in the prose that names the forfeit is one of those, varied rather than "
            'repeated, and nothing else, and a bare "the forfeit" is such a mention'
        ) in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert (
            "A short aside that only alludes to it - what it involves, where it happens or who sees it - "
            "may be in your own words, as long as it keeps the forfeit as it is"
        ) in RECAP_SYNTHESIS_SYSTEM_PROMPT
        assert (
            "The headline is the one place you may play on the forfeit in words of your own, "
            "as long as it stays the same forfeit"
        ) in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_the_headline_sentence_says_what_the_same_forfeit_rules_out(self):
        """Round 2 of pr507-gw8: 3 of 20 headlines with phrasings set grew
        the forfeit ("a pub crawl", "the barman gets a busy night") or swapped
        it ("ten managers pay the bar tab")."""
        from fpl_cli.prompts.league_recap import RECAP_SYNTHESIS_SYSTEM_PROMPT

        assert (
            "never more of it than one per fine (a single fine is never a pub crawl or a long night at "
            "the bar), and never a different act in its place, such as paying, buying or standing a round"
        ) in RECAP_SYNTHESIS_SYSTEM_PROMPT

    def test_the_phrasings_line_keeps_the_headline_to_the_same_forfeit(self):
        from fpl_cli.cli._league_recap_types import RecapFineResult

        data = _make_recap_data(fines=[RecapFineResult(
            manager_name="Bob", rule_type="last-place", message=f"Finished last in the gameweek. {_PINT}",
        )])
        data["fine_penalties"] = [{"penalty": _PINT, "phrasings": _PHRASINGS}]
        text = format_recap_fines_context(data)
        assert "only the headline may play on it in words of its own, never into more of it or a different act" in text


class TestPenaltyRepetitions:
    _PENALTIES = [{"penalty": _PINT, "phrasings": []}]

    def test_once_is_allowed(self):
        assert check_penalty_repetitions(f"## The Pint Shop\nBob owes a {_PINT.lower()}.", self._PENALTIES) == []

    def test_twice_is_caught_with_both_quoted(self):
        summary = "Bob owes a **pint on video**. Cam joins him: pints on video all round."
        assert check_penalty_repetitions(summary, self._PENALTIES) == [
            'the editorial writes the forfeit "Pint on video" out word for word twice '
            '("pint on video", "pints on video"), where the brief allows it once',
        ]

    def test_the_headline_counts(self):
        summary = "## Pint on Video Time\nBob owes a pint on video."
        assert "twice" in check_penalty_repetitions(summary, self._PENALTIES)[0]

    def test_three_times_is_counted(self):
        summary = "Pint on video. Pint on video. Pint on video."
        assert "3 times" in check_penalty_repetitions(summary, self._PENALTIES)[0]

    def test_phrasings_and_near_misses_are_not_the_forfeit(self):
        summary = (
            "Bob owes a pint on video. A pint on camera for Cam, the pint of shame for Dan, "
            "and a pint. On video, Eve celebrated."
        )
        assert check_penalty_repetitions(summary, self._PENALTIES) == []

    def test_a_forfeit_with_its_own_sentence_break_still_matches_itself(self):
        """Review: the gap that stops a match at a sentence end was applied
        inside the forfeit too, so this never fired."""
        penalties = [{"penalty": "Pint on video. Tag the group", "phrasings": []}]
        summary = "Pint on video. Tag the group for Sam. Another Pint on video. Tag the group."
        assert "twice" in check_penalty_repetitions(summary, penalties)[0]

        penalties = [{"penalty": "Pint on video: no excuses", "phrasings": []}]
        summary = "Sam: pint on video: no excuses. Jo: Pint on video: no excuses."
        assert "twice" in check_penalty_repetitions(summary, penalties)[0]

    def test_a_break_the_forfeit_does_not_carry_still_ends_the_match(self):
        penalties = [{"penalty": "Pint on video. Tag the group", "phrasings": []}]
        assert check_penalty_repetitions("Pint on video; tag the group. Pint on video! Tag the group", penalties) == []

    def test_number_is_matched_whichever_way_the_forfeit_was_configured(self):
        """Review: a forfeit configured in the plural missed the singular."""
        summary = "Pint on video for Sam. Another pint on video for Jo."
        assert "twice" in check_penalty_repetitions(summary, [{"penalty": "Pints on video", "phrasings": []}])[0]
        summary = "A glass of wine for Sam, two glasses of wine for Jo."
        assert "twice" in check_penalty_repetitions(summary, [{"penalty": "Glasses of wine", "phrasings": []}])[0]

    def test_short_words_keep_the_number_they_were_written_in(self):
        penalties = [{"penalty": "Pay us back", "phrasings": []}]
        assert check_penalty_repetitions("Pay u back, then pay u back.", penalties) == []

    def test_restates_forfeit(self):
        assert restates_forfeit("pint on video", _PINT)
        assert restates_forfeit("a pints on video for the lads", _PINT)
        assert not restates_forfeit("a pint on camera", _PINT)
        assert not restates_forfeit("shot", "Shot")

    def test_a_one_word_forfeit_is_left_alone(self):
        summary = "Bob owes a shot. Cam had a shot at the title."
        assert check_penalty_repetitions(summary, [{"penalty": "Shot", "phrasings": []}]) == []

    def test_the_registry_reads_the_forfeits_off_the_data(self):
        data = _make_recap_data()
        summary = "Bob owes a pint on video. Another pint on video for Cam."
        assert check_recap_editorial(summary, data) == {}

        data["fine_penalties"] = self._PENALTIES
        problems = check_recap_editorial(summary, data)
        assert list(problems) == [RECAP_WARNING_PENALTY_REPEATED]
        assert recap_editorial_flaw(problems) == "breaks the brief it was given"

    def test_the_retry_names_the_forfeit_line_and_keeps_it_unchanged(self):
        prompt = get_recap_editorial_retry_prompt(
            "ORIGINAL", {RECAP_WARNING_PENALTY_REPEATED: ["the editorial writes it twice"]},
        )
        assert "Your previous draft broke the brief it was given:" in prompt
        assert 'in the exact words of its "Naming the forfeit" line at most once' in prompt
        assert "without changing what the forfeit is." in prompt
