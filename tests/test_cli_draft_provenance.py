"""Tests for how the lookup commands say where a draft player came from (#460).

Built on the replay's own placeholder league (`tests/test_draft_provenance.py`):
three managers, nine drafted players and three in the pool, and after the draft

    GW2  Bob claims 10 on waivers, releasing 5
    GW3  Alice signs 11 as a free agent, releasing 7; Cam signs 12, releasing 3
    GW4  trade: Alice gives 4 to Bob for 8
    GW5  Cam claims 3 on waivers (his own first-round pick), releasing 12

so the league ends with Alice on 1, 8, 11; Bob on 2, 4, 10; Cam on 3, 6, 9, and
5, 7 and 12 back in the pool. Player 13 is in nobody's history at all.
"""

from __future__ import annotations

from dataclasses import replace

from fpl_cli.cli._draft_provenance import (
    DRAFT_PROVENANCE_UNAVAILABLE,
    STATS_TAG_KEY,
    history_text,
    ownership_note,
    player_standing,
    provenance_warning,
    standing_json,
    standings_tagged,
    stats_tag,
)
from fpl_cli.services.draft_provenance import DraftLogs, DraftProvenance, replay_draft_history
from tests.test_draft_provenance import (
    ALICE,
    BOB,
    CAM,
    DRAFT,
    DRAFTED_AT,
    FIRST_DRAFT,
    OWNERS_NOW,
    REDRAFT,
    REDRAFTED_AT,
    TRADES,
    TRANSACTIONS,
    _draft_row,
    _logs,
    _pick,
    _status,
    _trade,
    _txn,
)

NAMES = {ALICE: "Alice", BOB: "Bob", CAM: "Cam"}
NEVER_HELD = 13


def _provenance(logs: DraftLogs | None = None, gameweek: int = 5) -> DraftProvenance:
    provenance = replay_draft_history(logs or _logs(), gameweek)
    assert provenance.available, provenance.problem
    return provenance


def _owned(element: int, provenance: DraftProvenance | None = None):
    provenance = provenance or _provenance()
    standing = player_standing(provenance, element, provenance.players[element].owner)
    assert standing is not None
    return standing


def _free(element: int, provenance: DraftProvenance | None = None):
    standing = player_standing(provenance or _provenance(), element, None)
    assert standing is not None
    return standing


class TestTheLineSaysHowHeCameToHisOwner:
    def test_a_player_still_with_the_manager_who_drafted_him_reads_as_the_pick(self):
        standing = _owned(1)

        assert ownership_note(standing, NAMES) == "round-1 pick"
        assert history_text(standing, NAMES) is None

    def test_an_auto_pick_says_so_rather_than_claim_the_manager_chose_him(self):
        assert ownership_note(_owned(6), NAMES) == "round-2 auto-pick"

    def test_a_waiver_pickup_nobody_drafted_says_so(self):
        assert ownership_note(_owned(10), NAMES) == "GW2 waiver pickup; undrafted"

    def test_a_free_agent_signing_reads_as_the_signing(self):
        assert ownership_note(_owned(11), NAMES) == "GW3 free-agent signing; undrafted"

    def test_a_trade_names_the_manager_he_came_from_and_whose_pick_he_was(self):
        assert ownership_note(_owned(4), NAMES) == "acquired from Alice in a GW4 trade; Alice's round-2 pick"
        assert ownership_note(_owned(8), NAMES) == "acquired from Bob in a GW4 trade; Bob's round-3 pick"

    def test_a_pick_traded_on_has_no_history_line_since_the_draft_line_names_both_steps(self):
        assert history_text(_owned(4), NAMES) is None

    def test_a_pick_dropped_and_signed_again_reads_as_the_signing_with_the_pick_behind_it(self):
        standing = _owned(3)

        assert ownership_note(standing, NAMES) == "GW5 waiver pickup; Cam's round-1 pick"
        # The release is a step the draft line does not name.
        assert history_text(standing, NAMES) == "Cam's round-1 pick; released GW3; waiver to Cam GW5"

    def test_a_manager_the_league_does_not_name_is_still_named_by_entry(self):
        assert ownership_note(_owned(4), {}) == f"acquired from Team #{ALICE} in a GW4 trade; Team #{ALICE}'s round-2 pick"


