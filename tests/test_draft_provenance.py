"""Tests for the draft provenance replay (#458).

One placeholder league, three managers, nine drafted players and three in the
pool. Everything that happens to it after the draft is on the page:

    GW2  Bob claims 10 on waivers, releasing 5
    GW3  Alice signs 11 as a free agent, releasing 7; Cam signs 12, releasing 3
    GW4  trade: Alice gives 4 to Bob for 8
    GW5  Cam claims 3 on waivers (his own first-round pick, dropped in GW3),
         releasing 12; a trade of 1 for 6 between Alice and Cam is vetoed

so the league ends with Alice on 1, 8, 11; Bob on 2, 4, 10; Cam on 3, 6, 9.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from fpl_cli.services.draft_provenance import (
    DraftLogs,
    DraftProvenance,
    draft_in_force,
    get_draft_provenance,
    read_draft_logs,
    replay_draft_history,
)
from fpl_cli.services.league_history_drafts import (
    completed_drafts,
    lost_draft_path,
    snapshot_draft_choices,
)
from tests.conftest import make_player

ALICE, BOB, CAM = 101, 102, 103
DRAFT = 10
REDRAFT = 20
DRAFTED_AT = "2026-08-10T12:00:00Z"
REDRAFTED_AT = "2026-10-01T12:00:00Z"
ELEMENTS = range(1, 13)


def _pick(element: int, entry: int, rnd: int, pick: int, *, draft: int = DRAFT, auto: bool = False) -> dict[str, Any]:
    return {
        "element": element, "entry": entry, "round": rnd, "pick": pick, "draft": draft,
        "index": (rnd - 1) * 3 + pick, "was_auto": auto,
    }


def _txn(
    ident: int, entry: int, element_in: int, element_out: int | None, event: int, added: str, *,
    kind: str = "w", result: str = "a", index: int = 1,
) -> dict[str, Any]:
    return {
        "id": ident, "entry": entry, "element_in": element_in, "element_out": element_out,
        "event": event, "added": added, "kind": kind, "result": result, "index": index, "priority": 1,
    }


def _trade(
    ident: int, offered: int, received: int, items: list[tuple[int, int]], event: int, responded: str, *,
    state: str = "p",
) -> dict[str, Any]:
    """*items* are (element_out, element_in) from the offering entry's side."""
    return {
        "id": ident, "event": event, "offered_entry": offered, "received_entry": received, "state": state,
        "offer_time": responded, "response_time": responded,
        "tradeitem_set": [{"element_out": out, "element_in": into} for out, into in items],
    }


def _status(owners: dict[int, int]) -> list[dict[str, Any]]:
    return [
        {"element": e, "owner": owners.get(e), "status": "o" if e in owners else "a", "in_accepted_trade": False}
        for e in ELEMENTS
    ]


def _draft_row(ident: int, event: int, completed: str | None) -> dict[str, Any]:
    return {"id": ident, "event": event, "draft_completed": completed, "draft_dt": completed, "league": 1}


FIRST_DRAFT = [
    _pick(1, ALICE, 1, 1), _pick(2, BOB, 1, 2), _pick(3, CAM, 1, 3),
    _pick(4, ALICE, 2, 1), _pick(5, BOB, 2, 2), _pick(6, CAM, 2, 3, auto=True),
    _pick(7, ALICE, 3, 1), _pick(8, BOB, 3, 2), _pick(9, CAM, 3, 3),
]
TRANSACTIONS = [
    _txn(1, BOB, 10, 5, 2, "2026-08-20T10:00:00Z"),
    _txn(2, ALICE, 11, 7, 3, "2026-08-27T09:00:00Z", kind="f", index=0),
    _txn(3, CAM, 12, 3, 3, "2026-08-27T10:00:00Z", kind="f", index=0),
    # Denied claims change nothing, whatever they name.
    _txn(4, ALICE, 3, 11, 5, "2026-09-10T09:00:00Z", result="di", index=0),
    _txn(5, CAM, 3, 12, 5, "2026-09-10T10:00:00Z"),
]
TRADES = [
    _trade(1, ALICE, BOB, [(4, 8)], 4, "2026-09-03T12:00:00Z"),
    _trade(2, ALICE, CAM, [(1, 6)], 5, "2026-09-11T12:00:00Z", state="v"),
]
OWNERS_NOW = {1: ALICE, 8: ALICE, 11: ALICE, 2: BOB, 4: BOB, 10: BOB, 3: CAM, 6: CAM, 9: CAM}
SQUADS_GW3 = {1: ALICE, 4: ALICE, 11: ALICE, 2: BOB, 8: BOB, 10: BOB, 6: CAM, 9: CAM, 12: CAM}
SQUADS_GW4 = {1: ALICE, 8: ALICE, 11: ALICE, 2: BOB, 4: BOB, 10: BOB, 6: CAM, 9: CAM, 12: CAM}


def _logs(**overrides: Any) -> DraftLogs:
    logs = DraftLogs(
        drafts=[_draft_row(DRAFT, 1, DRAFTED_AT)],
        choices=FIRST_DRAFT,
        element_status=_status(OWNERS_NOW),
        transactions=TRANSACTIONS,
        trades=TRADES,
    )
    return replace(logs, **overrides)


def _histories(provenance: DraftProvenance):
    assert provenance.available, provenance.problem
    return provenance.players


