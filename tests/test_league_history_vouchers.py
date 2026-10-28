"""Tests for the draft Player of the Month voucher (issue #492): who held the
winner, read off the ledger, and the record that has each month announced once."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from fpl_cli.api.premier_league import AwardsFeedError, MonthlyAward, parse_monthly_awards
from fpl_cli.cli._fines_config import VoucherRule
from fpl_cli.cli._league_recap_vouchers import (
    RECAP_WARNING_POTM_RECORD_CONFLICT,
    RECAP_WARNING_POTM_UNCHECKED,
    RECAP_WARNING_POTM_UNRECORDED,
    PlayerOfTheMonthCapture,
    capture_player_of_the_month,
    holding_line,
    player_of_the_month_line,
    player_of_the_month_unchecked_line,
    recap_vouchers,
    serialize_player_of_the_month,
)
from fpl_cli.models.league_history import LedgerPlayer
from fpl_cli.models.season_month import SeasonMonth
from fpl_cli.services.league_history import LeagueHistoryStore
from fpl_cli.services.league_history_vouchers import (
    AwardRecord,
    award_record_path,
    calendar_month_key,
    read_award_record,
)
from tests.conftest import make_history_row, make_season_months

SEASON = "2026-27"
LEAGUE = 7
MONTHS = make_season_months()
SEPTEMBER = next(month for month in MONTHS if month.name == "September")  # GW3-5
GROSS = 60307  # Pascal Groß's FPL code, September 2026's Player of the Month
JOAO = 475168  # João Pedro's, August's
NOW = datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc)
VOUCHER = VoucherRule(type="player-of-the-month", penalty="Pint on video")
# The bootstrap's names by code: the check that a winner's code is real.
NAMES = {GROSS: "Groß", JOAO: "João Pedro", 111: "Filler"}

# (manager key, name) for the league's three managers.
ALICE, BOB, CY = (10, "Alice"), (11, "Bob"), (12, "Cy")


def _store() -> LeagueHistoryStore:
    return LeagueHistoryStore(SEASON, "draft", LEAGUE)


def _squad(*codes: int | None) -> list[LedgerPlayer]:
    """A squad of these codes, bench included: the last is a substitute."""
    return [
        LedgerPlayer(
            name="Groß" if code == GROSS else f"Player {code}", team="BHA", position="MID", code=code,
            contributed=index < len(codes) - 1, unmatched=code is None,
        )
        for index, code in enumerate(codes)
    ]


def _record(store: LeagueHistoryStore, gameweek: int, holder: tuple[int, str] | None, **unknown: bool) -> None:
    """Every manager's row for one gameweek, `holder` with Groß on his bench
    and the rest without him; a manager named in `unknown` gets an
    unknown-status row with no squad."""
    rows = []
    for key, name in (ALICE, BOB, CY):
        if unknown.get(name.lower()):
            rows.append(make_history_row(
                season=SEASON, fpl_format="draft", league_id=LEAGUE, gameweek=gameweek,
                manager_key=key, manager_name=name, capture_status="unknown",
            ))
            continue
        codes = (111, GROSS) if holder is not None and holder[0] == key else (111, 222 + key)
        rows.append(make_history_row(
            season=SEASON, fpl_format="draft", league_id=LEAGUE, gameweek=gameweek,
            manager_key=key, manager_name=name, gross_points=50, squad=_squad(*codes),
        ))
    store.append_rows(gameweek, rows)


def _award(code: int = GROSS, month: str = "2026-9", award_type: str = "POTM") -> MonthlyAward:
    (award,) = parse_monthly_awards({"player_awards": [
        {"type": award_type, "date": month, "id": str(code), "name": {"display": f"Feed name {code}"}},
    ]})
    return award


def _feed(*awards: MonthlyAward) -> AsyncMock:
    return AsyncMock(return_value=list(awards))


async def _capture(
    gameweek: int = 6, *, feed: AsyncMock | None = None, may_announce: bool = True,
    voucher: VoucherRule = VOUCHER, months: list[SeasonMonth] | None = None,
    league_start_gameweek: int = 1, names: dict[int, str] | None = None,
) -> PlayerOfTheMonthCapture:
    return await capture_player_of_the_month(
        _store(), MONTHS if months is None else months, gameweek,
        voucher=voucher, fetch_awards=feed if feed is not None else _feed(_award()),
        may_announce=may_announce, league_start_gameweek=league_start_gameweek,
        bootstrap_names=NAMES if names is None else names, now=NOW,
    )


def _september(capture: PlayerOfTheMonthCapture):
    return next(potm for potm in capture.fold.months if potm.month_key == "2026-09")


# ---------------------------------------------------------------------------
# FPL's months against the calendar's
# ---------------------------------------------------------------------------


class TestCalendarMonths:
    @pytest.mark.parametrize(("name", "key"), [
        ("August", "2026-08"), ("September", "2026-09"), ("December", "2026-12"),
        ("January", "2027-01"), ("May", "2027-05"),
    ])
    def test_each_month_is_dated_in_the_year_the_season_reaches_it(self, name, key):
        month = next(month for month in MONTHS if month.name == name)

        assert calendar_month_key(month, 2026) == key

    def test_a_name_that_is_no_month_has_no_key(self):
        assert calendar_month_key(SEPTEMBER.model_copy(update={"name": "Overall"}), 2026) is None


# ---------------------------------------------------------------------------
# Who held him
# ---------------------------------------------------------------------------


class TestOwner:
    async def test_held_all_month_gives_the_holder_the_voucher_with_no_split_line(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)

        potm = _september(await _capture())

        assert potm.owner is not None and potm.owner.manager_name == "Alice"
        assert not potm.changed_hands
        assert player_of_the_month_line(potm) == "Groß for September (GW3-5), owned by Alice at the month's end"

    async def test_bench_counts_as_holding_him(self):
        """`_record` puts him on the bench: the squad's last, non-contributing slot."""
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, BOB)

        potm = _september(await _capture())

        assert potm.owner is not None and potm.owner.manager_key == BOB[0]

    async def test_changing_hands_gives_the_month_end_holder_the_voucher_and_names_both(self):
        store = _store()
        _record(store, 3, BOB)
        _record(store, 4, BOB)
        _record(store, 5, ALICE)

        potm = _september(await _capture())

        assert potm.owner is not None and potm.owner.manager_name == "Alice"
        assert player_of_the_month_line(potm) == (
            "Groß for September (GW3-5), owned by Alice at the month's end. "
            "Held by Bob in GW3-4 and Alice in GW5, so the voucher goes to Alice"
        )

    async def test_held_earlier_but_a_free_agent_at_the_months_end_gives_no_voucher(self):
        store = _store()
        _record(store, 3, BOB)
        _record(store, 4, BOB)
        _record(store, 5, None)

        potm = _september(await _capture())

        assert potm.owner is None and potm.unowned
        assert player_of_the_month_line(potm) == (
            "Groß for September (GW3-5), a free agent at the month's end, so there is no voucher. "
            "Held by Bob in GW3-4 and nobody in GW5"
        )

    async def test_a_free_agent_all_month_gives_no_voucher_and_no_split_line(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, None)

        potm = _september(await _capture())

        assert potm.unowned and not potm.changed_hands
        assert player_of_the_month_line(potm) == (
            "Groß for September (GW3-5), a free agent at the month's end, so there is no voucher"
        )

    async def test_an_unknown_row_at_the_months_end_withholds_the_voucher_with_the_reason(self):
        """The missing row could be the owner's."""
        store = _store()
        _record(store, 3, ALICE)
        _record(store, 4, ALICE)
        _record(store, 5, None, cy=True)

        potm = _september(await _capture())

        assert potm.owner is None and not potm.unowned
        assert potm.withheld_reason == "no squad is recorded for Cy in GW5"
        assert player_of_the_month_line(potm) == (
            "Groß for September (GW3-5): the voucher is withheld, as no squad is recorded for Cy in GW5"
        )

    async def test_an_unknown_row_elsewhere_at_the_months_end_leaves_a_found_owner_standing(self):
        """Exclusive ownership: once he is in Alice's squad, Cy's missing row
        cannot be his owner's."""
        store = _store()
        for gameweek in (3, 4):
            _record(store, gameweek, ALICE)
        _record(store, 5, ALICE, cy=True)

        potm = _september(await _capture())

        assert potm.owner is not None and potm.owner.manager_name == "Alice"
        assert potm.withheld_reason is None

    async def test_a_player_never_identified_in_a_month_end_squad_withholds_the_voucher(self):
        """An unmatched draft pick has no code, and could be him."""
        store = _store()
        for gameweek in (3, 4):
            _record(store, gameweek, None)
        store.append_rows(5, [
            make_history_row(
                season=SEASON, fpl_format="draft", league_id=LEAGUE, gameweek=5, manager_key=key,
                manager_name=name, gross_points=50, squad=_squad(111, None if key == BOB[0] else 333),
            )
            for key, name in (ALICE, BOB, CY)
        ])

        potm = _september(await _capture())

        assert potm.withheld_reason == "a player in Bob's GW5 squad could not be identified"

    async def test_a_month_end_never_captured_withholds_the_voucher(self):
        store = _store()
        _record(store, 3, ALICE)
        _record(store, 4, ALICE)

        potm = _september(await _capture())

        assert potm.withheld_reason == "GW5 was never captured"

    async def test_a_gap_earlier_in_the_month_only_qualifies_the_split_line(self):
        store = _store()
        _record(store, 3, BOB)
        _record(store, 4, None, alice=True)
        _record(store, 5, ALICE)

        potm = _september(await _capture())

        assert potm.owner is not None and potm.owner.manager_name == "Alice"
        assert holding_line(potm) == "Held by Bob in GW3 and Alice in GW5 (GW4 not recorded), so the voucher goes to Alice"

    async def test_a_gap_between_the_same_holder_is_no_change_of_hands(self):
        store = _store()
        _record(store, 3, ALICE)
        _record(store, 5, ALICE)

        potm = _september(await _capture())

        assert not potm.changed_hands
        assert player_of_the_month_line(potm) == "Groß for September (GW3-5), owned by Alice at the month's end"

    async def test_a_month_the_league_began_partway_through_counts_from_its_first_gameweek(self):
        store = _store()
        _record(store, 4, BOB)
        _record(store, 5, ALICE)

        potm = _september(await _capture(league_start_gameweek=4))

        assert [holder.gameweek for holder in potm.holders] == [4, 5]
        assert holding_line(potm) == "Held by Bob in GW4 and Alice in GW5, so the voucher goes to Alice"

    async def test_a_month_ended_before_the_league_began_is_left_out(self):
        store = _store()
        _record(store, 6, ALICE)

        capture = await _capture(league_start_gameweek=6, feed=_feed(_award(JOAO, "2026-8"), _award()))

        assert capture.fold.months == []