class TestAFreeAgentSaysHisLastExit:
    def test_a_dropped_pick_says_who_released_him_and_whose_pick_he_was(self):
        standing = _free(5)

        assert ownership_note(standing, NAMES) == "released by Bob in GW2; Bob's round-2 pick"
        # Pick and release are both on the draft line already.
        assert history_text(standing, NAMES) is None

    def test_a_signing_let_go_again_says_he_was_never_drafted_and_the_history_has_the_signing(self):
        standing = _free(12)

        assert ownership_note(standing, NAMES) == "released by Cam in GW5; undrafted"
        assert history_text(standing, NAMES) == "free agent to Cam GW3; released GW5"

    def test_a_player_nobody_has_held_is_undrafted(self):
        standing = _free(NEVER_HELD)

        assert ownership_note(standing, NAMES) == "undrafted"
        assert history_text(standing, NAMES) is None


class TestALongChain:
    """The issue's own example: drafted, dropped, claimed, dropped, claimed."""

    def _provenance(self) -> DraftProvenance:
        txns = [
            _txn(1, ALICE, 10, 1, 2, "2026-08-20T10:00:00Z", kind="f", index=0),
            _txn(2, BOB, 1, 5, 5, "2026-09-10T10:00:00Z"),
            _txn(3, BOB, 12, 1, 6, "2026-09-17T10:00:00Z", kind="f", index=0),
            _txn(4, CAM, 1, 9, 7, "2026-09-24T10:00:00Z"),
        ]
        owners = {10: ALICE, 4: ALICE, 7: ALICE, 2: BOB, 8: BOB, 12: BOB, 1: CAM, 3: CAM, 6: CAM}
        return _provenance(replace(_logs(), transactions=txns, trades=[], element_status=_status(owners)), 7)

    def test_every_step_is_on_the_history_line_and_the_latest_on_the_draft_line(self):
        standing = _owned(1, self._provenance())

        assert ownership_note(standing, NAMES) == "GW7 waiver pickup; Alice's round-1 pick"
        assert history_text(standing, NAMES) == (
            "Alice's round-1 pick; released GW2; waiver to Bob GW5; released GW6; waiver to Cam GW7"
        )


class TestARedraft:
    """A GW6 redraft re-picks the nine; Alice then claims 10 for 5 in GW7."""

    def _provenance(self) -> DraftProvenance:
        repicks = [
            _pick(1, CAM, 1, 1, draft=REDRAFT), _pick(3, BOB, 1, 2, draft=REDRAFT),
            _pick(2, ALICE, 1, 3, draft=REDRAFT), _pick(4, CAM, 2, 1, draft=REDRAFT),
            _pick(6, BOB, 2, 2, draft=REDRAFT), _pick(5, ALICE, 2, 3, draft=REDRAFT),
            _pick(7, CAM, 3, 1, draft=REDRAFT), _pick(9, BOB, 3, 2, draft=REDRAFT),
            _pick(8, ALICE, 3, 3, draft=REDRAFT),
        ]
        owners = {1: CAM, 4: CAM, 7: CAM, 3: BOB, 6: BOB, 9: BOB, 2: ALICE, 8: ALICE, 10: ALICE}
        logs = DraftLogs(
            drafts=[_draft_row(DRAFT, 1, DRAFTED_AT), _draft_row(REDRAFT, 6, REDRAFTED_AT)],
            choices=[*FIRST_DRAFT, *repicks],
            element_status=_status(owners),
            transactions=[*TRANSACTIONS, _txn(20, ALICE, 10, 5, 7, "2026-10-08T10:00:00Z")],
            trades=TRADES,
        )
        return _provenance(logs, 7)

    def test_a_pick_names_the_redraft_it_was_made_in(self):
        assert ownership_note(_owned(1, self._provenance()), NAMES) == "round-1 pick in the GW6 redraft"

    def test_a_dropped_redraft_pick_names_the_redraft(self):
        assert ownership_note(_free(5, self._provenance()), NAMES) == (
            "released by Alice in GW7; Alice's round-2 pick in the GW6 redraft"
        )

    def test_a_player_the_redraft_passed_over_is_undrafted_in_it_whatever_the_first_draft_did(self):
        # 11 was Alice's GW3 signing under the first draft; the redraft started afresh.
        assert ownership_note(_free(11, self._provenance()), NAMES) == "undrafted in the GW6 redraft"