class TestTheDraftAndTheLogsAfterIt:
    def test_a_player_who_stayed_where_he_was_drafted_reads_as_the_pick(self):
        histories = _histories(replay_draft_history(_logs(), 5))

        latest = histories[1].latest
        assert (latest.kind, latest.entry, latest.gameweek) == ("draft", ALICE, 1)
        assert latest.pick is not None
        assert (latest.pick.round, latest.pick.pick, latest.pick.overall) == (1, 1, 1)
        assert (latest.pick.draft_id, latest.pick.draft_gameweek, latest.pick.auto) == (DRAFT, 1, False)
        assert [step.kind for step in histories[1].chain] == ["draft"]

    def test_a_waiver_pickup_reads_as_the_waiver_with_its_gameweek(self):
        histories = _histories(replay_draft_history(_logs(), 5))

        latest = histories[10].latest
        assert (latest.kind, latest.entry, latest.gameweek, latest.pick) == ("waiver", BOB, 2, None)
        assert histories[10].owner == BOB

    def test_a_free_agent_signing_reads_as_the_signing_with_its_gameweek(self):
        histories = _histories(replay_draft_history(_logs(), 5))

        latest = histories[11].latest
        assert (latest.kind, latest.entry, latest.gameweek) == ("free_agent", ALICE, 3)

    def test_a_processed_trade_reads_as_the_trade_in_each_direction(self):
        """`element_out` leaves the offering entry for the receiving one and
        `element_in` goes the other way."""
        histories = _histories(replay_draft_history(_logs(), 5))

        to_bob, to_alice = histories[4].latest, histories[8].latest
        assert (to_bob.kind, to_bob.entry, to_bob.from_entry, to_bob.gameweek) == ("trade", BOB, ALICE, 4)
        assert (to_alice.kind, to_alice.entry, to_alice.from_entry, to_alice.gameweek) == ("trade", ALICE, BOB, 4)
        assert [step.kind for step in histories[8].chain] == ["draft", "trade"]
        assert histories[8].chain[0].entry == BOB

    def test_a_vetoed_trade_changes_nothing(self):
        histories = _histories(replay_draft_history(_logs(), 5))

        assert histories[1].owner == ALICE and histories[1].latest.kind == "draft"
        assert histories[6].owner == CAM and histories[6].latest.kind == "draft"
        assert not any(step.kind == "trade" and step.gameweek == 5 for h in histories.values() for step in h.chain)

    def test_a_trade_in_a_state_nobody_has_seen_move_players_is_not_applied(self):
        trades = [replace_state(TRADES[0], "o"), TRADES[1]]

        provenance = replay_draft_history(_logs(trades=trades), 5)

        assert not provenance.available
        assert provenance.players == {}

    def test_a_pick_dropped_and_signed_again_is_on_the_signing_with_the_whole_chain_kept(self):
        histories = _histories(replay_draft_history(_logs(), 5))

        chain = histories[3].chain
        assert [(step.kind, step.entry, step.gameweek) for step in chain] == [
            ("draft", CAM, 1), ("release", CAM, 3), ("waiver", CAM, 5),
        ]
        assert histories[3].latest.kind == "waiver"
        assert histories[3].latest.pick is None

    def test_an_auto_pick_is_marked(self):
        histories = _histories(replay_draft_history(_logs(), 5))

        pick = histories[6].latest.pick
        assert pick is not None and (pick.round, pick.pick, pick.auto) == (2, 3, True)
        assert histories[1].latest.pick is not None and histories[1].latest.pick.auto is False

    def test_only_the_owned_players_have_a_history(self):
        histories = _histories(replay_draft_history(_logs(), 5))

        assert set(histories) == set(OWNERS_NOW)
        assert {h.owner for h in histories.values()} == {ALICE, BOB, CAM}

    def test_the_main_games_id_and_code_ride_beside_the_draft_element(self):
        main = {1: make_player(id=501, code=7001), 2: make_player(id=502, code=0)}

        histories = _histories(replay_draft_history(_logs(), 5, main_players=main))

        assert (histories[1].element, histories[1].id, histories[1].code) == (1, 501, 7001)
        assert (histories[2].id, histories[2].code) == (502, None)
        assert (histories[3].id, histories[3].code) == (None, None)

    def test_a_clock_tie_between_waivers_is_broken_by_the_order_the_league_processed_them(self):
        """Two claims sharing a timestamp, the second on the first's released
        player, only fit one way round: the league's `index`, not the row id."""
        same = "2026-08-20T10:00:00Z"
        txns = [
            _txn(1, ALICE, 5, 7, 2, same, index=2),
            _txn(2, BOB, 10, 5, 2, same, index=1),
        ]
        owners = {1: ALICE, 4: ALICE, 5: ALICE, 2: BOB, 8: BOB, 10: BOB, 3: CAM, 6: CAM, 9: CAM}
        logs = _logs(transactions=txns, trades=[], element_status=_status(owners))

        histories = _histories(replay_draft_history(logs, 5))

        assert (histories[5].latest.kind, histories[5].owner) == ("waiver", ALICE)
        assert [step.kind for step in histories[5].chain] == ["draft", "release", "waiver"]
        assert histories[5].chain[0].entry == BOB


def replace_state(trade: dict[str, Any], state: str) -> dict[str, Any]:
    return {**trade, "state": state}