# ---------------------------------------------------------------------------
# Announcing each month once
# ---------------------------------------------------------------------------


class TestAnnouncement:
    async def test_the_latest_gameweeks_recap_announces_a_new_award_and_records_it(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)

        capture = await _capture(8)

        assert [potm.month_key for potm in capture.fold.announced_at(8)] == ["2026-09"]
        record, problem = read_award_record(SEASON, LEAGUE, "2026-09")
        assert problem is None and record is not None
        assert (record.code, record.source, record.announced_gameweek, record.first_seen_at) == (GROSS, "feed", 8, NOW)
        assert record.entry is not None and record.entry["id"] == str(GROSS)
        assert capture.saved == [award_record_path(SEASON, LEAGUE, "2026-09")]

    async def test_a_later_recap_does_not_announce_it_again_and_needs_no_feed(self):
        """Every month completed by GW7 is recorded at GW6, so GW7 asks nothing."""
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        await _capture(6, feed=_feed(_award(JOAO, "2026-8"), _award()))
        feed = _feed(_award())

        later = await _capture(7, feed=feed)

        assert later.fold.announced_at(7) == []
        assert _september(later).announced_gameweek == 6
        feed.assert_not_awaited()

    async def test_re_running_the_announcing_gameweek_announces_it_again_and_rewrites_nothing(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        await _capture(8)
        path = award_record_path(SEASON, LEAGUE, "2026-09")
        written = path.read_text(encoding="utf-8")

        again = await _capture(8, may_announce=False)

        assert [potm.month_key for potm in again.fold.announced_at(8)] == ["2026-09"]
        assert again.saved == []
        assert path.read_text(encoding="utf-8") == written

    async def test_a_skipped_recap_still_gets_it_next_time(self):
        """Published after GW7's recap, no recap for GW8: GW9's announces it."""
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        before = await _capture(7, feed=_feed())
        assert before.fold.months == [] and before.fold.unchecked == []

        capture = await _capture(9)

        assert [potm.month_key for potm in capture.fold.announced_at(9)] == ["2026-09"]

    async def test_a_replay_of_an_older_gameweek_never_claims_an_announcement(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)

        replay = await _capture(6, may_announce=False)

        assert replay.fold.announced_at(6) == []
        assert _september(replay).announced_gameweek is None
        assert read_award_record(SEASON, LEAGUE, "2026-09") == (None, None)

    async def test_two_months_waiting_are_both_announced_once(self):
        store = _store()
        for gameweek in (1, 2):
            _record(store, gameweek, BOB)
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)

        capture = await _capture(8, feed=_feed(_award(JOAO, "2026-8"), _award()))

        assert [potm.month_key for potm in capture.fold.announced_at(8)] == ["2026-08", "2026-09"]

    async def test_a_month_not_yet_awarded_is_said_nothing_of(self):
        """September's award is not out yet; August's is, and is announced,
        withheld for the gameweeks this ledger never captured."""
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)

        capture = await _capture(6, feed=_feed(_award(JOAO, "2026-8")))

        (august,) = capture.fold.months
        assert august.month_key == "2026-08"
        assert august.withheld_reason == "GW2 was never captured"
        assert capture.fold.unchecked == [] and capture.warnings == []

    async def test_a_record_that_cannot_be_written_still_shows_this_run_and_says_so(self, monkeypatch):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        monkeypatch.setattr(
            "fpl_cli.services.league_history_vouchers.atomic_write_text",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )

        capture = await _capture(8)

        assert [potm.month_key for potm in capture.fold.announced_at(8)] == ["2026-09"]
        assert [w["code"] for w in capture.warnings] == [RECAP_WARNING_POTM_UNRECORDED]
        assert "disk full" in capture.warnings[0]["message"]
        assert capture.saved == []