class TestLostRounds:
    """The draft's picks are lost, so who drafted whom is rebuilt from the GW1 squads (#458)."""

    def _provenance(self) -> DraftProvenance:
        logs = DraftLogs(
            drafts=[_draft_row(DRAFT, 1, DRAFTED_AT)],
            choices=[],
            element_status=_status(OWNERS_NOW),
            transactions=TRANSACTIONS,
            trades=TRADES,
            draft_squads={DRAFT: {e["element"]: e["entry"] for e in FIRST_DRAFT}},
        )
        provenance = _provenance(logs)
        assert provenance.draft is not None and not provenance.draft.rounds_known
        return provenance

    def test_the_drafter_is_named_and_the_round_is_not_guessed(self):
        provenance = self._provenance()

        assert ownership_note(_owned(1, provenance), NAMES) == "drafted, round unknown"
        assert ownership_note(_owned(4, provenance), NAMES) == (
            "acquired from Alice in a GW4 trade; Alice's pick, round unknown"
        )
        assert ownership_note(_free(5, provenance), NAMES) == "released by Bob in GW2; Bob's pick, round unknown"
        assert history_text(_owned(3, provenance), NAMES) == (
            "Cam's pick, round unknown; released GW3; waiver to Cam GW5"
        )

    def test_the_json_flags_the_round_as_unknown(self):
        draft = standing_json(_owned(1, self._provenance()), NAMES)

        assert draft["drafter"] == {"entry_id": ALICE, "name": "Alice"}
        assert (draft["round"], draft["pick"], draft["round_unknown"]) == (None, None, True)

    def test_the_stats_tag_says_the_round_is_unknown(self):
        assert stats_tag(_owned(1, self._provenance())) == "R?"


class TestNobodyIsDescribedWithoutAHistoryThatAgrees:
    def test_a_failed_read_describes_nobody_and_says_why(self):
        provenance = replay_draft_history(_logs(trades=None), 5)

        assert player_standing(provenance, 1, ALICE) is None
        assert player_standing(provenance, NEVER_HELD, None) is None
        warning = provenance_warning(provenance)
        assert warning is not None and warning["code"] == DRAFT_PROVENANCE_UNAVAILABLE
        assert "trades could not be read" in warning["message"]

    def test_a_replay_that_ends_away_from_fpls_ownership_list_describes_nobody(self):
        provenance = replay_draft_history(_logs(element_status=_status({**OWNERS_NOW, 5: ALICE})), 5)

        assert player_standing(provenance, 1, ALICE) is None
        assert provenance_warning(provenance) is not None

    def test_a_player_shown_with_another_owner_is_not_described(self):
        assert player_standing(_provenance(), 1, BOB) is None

    def test_a_player_shown_available_whom_the_history_holds_is_not_described(self):
        # A squad the ownership read missed shows its players available (#438).
        assert player_standing(_provenance(), 1, None) is None

    def test_a_player_with_no_draft_id_is_not_described(self):
        assert player_standing(_provenance(), None, None) is None

    def test_no_history_asked_for_is_no_warning(self):
        assert player_standing(None, 1, ALICE) is None
        assert provenance_warning(None) is None
        assert provenance_warning(_provenance()) is None