class TestAnEarlierGameweek:
    def test_the_log_stops_at_the_gameweek_so_the_week_shows_its_own_owners(self):
        provenance = replay_draft_history(_logs(), 3, observed=SQUADS_GW3)

        histories = _histories(provenance)
        assert histories[8].owner == BOB and histories[8].latest.kind == "draft"
        assert histories[4].owner == ALICE and histories[4].latest.kind == "draft"
        assert (histories[12].latest.kind, histories[12].latest.gameweek) == ("free_agent", 3)
        assert 3 not in histories

    def test_a_trade_after_the_gameweek_is_not_yet_a_trade(self):
        before = _histories(replay_draft_history(_logs(), 3, observed=SQUADS_GW3))
        after = _histories(replay_draft_history(_logs(), 4, observed=SQUADS_GW4))

        assert before[8].latest.kind == "draft"
        assert (after[8].latest.kind, after[8].latest.from_entry) == ("trade", BOB)

    def test_a_replay_that_puts_players_in_other_squads_than_the_week_recorded_is_refused(self):
        wrong = {**SQUADS_GW3, 8: ALICE}

        provenance = replay_draft_history(_logs(), 3, observed=wrong)

        assert not provenance.available
        assert provenance.players == {}
        assert "GW3" in (provenance.problem or "")

    def test_a_manager_whose_squad_was_not_read_is_no_disagreement(self):
        only_alice = {e: o for e, o in SQUADS_GW3.items() if o == ALICE}

        assert replay_draft_history(_logs(), 3, observed=only_alice).available


class TestRedrafts:
    """A redraft on GW6 re-picks nine players, some kept and some not."""

    REDRAFT_PICKS = [
        _pick(1, CAM, 1, 1, draft=REDRAFT), _pick(3, BOB, 1, 2, draft=REDRAFT), _pick(2, ALICE, 1, 3, draft=REDRAFT),
        _pick(4, CAM, 2, 1, draft=REDRAFT), _pick(6, BOB, 2, 2, draft=REDRAFT), _pick(5, ALICE, 2, 3, draft=REDRAFT),
        _pick(7, CAM, 3, 1, draft=REDRAFT), _pick(9, BOB, 3, 2, draft=REDRAFT), _pick(8, ALICE, 3, 3, draft=REDRAFT),
    ]
    AFTER = [_txn(20, ALICE, 10, 5, 7, "2026-10-08T10:00:00Z")]
    OWNERS = {1: CAM, 4: CAM, 7: CAM, 3: BOB, 6: BOB, 9: BOB, 2: ALICE, 8: ALICE, 10: ALICE}
    # The first draft's league as it stood at the end of GW5, the week before the redraft.
    SQUADS_GW5 = OWNERS_NOW

    def _league(self, **overrides: Any) -> DraftLogs:
        base = DraftLogs(
            drafts=[_draft_row(DRAFT, 1, DRAFTED_AT), _draft_row(REDRAFT, 6, REDRAFTED_AT)],
            choices=[*FIRST_DRAFT, *self.REDRAFT_PICKS],
            element_status=_status(self.OWNERS),
            transactions=[*TRANSACTIONS, *self.AFTER],
            trades=TRADES,
        )
        return replace(base, **overrides)

    def test_the_draft_in_force_is_the_latest_one_completed_at_or_before_the_gameweek(self):
        drafts = [_draft_row(10, 1, DRAFTED_AT), _draft_row(20, 6, REDRAFTED_AT), _draft_row(30, 12, None)]

        assert [d["id"] for d in completed_drafts(drafts)] == [10, 20]
        assert (draft_in_force(drafts, 5) or {})["id"] == 10
        assert (draft_in_force(drafts, 6) or {})["id"] == 20
        assert (draft_in_force(drafts, 38) or {})["id"] == 20
        assert draft_in_force(drafts, 0) is None

    def test_a_player_kept_through_a_redraft_is_drafted_again_under_it(self):
        provenance = replay_draft_history(self._league(), 7)

        histories = _histories(provenance)
        pick = histories[1].latest.pick
        assert histories[1].owner == CAM
        assert pick is not None and (pick.draft_id, pick.draft_gameweek, pick.round, pick.pick) == (REDRAFT, 6, 1, 1)
        assert [step.kind for step in histories[1].chain] == ["draft"]
        assert provenance.draft is not None
        assert (provenance.draft.id, provenance.draft.gameweek, provenance.draft.redraft) == (REDRAFT, 6, True)

    def test_moves_after_the_redraft_follow_the_acquisition_rule(self):
        histories = _histories(replay_draft_history(self._league(), 7))

        assert (histories[10].latest.kind, histories[10].latest.gameweek, histories[10].owner) == ("waiver", 7, ALICE)

    def test_moves_before_the_redraft_are_not_part_of_a_chain_that_starts_at_it(self):
        histories = _histories(replay_draft_history(self._league(), 7))

        assert all(step.at.isoformat() > "2026-10-01" for h in histories.values() for step in h.chain)

    def test_a_replay_of_a_gameweek_before_the_redraft_uses_the_draft_in_force_then(self):
        provenance = replay_draft_history(self._league(), 5, observed=self.SQUADS_GW5)

        histories = _histories(provenance)
        pick = histories[2].latest.pick
        assert pick is not None and pick.draft_id == DRAFT and histories[2].owner == BOB
        assert provenance.draft is not None and provenance.draft.redraft is False
        assert (histories[8].latest.kind, histories[8].owner) == ("trade", ALICE)

    def test_the_label_names_the_draft_only_once_the_league_has_drafted_twice(self):
        before = replay_draft_history(self._league(), 5, observed=self.SQUADS_GW5)
        after = replay_draft_history(self._league(), 6)

        assert before.draft is not None and before.draft.redraft is False
        assert after.draft is not None and after.draft.redraft is True

    def test_a_feed_that_serves_only_the_latest_draft_cannot_replay_an_earlier_week(self):
        """What FPL's choices feed does: the league's latest draft, nothing
        for the one before it. The earlier week is refused, with the reason;
        the redraft's own weeks are unaffected."""
        league = self._league(choices=self.REDRAFT_PICKS)

        earlier = replay_draft_history(league, 5, observed=self.SQUADS_GW5)
        later = replay_draft_history(league, 7)

        assert not earlier.available and earlier.players == {}
        assert "holds the picks of the GW1 draft" in (earlier.problem or "")
        assert "latest draft only" in (earlier.problem or "")
        assert later.available