# ---------------------------------------------------------------------------
# The winner: feed, override and record
# ---------------------------------------------------------------------------


class TestWinner:
    async def test_the_override_wins_over_the_feed(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        override = VoucherRule(type="player-of-the-month", overrides={"2026-09": 111})

        potm = _september(await _capture(8, voucher=override))

        assert (potm.code, potm.source, potm.name) == (111, "override", "Filler")
        assert potm.owner is not None  # everyone has 111 in `_record`'s squads
        record, _ = read_award_record(SEASON, LEAGUE, "2026-09")
        assert record is not None and (record.source, record.entry) == ("override", None)

    async def test_an_override_needs_no_feed(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        feed = AsyncMock(side_effect=httpx.ConnectError("blocked"))
        override = VoucherRule(type="player-of-the-month", overrides={"2026-08": JOAO, "2026-09": GROSS})

        capture = await _capture(8, voucher=override, feed=feed)

        feed.assert_not_awaited()
        assert capture.fold.unchecked == []
        assert [potm.month_key for potm in capture.fold.announced_at(8)] == ["2026-08", "2026-09"]

    async def test_a_recorded_winner_stands_and_the_feed_naming_another_is_said(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        await _capture(8)

        # October (GW6-9) is pending by GW10, so the feed is read, and it now
        # names someone else for September.
        capture = await _capture(10, feed=_feed(_award(JOAO, "2026-9")))

        assert _september(capture).code == GROSS
        assert [w["code"] for w in capture.warnings] == [RECAP_WARNING_POTM_RECORD_CONFLICT]
        assert "player 475168" in capture.warnings[0]["message"]

    async def test_a_record_that_cannot_be_read_is_left_alone_and_not_announced_again(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        path = award_record_path(SEASON, LEAGUE, "2026-09")
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        capture = await _capture(8)

        assert capture.fold.announced_at(8) == []
        assert path.read_text(encoding="utf-8") == "{not json"
        assert [w["code"] for w in capture.warnings] == [RECAP_WARNING_POTM_RECORD_CONFLICT]
        assert "is not valid JSON" in capture.warnings[0]["message"]

    def test_a_record_reads_back_what_was_written(self, tmp_path):
        record = AwardRecord(
            month_key="2026-09", code=GROSS, source="feed", announced_gameweek=8,
            first_seen_at=NOW, entry={"id": "60307"},
        )
        path = award_record_path(SEASON, LEAGUE, "2026-09")
        path.parent.mkdir(parents=True)
        path.write_text(record.to_json(), encoding="utf-8")

        assert read_award_record(SEASON, LEAGUE, "2026-09") == (record, None)

    @pytest.mark.parametrize("stored", [
        [], {"month": "2026-10", "code": GROSS, "source": "feed", "announced_gameweek": 8, "first_seen_at": NOW.isoformat()},
        {"month": "2026-09", "code": "60307", "source": "feed", "announced_gameweek": 8, "first_seen_at": NOW.isoformat()},
        {"month": "2026-09", "code": GROSS, "source": "guess", "announced_gameweek": 8, "first_seen_at": NOW.isoformat()},
        {"month": "2026-09", "code": GROSS, "source": "feed", "announced_gameweek": 8, "first_seen_at": "soon"},
    ], ids=["not-an-object", "another-month", "quoted-code", "unknown-source", "no-time"])
    def test_a_record_this_cannot_read_says_so(self, stored):
        path = award_record_path(SEASON, LEAGUE, "2026-09")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(stored), encoding="utf-8")

        record, problem = read_award_record(SEASON, LEAGUE, "2026-09")

        assert record is None and problem is not None


# ---------------------------------------------------------------------------
# Not checked, never "no award"
# ---------------------------------------------------------------------------


class TestNotChecked:
    @pytest.mark.parametrize(("error", "said"), [
        (httpx.ConnectError("blocked"), "ConnectError; where outbound hosts are restricted"),
        (httpx.HTTPStatusError(
            "503", request=httpx.Request("GET", "https://x"), response=httpx.Response(503),
        ), "HTTP 503"),
        (AwardsFeedError("the response carries no `player_awards` list"), "so its shape may have changed"),
        (json.JSONDecodeError("Expecting value", "<html>", 0), "the response is not JSON"),
    ], ids=["blocked", "refused", "reshaped", "not-json"])
    async def test_an_unreadable_feed_is_not_checked_never_no_award(self, error, said):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)

        capture = await _capture(8, feed=AsyncMock(side_effect=error))

        assert capture.fold.months == [] and capture.fold.announced_at(8) == []
        assert said in capture.fold.unchecked[0]
        line = player_of_the_month_unchecked_line(capture)
        assert line is not None and line.startswith("not checked, as the Premier League's awards could not be read (")
        assert [w["code"] for w in capture.warnings] == [RECAP_WARNING_POTM_UNCHECKED]
        assert read_award_record(SEASON, LEAGUE, "2026-09") == (None, None)

    async def test_a_feed_down_with_every_month_recorded_is_never_asked(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        await _capture(6, feed=_feed(_award(JOAO, "2026-8"), _award()))
        feed = AsyncMock(side_effect=httpx.ConnectError("blocked"))

        capture = await _capture(8, feed=feed)

        feed.assert_not_awaited()
        assert player_of_the_month_unchecked_line(capture) is None

    async def test_two_winners_for_one_month_are_not_checked(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)

        capture = await _capture(8, feed=_feed(_award(), _award(JOAO)))

        assert capture.fold.months == []
        assert capture.fold.unchecked == ["the Premier League names 2 Players of the Month for September (GW3-5)"]

    async def test_a_winner_neither_fpl_nor_the_ledger_knows_is_not_checked_or_announced(self):
        """What a feed whose ids drifted off FPL's codes looks like: reading
        it as nobody's would announce a wrong answer."""
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)

        capture = await _capture(8, feed=_feed(_award(999_999)))

        assert capture.fold.months == []
        assert capture.fold.unchecked == [
            "the Premier League's player id 999999 for September (GW3-5) matches no FPL player",
        ]
        assert read_award_record(SEASON, LEAGUE, "2026-09") == (None, None)

    async def test_a_winner_who_has_left_fpl_but_was_held_is_known(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)

        potm = _september(await _capture(8, names={}))

        assert potm.owner is not None
        # Named as the month's squads recorded him.
        assert potm.name == "Groß"

    async def test_an_override_typo_is_not_checked(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        typo = VoucherRule(type="player-of-the-month", overrides={"2026-09": 6037})

        capture = await _capture(8, voucher=typo)

        assert capture.fold.unchecked == ["the override's player 6037 for September (GW3-5) matches no FPL player"]


# ---------------------------------------------------------------------------
# What each surface reads
# ---------------------------------------------------------------------------


class TestSurfaces:
    async def test_the_voucher_names_the_holder_and_the_forfeit(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        capture = await _capture(8)

        (voucher,) = recap_vouchers(capture, 8, VOUCHER)

        assert voucher["lead"] == "Player of the Month voucher (September)"
        assert voucher["detail"] == "Alice may hand out one fine to anyone they choose (Pint on video)"
        assert (voucher["manager_key"], voucher["player"], voucher["penalty"]) == (ALICE[0], "Groß", "Pint on video")

    async def test_a_voucher_naming_no_forfeit_is_one_fine(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        capture = await _capture(8)

        (voucher,) = recap_vouchers(capture, 8, VoucherRule(type="player-of-the-month"))

        assert voucher["detail"] == "Alice may hand out one fine to anyone they choose"
        assert voucher["penalty"] is None

    async def test_no_voucher_where_nobody_held_him_or_the_month_is_withheld(self):
        store = _store()
        for gameweek in (3, 4):
            _record(store, gameweek, ALICE)
        _record(store, 5, None, bob=True)

        assert recap_vouchers(await _capture(8), 8, VOUCHER) == []

    async def test_a_month_announced_earlier_gives_no_voucher_now(self):
        store = _store()
        for gameweek in (3, 4, 5):
            _record(store, gameweek, ALICE)
        await _capture(8)

        assert recap_vouchers(await _capture(9), 9, VOUCHER) == []

    async def test_the_json_carries_every_known_month_with_its_code_and_no_id(self):
        store = _store()
        _record(store, 3, BOB)
        _record(store, 4, BOB)
        _record(store, 5, ALICE)

        payload = serialize_player_of_the_month(await _capture(8), 8)

        (september,) = payload["months"]
        assert september["player"] == {"code": GROSS, "name": "Groß"}
        assert "id" not in september["player"]
        assert september["owner"] == {"manager_key": ALICE[0], "manager_name": "Alice"}
        assert (september["voucher"], september["announced"], september["announced_gameweek"]) == (True, True, 8)
        assert [h["manager_name"] for h in september["holders"]] == ["Bob", "Bob", "Alice"]
        assert september["text"].endswith("so the voucher goes to Alice")
        assert payload["unchecked"] == []
        assert payload["records_saved"] == [str(award_record_path(SEASON, LEAGUE, "2026-09"))]


async def test_a_month_fpl_names_oddly_is_not_checked_without_override_advice():
    odd = [month.model_copy(update={"name": "Sept."}) if month.name == "September" else month for month in MONTHS]

    capture = await _capture(8, months=odd, feed=_feed())

    assert capture.fold.unchecked == ["FPL's month 'Sept.' is not a calendar month the award could name"]
    assert "overrides" not in capture.warnings[0]["message"]