class TestTheJson:
    def test_a_pick_carries_its_round_pick_and_drafter(self):
        draft = standing_json(_owned(1), NAMES)

        assert draft == {
            "owner": {"entry_id": ALICE, "name": "Alice"},
            "acquisition": "draft",
            "gameweek": 1,
            "draft_id": DRAFT,
            "draft_gameweek": 1,
            "redraft": False,
            "drafter": {"entry_id": ALICE, "name": "Alice"},
            "round": 1,
            "pick": 1,
            "overall_pick": 1,
            "auto_pick": False,
            "round_unknown": False,
            "counterparty": None,
            "history": [{
                "kind": "draft", "gameweek": 1, "manager": {"entry_id": ALICE, "name": "Alice"},
                "from_manager": None, "round": 1, "pick": 1, "overall_pick": 1, "auto_pick": False,
            }],
        }

    def test_a_trade_names_its_counterparty_and_keeps_the_pick_behind_it(self):
        draft = standing_json(_owned(4), NAMES)

        assert (draft["acquisition"], draft["gameweek"]) == ("trade", 4)
        assert draft["owner"] == {"entry_id": BOB, "name": "Bob"}
        assert draft["counterparty"] == {"entry_id": ALICE, "name": "Alice"}
        assert (draft["drafter"], draft["round"], draft["pick"]) == ({"entry_id": ALICE, "name": "Alice"}, 2, 1)
        assert [step["kind"] for step in draft["history"]] == ["draft", "trade"]
        assert draft["history"][1]["from_manager"] == {"entry_id": ALICE, "name": "Alice"}

    def test_a_released_player_names_who_let_him_go(self):
        draft = standing_json(_free(5), NAMES)

        assert (draft["owner"], draft["acquisition"], draft["gameweek"]) == (None, "released", 2)
        assert draft["counterparty"] == {"entry_id": BOB, "name": "Bob"}
        assert draft["drafter"] == {"entry_id": BOB, "name": "Bob"}

    def test_an_undrafted_free_agent_has_no_draft_facts_and_no_history(self):
        draft = standing_json(_free(NEVER_HELD), NAMES)

        assert (draft["acquisition"], draft["gameweek"], draft["drafter"], draft["round"]) == (
            "undrafted", None, None, None,
        )
        assert (draft["round_unknown"], draft["counterparty"], draft["history"]) == (False, None, [])
        assert draft["draft_id"] == DRAFT


class TestTheStatsTag:
    def test_each_acquisition_has_a_short_tag(self):
        assert stats_tag(_owned(1)) == "R1"
        assert stats_tag(_owned(10)) == "GW2 W"
        assert stats_tag(_owned(11)) == "GW3 FA"
        assert stats_tag(_owned(4)) == "GW4 T"

    def test_the_key_names_every_tag(self):
        assert STATS_TAG_KEY == "Draft: R3 = round-3 pick (R? round unknown), W = waiver, FA = free agent, T = trade"

    def test_a_free_agent_has_no_tag_and_a_table_of_them_needs_no_key(self):
        assert stats_tag(_free(5)) is None
        assert stats_tag(_free(NEVER_HELD)) is None
        assert not standings_tagged([_free(5), None])
        assert standings_tagged([_free(5), _owned(1)])


def test_a_trade_with_more_than_one_item_moves_each_player_with_his_own_chain():
    # Two players each way in one trade, read from each player's own side.
    trade = _trade(9, ALICE, BOB, [(1, 2), (7, 5)], 4, "2026-09-03T12:00:00Z")
    owners = {2: ALICE, 4: ALICE, 5: ALICE, 1: BOB, 7: BOB, 8: BOB, 3: CAM, 6: CAM, 9: CAM}
    provenance = _provenance(replace(_logs(), transactions=[], trades=[trade], element_status=_status(owners)))

    assert ownership_note(_owned(7, provenance), NAMES) == "acquired from Alice in a GW4 trade; Alice's round-3 pick"
    assert ownership_note(_owned(5, provenance), NAMES) == "acquired from Bob in a GW4 trade; Bob's round-2 pick"