class TestWhenTheHistoryCannotBeTrusted:
    def test_an_end_state_that_disagrees_with_the_ownership_list_gives_no_chains(self):
        moved = {**OWNERS_NOW, 2: ALICE}

        provenance = replay_draft_history(_logs(element_status=_status(moved)), 5)

        assert not provenance.available
        assert provenance.players == {}
        assert "1 player owned differently from FPL's own ownership list" in (provenance.problem or "")

    def test_a_failed_trades_read_is_a_disagreement_once_a_trade_has_happened(self):
        provenance = replay_draft_history(_logs(trades=None), 5)

        assert not provenance.available
        assert provenance.players == {}
        assert "the trades could not be read" in (provenance.problem or "")

    def test_a_failed_trades_read_costs_nothing_while_no_trade_has_happened(self):
        """Nothing was lost by not reading them, and the ownership list says
        so: the replay still ends exactly where FPL does."""
        txns = [t for t in TRANSACTIONS if t["id"] in (1, 2)]
        owners = {1: ALICE, 4: ALICE, 11: ALICE, 2: BOB, 8: BOB, 10: BOB, 3: CAM, 6: CAM, 9: CAM}

        provenance = replay_draft_history(
            _logs(trades=None, transactions=txns, element_status=_status(owners)), 5,
        )

        assert provenance.available
        assert provenance.players[11].latest.kind == "free_agent"

    def test_a_missing_ownership_list_leaves_nothing_to_check_against(self):
        for status in (None, []):
            provenance = replay_draft_history(_logs(element_status=status), 5)

            assert not provenance.available
            assert "ownership list" in (provenance.problem or "")

    def test_unread_logs_say_which(self):
        assert "choices" in (replay_draft_history(_logs(choices=None), 5).problem or "")
        assert "transactions" in (replay_draft_history(_logs(transactions=None), 5).problem or "")

    def test_no_picks_for_the_draft_is_an_answer_not_a_crash(self):
        provenance = replay_draft_history(_logs(choices=[]), 5)

        assert not provenance.available
        assert provenance.players == {}
        assert "holds the picks of the GW1 draft" in (provenance.problem or "")

    def test_a_league_with_a_later_draft_scheduled_says_why_its_picks_are_missing(self):
        """The case seen on public leagues: the feed serves the scheduled
        draft's (empty) picks, not the held one's."""
        drafts = [_draft_row(DRAFT, 1, DRAFTED_AT), _draft_row(30, 19, None)]

        provenance = replay_draft_history(_logs(drafts=drafts, choices=[]), 5)

        assert "latest draft only" in (provenance.problem or "")

    def test_a_scheduled_draft_that_has_not_been_held_is_not_in_force(self):
        drafts = [_draft_row(DRAFT, 1, DRAFTED_AT), _draft_row(30, 3, None)]

        provenance = replay_draft_history(_logs(drafts=drafts), 5)

        assert provenance.available
        assert provenance.draft is not None and provenance.draft.id == DRAFT

    @pytest.mark.parametrize("drafts", [[], [_draft_row(DRAFT, 3, None)]], ids=["none", "not yet held"])
    def test_before_any_draft_there_is_nothing_to_replay(self, drafts: list[dict[str, Any]]):
        provenance = replay_draft_history(_logs(drafts=drafts), 5)

        assert not provenance.available and provenance.players == {}

    def test_a_gameweek_before_the_first_draft_has_no_draft_in_force(self):
        provenance = replay_draft_history(_logs(drafts=[_draft_row(DRAFT, 4, DRAFTED_AT)]), 2)

        assert "no draft had been held by GW2" == provenance.problem

    @pytest.mark.parametrize("bad", [
        # Released by an entry that never held him.
        _txn(1, BOB, 10, 4, 2, "2026-08-20T10:00:00Z"),
        # A claim on a player someone holds.
        _txn(1, BOB, 1, 5, 2, "2026-08-20T10:00:00Z"),
        # A kind of move the log has not been seen to hold.
        _txn(1, BOB, 10, 5, 2, "2026-08-20T10:00:00Z", kind="t"),
        # Accepted, but with no time to place it by.
        {**_txn(1, BOB, 10, 5, 2, "2026-08-20T10:00:00Z"), "added": None},
    ], ids=["release by a stranger", "claim on a held player", "unknown kind", "no timestamp"])
    def test_a_move_that_does_not_fit_the_squads_it_applies_to_refuses_the_whole_replay(self, bad: dict[str, Any]):
        provenance = replay_draft_history(_logs(transactions=[bad, *TRANSACTIONS[1:]]), 5)

        assert not provenance.available and provenance.players == {}

    def test_a_trade_of_a_player_the_giver_did_not_hold_refuses_the_whole_replay(self):
        provenance = replay_draft_history(_logs(trades=[_trade(1, ALICE, BOB, [(9, 8)], 4, "2026-09-03T12:00:00Z")]), 5)

        assert not provenance.available and provenance.players == {}

    def test_a_pick_drafted_twice_or_without_its_round_refuses_the_replay(self):
        twice = [*FIRST_DRAFT, _pick(1, BOB, 4, 1)]
        no_round = [{**FIRST_DRAFT[0], "round": None}, *FIRST_DRAFT[1:]]

        assert not replay_draft_history(_logs(choices=twice), 5).available
        assert not replay_draft_history(_logs(choices=no_round), 5).available


class TestReadingTheLogs:
    def _client(self, **overrides: Any) -> MagicMock:
        client = MagicMock()
        client.get_draft_choices = AsyncMock(return_value={"choices": FIRST_DRAFT, "element_status": _status(OWNERS_NOW)})
        client.get_league_trades = AsyncMock(return_value={"trades": TRADES})
        client.get_league_transactions = AsyncMock(return_value={"transactions": TRANSACTIONS})
        client.get_league_details = AsyncMock(return_value={"league": {"drafts": [_draft_row(DRAFT, 1, DRAFTED_AT)]}})
        for name, value in overrides.items():
            setattr(client, name, value)
        return client

    async def test_the_two_new_reads_cost_two_requests_when_the_caller_holds_the_rest(self):
        client = self._client()

        logs = await read_draft_logs(
            client, 7, drafts=[_draft_row(DRAFT, 1, DRAFTED_AT)], transactions=TRANSACTIONS,
        )

        client.get_draft_choices.assert_awaited_once_with(7)
        client.get_league_trades.assert_awaited_once_with(7)
        client.get_league_details.assert_not_awaited()
        client.get_league_transactions.assert_not_awaited()
        assert logs.choices == FIRST_DRAFT and logs.trades == TRADES and logs.transactions == TRANSACTIONS

    async def test_what_the_caller_does_not_hold_is_read(self):
        client = self._client()

        logs = await read_draft_logs(client, 7)

        client.get_league_details.assert_awaited_once_with(7)
        client.get_league_transactions.assert_awaited_once_with(7)
        assert [d["id"] for d in logs.drafts] == [DRAFT]
        assert logs.transactions == TRANSACTIONS

    @pytest.mark.parametrize("failing", ["get_draft_choices", "get_league_trades", "get_league_transactions"])
    async def test_a_failed_read_is_a_missing_log_not_an_exception(self, failing: str):
        client = self._client(**{failing: AsyncMock(side_effect=httpx.ConnectError("down"))})

        logs = await read_draft_logs(client, 7, drafts=[_draft_row(DRAFT, 1, DRAFTED_AT)])

        missing = {
            "get_draft_choices": (logs.choices, logs.element_status),
            "get_league_trades": (logs.trades,),
            "get_league_transactions": (logs.transactions,),
        }[failing]
        assert all(log is None for log in missing)

    async def test_a_reply_in_a_shape_nobody_expected_is_a_missing_log(self):
        client = self._client(get_league_trades=AsyncMock(return_value=["not", "a", "dict"]))

        logs = await read_draft_logs(client, 7, drafts=[], transactions=[])

        assert logs.trades is None

    async def test_unparseable_json_is_a_missing_log(self):
        client = self._client(get_league_trades=AsyncMock(side_effect=ValueError("not json")))

        assert (await read_draft_logs(client, 7, drafts=[], transactions=[])).trades is None

    async def test_read_and_replay_together(self):
        provenance = await get_draft_provenance(
            self._client(), 7, 5,
            main_players={1: make_player(id=501, code=7001)},
            drafts=[_draft_row(DRAFT, 1, DRAFTED_AT)], transactions=TRANSACTIONS,
        )

        assert provenance.available
        assert (provenance.players[1].id, provenance.players[1].code) == (501, 7001)

    async def test_a_failed_trades_read_through_the_whole_helper_degrades_the_same_way(self):
        client = self._client(get_league_trades=AsyncMock(side_effect=httpx.ConnectError("down")))

        provenance = await get_draft_provenance(client, 7, 5, drafts=[_draft_row(DRAFT, 1, DRAFTED_AT)])

        assert not provenance.available and provenance.players == {}


SEASON = "2026-27"
LEAGUE = 42


def _saved(draft: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """Write a snapshot the way `league-recap --draft` does, with #464's writer."""
    response = {"choices": rows, "element_status": _status(OWNERS_NOW), "idle": []}
    snapshot_draft_choices(SEASON, LEAGUE, completed_drafts([draft]), response)


class TestWhenTheFeedHasMovedOn:
    """FPL serves a league's latest draft only. Once a redraft is scheduled the
    held draft's picks come from the snapshot taken while the feed had them;
    the feed is still asked first."""

    HELD = _draft_row(DRAFT, 1, DRAFTED_AT)
    SCHEDULED = _draft_row(30, 19, None)

    def _client(self, served: list[dict[str, Any]], **logs: Any) -> MagicMock:
        client = MagicMock()
        client.get_draft_choices = AsyncMock(return_value={
            "choices": served, "element_status": _status(OWNERS_NOW), "idle": [],
        })
        client.get_league_trades = AsyncMock(return_value={"trades": TRADES})
        client.get_league_transactions = AsyncMock(return_value={"transactions": TRANSACTIONS})
        return client

    async def _provenance(self, client: MagicMock, **kwargs: Any) -> DraftProvenance:
        return await get_draft_provenance(
            client, LEAGUE, 5, drafts=kwargs.pop("drafts", [self.HELD, self.SCHEDULED]),
            transactions=TRANSACTIONS, season=SEASON, observed=OWNERS_NOW, **kwargs,
        )

    async def test_a_feed_with_no_rows_for_the_held_draft_falls_back_to_its_snapshot(self):
        """The feed serves the scheduled draft's (empty) picks."""
        _saved(self.HELD, FIRST_DRAFT)

        provenance = await self._provenance(self._client([]))

        histories = _histories(provenance)
        pick = histories[1].latest.pick
        assert pick is not None and (pick.draft_id, pick.round, pick.pick) == (DRAFT, 1, 1)
        assert histories[3].latest.kind == "waiver"
        assert histories[8].latest.kind == "trade"

    async def test_a_feed_serving_another_draft_falls_back_to_the_snapshot_of_this_one(self):
        elsewhere = [_pick(1, ALICE, 1, 1, draft=30)]
        _saved(self.HELD, FIRST_DRAFT)

        provenance = await self._provenance(self._client(elsewhere), drafts=[self.HELD, _draft_row(30, 19, None)])

        assert provenance.available, provenance.problem
        assert provenance.draft is not None and provenance.draft.id == DRAFT

    async def test_the_feed_is_asked_first_and_wins_where_it_has_the_picks(self):
        wrong = [{**FIRST_DRAFT[0], "entry": BOB}, *FIRST_DRAFT[1:]]
        _saved(self.HELD, wrong)

        provenance = await self._provenance(self._client(FIRST_DRAFT), drafts=[self.HELD])

        assert _histories(provenance)[1].owner == ALICE

    async def test_neither_source_having_the_picks_degrades_with_the_reason(self):
        provenance = await self._provenance(self._client([]))

        assert not provenance.available and provenance.players == {}
        assert "neither FPL's choices list nor a saved snapshot holds the picks of the GW1 draft" in (
            provenance.problem or ""
        )
        assert "latest draft only" in (provenance.problem or "")

    async def test_a_loss_a_run_already_recorded_is_said_so(self):
        lost_draft_path(SEASON, LEAGUE, DRAFT).parent.mkdir(parents=True, exist_ok=True)
        lost_draft_path(SEASON, LEAGUE, DRAFT).write_text("{}", encoding="utf-8")

        provenance = await self._provenance(self._client([]))

        assert "already recorded that they were gone before any was saved" in (provenance.problem or "")

    async def test_a_snapshot_written_by_hand_without_the_leagues_entry_reads_the_same(self):
        """A response saved straight from the feed lacks `league_draft`."""
        path = lost_draft_path(SEASON, LEAGUE, DRAFT).parent / f"draft-{DRAFT}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"choices": FIRST_DRAFT, "element_status": _status(OWNERS_NOW), "idle": []}),
            encoding="utf-8",
        )

        assert (await self._provenance(self._client([]))).available

    async def test_no_season_means_the_snapshot_is_not_consulted(self):
        _saved(self.HELD, FIRST_DRAFT)

        provenance = await get_draft_provenance(
            self._client([]), LEAGUE, 5, drafts=[self.HELD, self.SCHEDULED], transactions=TRANSACTIONS,
        )

        assert not provenance.available

    async def test_an_unreadable_snapshot_is_no_copy_at_all(self):
        path = lost_draft_path(SEASON, LEAGUE, DRAFT).parent / f"draft-{DRAFT}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        provenance = await self._provenance(self._client([]))

        assert not provenance.available
        assert "holds the picks of the GW1 draft" in (provenance.problem or "")

    async def test_a_snapshot_cannot_stand_in_for_a_failed_ownership_read(self):
        """The live ownership list is what the replay is checked against, and a
        snapshot's copy is as old as the draft."""
        _saved(self.HELD, FIRST_DRAFT)
        client = self._client([])
        client.get_draft_choices = AsyncMock(side_effect=httpx.ConnectError("down"))

        provenance = await self._provenance(client)

        assert not provenance.available
        assert "choices could not be read" in (provenance.problem or "")


class TestWhenThePicksAreLost:
    """A draft held, its picks in neither the feed nor a snapshot: who drafted whom
    is rebuilt from each manager's squad at the draft's gameweek, with the moves
    made before that gameweek undone, and the rounds are not guessed.

    The same league as the picks-known tests start from, with moves before GW1:

        GW1  Bob claims 10 on waivers, releasing 5
        GW1  trade: Alice gives 4 to Cam for 6
        GW1  Cam signs 11 as a free agent, releasing 9
        GW3  Alice claims 5 on waivers, releasing 7
        GW4  trade: Bob gives 8 to Cam for 3

    so the GW1 squads were Alice 1, 6, 7; Bob 2, 8, 10; Cam 3, 4, 11, and the
    league ends with Alice on 1, 5, 6; Bob on 2, 3, 10; Cam on 4, 8, 11. Each was
    drafted as the picks-known league's were: Alice 1, 4, 7; Bob 2, 5, 8; Cam 3, 6, 9.
    """

    TXNS = [
        _txn(1, BOB, 10, 5, 1, "2026-08-11T10:00:00Z"),
        _txn(2, CAM, 11, 9, 1, "2026-08-13T09:00:00Z", kind="f", index=0),
        _txn(3, ALICE, 5, 7, 3, "2026-08-27T09:00:00Z"),
    ]
    TRADES = [
        _trade(1, ALICE, CAM, [(4, 6)], 1, "2026-08-12T10:00:00Z"),
        _trade(2, BOB, CAM, [(8, 3)], 4, "2026-09-03T12:00:00Z"),
    ]
    SQUADS_AT_GW1 = {1: ALICE, 6: ALICE, 7: ALICE, 2: BOB, 8: BOB, 10: BOB, 3: CAM, 4: CAM, 11: CAM}
    OWNERS = {1: ALICE, 5: ALICE, 6: ALICE, 2: BOB, 3: BOB, 10: BOB, 4: CAM, 8: CAM, 11: CAM}

    def _lost(self, **overrides: Any) -> DraftLogs:
        logs = DraftLogs(
            drafts=[_draft_row(DRAFT, 1, DRAFTED_AT)],
            choices=[],
            element_status=_status(self.OWNERS),
            transactions=self.TXNS,
            trades=self.TRADES,
            draft_squads={DRAFT: self.SQUADS_AT_GW1},
        )
        return replace(logs, **overrides)

    def _histories(self, **overrides: Any):
        return _histories(replay_draft_history(self._lost(**overrides), 4))

    def test_drafters_are_labelled_without_a_round_and_the_draft_says_so(self):
        provenance = replay_draft_history(self._lost(), 4)

        histories = _histories(provenance)
        step = histories[1].latest
        assert (step.kind, step.entry, step.gameweek, step.pick) == ("draft", ALICE, 1, None)
        assert provenance.draft is not None
        assert (provenance.draft.id, provenance.draft.rounds_known) == (DRAFT, False)

    def test_a_pre_gameweek_waiver_pickup_is_not_drafted_by_the_squad_it_joined(self):
        histories = self._histories()

        pickup = histories[10]
        assert (pickup.latest.kind, pickup.latest.entry, pickup.latest.gameweek) == ("waiver", BOB, 1)
        assert [step.kind for step in pickup.chain] == ["waiver"]

    def test_the_player_a_pre_gameweek_waiver_replaced_was_drafted_by_that_manager(self):
        histories = self._histories()

        chain = histories[5].chain
        assert [(step.kind, step.entry, step.gameweek) for step in chain] == [
            ("draft", BOB, 1), ("release", BOB, 1), ("waiver", ALICE, 3),
        ]
        assert chain[0].pick is None

    def test_a_pre_gameweek_trade_swaps_the_two_players_correctly(self):
        histories = self._histories()

        to_cam, to_alice = histories[4], histories[6]
        assert [(s.kind, s.entry, s.from_entry) for s in to_cam.chain] == [
            ("draft", ALICE, None), ("trade", CAM, ALICE),
        ]
        assert [(s.kind, s.entry, s.from_entry) for s in to_alice.chain] == [
            ("draft", CAM, None), ("trade", ALICE, CAM),
        ]

    def test_a_pre_gameweek_free_agent_signing_is_the_signing_and_the_player_he_replaced_is_gone(self):
        histories = self._histories()

        assert (histories[11].latest.kind, histories[11].latest.entry) == ("free_agent", CAM)
        assert 9 not in histories

    def test_moves_after_the_gameweek_follow_the_acquisition_rule_from_there(self):
        histories = self._histories()

        assert (histories[8].latest.kind, histories[8].latest.entry, histories[8].latest.gameweek) == ("trade", CAM, 4)
        assert [s.kind for s in histories[8].chain] == ["draft", "trade"]
        assert histories[8].chain[0].entry == BOB

    def test_a_replay_of_the_drafts_own_gameweek_reads_the_squads_it_was_rebuilt_from(self):
        provenance = replay_draft_history(self._lost(), 1, observed=self.SQUADS_AT_GW1)

        histories = _histories(provenance)
        assert (histories[4].owner, histories[4].latest.kind, histories[4].latest.gameweek) == (CAM, "trade", 1)
        assert histories[6].owner == ALICE

    def test_an_earlier_gameweek_is_held_to_the_squads_it_recorded(self):
        gw3 = {1: ALICE, 5: ALICE, 6: ALICE, 2: BOB, 8: BOB, 10: BOB, 3: CAM, 4: CAM, 11: CAM}

        assert replay_draft_history(self._lost(), 3, observed=gw3).available
        assert not replay_draft_history(self._lost(), 3, observed={**gw3, 8: CAM}).available

    def test_the_same_checks_gate_the_labels_as_when_the_picks_are_known(self):
        off_list = replay_draft_history(self._lost(element_status=_status({**self.OWNERS, 2: ALICE})), 4)
        off_squads = replay_draft_history(self._lost(), 4, observed={**self.OWNERS, 2: ALICE})

        assert not off_list.available and off_list.players == {}
        assert "owned differently from FPL's own ownership list" in (off_list.problem or "")
        assert not off_squads.available and off_squads.players == {}
        assert "different squad" in (off_squads.problem or "")

    def test_a_trades_log_that_did_not_come_back_refuses_the_rebuild(self):
        """The two checks cannot see a missing GW1 trade when the start is derived
        from the same logs, so what could hide one refuses instead."""
        provenance = replay_draft_history(self._lost(trades=None), 4)

        assert not provenance.available and provenance.players == {}
        assert "the trades could not be read" in (provenance.problem or "")

    def test_a_pre_gameweek_trade_in_a_state_nobody_has_seen_move_players_refuses_the_rebuild(self):
        pending = _trade(3, ALICE, BOB, [(1, 2)], 1, "2026-08-12T11:00:00Z", state="o")

        provenance = replay_draft_history(self._lost(trades=[*self.TRADES, pending]), 4)

        assert not provenance.available
        assert "state this client has not seen move players" in (provenance.problem or "")

    def test_a_vetoed_pre_gameweek_trade_changes_nothing(self):
        vetoed = _trade(3, ALICE, BOB, [(1, 2)], 1, "2026-08-12T11:00:00Z", state="v")

        assert replay_draft_history(self._lost(trades=[*self.TRADES, vetoed]), 4).available

    def test_a_pre_gameweek_move_that_does_not_fit_the_squads_refuses_the_rebuild(self):
        """A claim for a player the manager's GW1 squad does not have."""
        bad = _txn(4, ALICE, 12, 7, 1, "2026-08-12T09:00:00Z")

        provenance = replay_draft_history(self._lost(transactions=[*self.TXNS, bad]), 4)

        assert not provenance.available and provenance.players == {}
        assert "brought in a player" in (provenance.problem or "")

    def test_squads_that_could_not_all_be_read_refuse_the_rebuild_and_say_how_many(self):
        provenance = replay_draft_history(
            self._lost(draft_squads={}, unread_squads={DRAFT: (ALICE, BOB)}), 4,
        )

        assert not provenance.available
        assert "squads of 2 managers could not be read" in (provenance.problem or "")
        assert "neither FPL's choices list nor a saved snapshot" in (provenance.problem or "")

    def test_no_squads_at_all_is_the_unavailable_message_as_before(self):
        provenance = replay_draft_history(self._lost(draft_squads={}), 4)

        assert "neither FPL's choices list nor a saved snapshot holds the picks of the GW1 draft" == provenance.problem

    def test_a_snapshot_appearing_later_promotes_the_same_draft_to_known_rounds(self):
        before = replay_draft_history(self._lost(), 4)
        after = replay_draft_history(self._lost(saved_choices={DRAFT: FIRST_DRAFT}), 4)

        assert before.draft is not None and before.draft.rounds_known is False
        assert after.draft is not None and after.draft.rounds_known is True
        pick = _histories(after)[1].latest.pick
        assert pick is not None and (pick.round, pick.pick) == (1, 1)
        # The same chains otherwise: the rebuild and the picks agree about who drafted whom.
        assert {e: h.owner for e, h in before.players.items()} == {e: h.owner for e, h in after.players.items()}
        assert [s.kind for s in _histories(after)[5].chain] == [s.kind for s in _histories(before)[5].chain]

    def test_the_feeds_picks_win_over_the_rebuild(self):
        provenance = replay_draft_history(self._lost(choices=FIRST_DRAFT), 4)

        assert provenance.draft is not None and provenance.draft.rounds_known is True


class TestReadingTheSquadsAtTheDraftsGameweek:
    """Squads are read only for a draft whose picks neither source holds, one
    request per manager, so a league with its picks pays nothing for the rebuild."""

    def _client(self, squads: dict[int, list[int]] | None = None, failing: int | None = None) -> MagicMock:
        squads = squads or {ALICE: [1, 6, 7], BOB: [2, 8, 10], CAM: [3, 4, 11]}

        async def picks(entry: int, gameweek: int) -> dict[str, Any]:
            if entry == failing:
                raise httpx.ConnectError("down")
            return {"picks": [{"element": e, "position": n} for n, e in enumerate(squads[entry], start=1)]}

        client = MagicMock()
        client.get_draft_choices = AsyncMock(return_value={"choices": [], "element_status": _status({}), "idle": []})
        client.get_league_trades = AsyncMock(return_value={"trades": []})
        client.get_entry_picks = AsyncMock(side_effect=picks)
        return client

    async def _read(self, client: MagicMock, **kwargs: Any) -> DraftLogs:
        return await read_draft_logs(
            client, 7, drafts=[_draft_row(DRAFT, 1, DRAFTED_AT)], transactions=[],
            entry_ids=[ALICE, BOB, CAM], **kwargs,
        )

    async def test_each_managers_squad_is_read_at_the_drafts_gameweek(self):
        client = self._client()

        logs = await self._read(client, gameweek=5)

        assert sorted(call.args for call in client.get_entry_picks.await_args_list) == [
            (ALICE, 1), (BOB, 1), (CAM, 1),
        ]
        assert logs.draft_squads == {DRAFT: {1: ALICE, 6: ALICE, 7: ALICE, 2: BOB, 8: BOB, 10: BOB, 3: CAM, 4: CAM, 11: CAM}}
        assert logs.unread_squads == {}

    async def test_a_draft_with_its_picks_costs_no_squad_reads(self):
        client = self._client()
        client.get_draft_choices = AsyncMock(return_value={
            "choices": FIRST_DRAFT, "element_status": _status({}), "idle": [],
        })

        logs = await self._read(client, gameweek=5)

        client.get_entry_picks.assert_not_awaited()
        assert logs.draft_squads == {}

    async def test_nobody_is_asked_without_the_entries(self):
        client = self._client()

        await read_draft_logs(client, 7, drafts=[_draft_row(DRAFT, 1, DRAFTED_AT)], transactions=[])

        client.get_entry_picks.assert_not_awaited()

    async def test_one_squad_that_fails_leaves_the_set_unusable_and_names_who(self):
        logs = await self._read(self._client(failing=BOB), gameweek=5)

        assert logs.draft_squads == {}
        assert logs.unread_squads == {DRAFT: (BOB,)}

    async def test_a_player_in_two_squads_at_once_cannot_be_rebuilt_from(self):
        client = self._client({ALICE: [1, 6, 7], BOB: [2, 6, 10], CAM: [3, 4, 11]})

        logs = await self._read(client, gameweek=5)

        assert logs.draft_squads == {}
        assert len(logs.unread_squads[DRAFT]) >= 1

    async def test_the_whole_helper_labels_drafters_from_the_squads(self):
        txns = TestWhenThePicksAreLost.TXNS
        client = self._client()
        client.get_league_trades = AsyncMock(return_value={"trades": TestWhenThePicksAreLost.TRADES})
        client.get_draft_choices = AsyncMock(return_value={
            "choices": [], "element_status": _status(TestWhenThePicksAreLost.OWNERS), "idle": [],
        })

        provenance = await get_draft_provenance(
            client, 7, 4, drafts=[_draft_row(DRAFT, 1, DRAFTED_AT)], transactions=txns,
            entry_ids=[ALICE, BOB, CAM], observed=TestWhenThePicksAreLost.OWNERS,
        )

        assert provenance.available, provenance.problem
        assert provenance.draft is not None and provenance.draft.rounds_known is False
        assert provenance.players[1].latest.kind == "draft"
