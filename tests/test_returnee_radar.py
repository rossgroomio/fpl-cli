"""Tests for the injury returnee radar's news signal parser."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from fpl_cli.models.player import PlayerStatus
from fpl_cli.season import get_season_year
from fpl_cli.services.returnee_radar import (
    SOURCE_FPL_NEWS,
    ReturnSignal,
    build_return_signal,
    gameweek_for_date,
    news_age_days,
    resolve_return_date,
)
from tests.conftest import make_player

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A 2026-27 shaped schedule with the live three-week GW5 -> GW6 break, so a
# date in the gap can only land on GW6 by walking deadlines: a fixed
# weeks-per-gameweek assumption anchored on GW1 would put 27 Sep in GW7.
GAMEWEEKS: list[dict[str, Any]] = [
    {"id": 1, "deadline_time": "2026-08-14T17:30:00Z"},
    {"id": 2, "deadline_time": "2026-08-22T10:00:00Z"},
    {"id": 3, "deadline_time": "2026-08-29T10:00:00Z"},
    {"id": 4, "deadline_time": "2026-09-12T10:00:00Z"},
    {"id": 5, "deadline_time": "2026-09-19T10:00:00Z"},
    {"id": 6, "deadline_time": "2026-10-10T10:00:00Z"},
    {"id": 7, "deadline_time": "2026-10-17T10:00:00Z"},
]

SEASON_YEAR = 2026

# Before GW1's deadline: nothing has lapsed yet.
PRESEASON_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
# After GW5's deadline: anything due before 19 Sep has lapsed.
POST_GW5_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _signal(
    news: str = "",
    *,
    news_added: str | None = None,
    chance: int | None = None,
    status: PlayerStatus = PlayerStatus.INJURED,
    gameweeks: list[dict[str, Any]] | None = None,
    now: datetime | None = PRESEASON_NOW,
    season_year: int | None = SEASON_YEAR,
) -> ReturnSignal:
    player = make_player(
        status=status,
        news=news,
        news_added=news_added,
        chance_of_playing_next_round=chance,
    )
    return build_return_signal(
        player,
        gameweeks=GAMEWEEKS if gameweeks is None else gameweeks,
        now=now,
        season_year=season_year,
    )


# ---------------------------------------------------------------------------
# Parsing the measured grammar
# ---------------------------------------------------------------------------


def test_expected_back_shape_parses_date_and_source():
    signal = _signal("Calf injury - Expected back 5 Sep")

    assert signal.return_date == date(2026, 9, 5)
    assert signal.source == SOURCE_FPL_NEWS
    assert signal.has_return_date is True
    assert signal.lapsed is False


def test_suspended_until_shape_parses_date():
    signal = _signal("Suspended until 19 Sep", status=PlayerStatus.SUSPENDED)

    assert signal.return_date == date(2026, 9, 19)
    assert signal.source == SOURCE_FPL_NEWS
    assert signal.has_return_date is True


def test_unknown_return_date_shape_yields_date_unknown_and_keeps_news():
    text = "Knee injury - Unknown return date"
    signal = _signal(text)

    assert signal.return_date is None
    assert signal.return_gameweek is None
    assert signal.source is None
    assert signal.has_return_date is False
    assert signal.news == text


def test_chance_of_playing_shape_does_not_read_percentage_as_a_day():
    signal = _signal("Thigh injury - 75% chance of playing", chance=75)

    assert signal.return_date is None
    assert signal.source is None
    assert signal.chance_of_playing == 75


def test_empty_news_yields_date_unknown_without_raising():
    signal = _signal("")

    assert signal.return_date is None
    assert signal.has_return_date is False
    assert signal.news == ""


def test_unrecognised_shape_yields_date_unknown():
    signal = _signal("Has joined Getafe permanently")

    assert signal.return_date is None
    assert signal.source is None
    assert signal.news == "Has joined Getafe permanently"


def test_impossible_calendar_date_yields_date_unknown():
    signal = _signal("Ankle injury - Expected back 31 Feb")

    assert signal.return_date is None
    assert signal.source is None


def test_unknown_month_token_yields_date_unknown():
    signal = _signal("Ankle injury - Expected back 5 Smarch")

    assert signal.return_date is None


def test_player_shaped_mapping_is_accepted():
    signal = build_return_signal(
        {"news": "Calf injury - Expected back 5 Sep", "chance_of_playing_next_round": 25},
        gameweeks=GAMEWEEKS,
        now=PRESEASON_NOW,
        season_year=SEASON_YEAR,
    )

    assert signal.return_date == date(2026, 9, 5)
    assert signal.chance_of_playing == 25


# ---------------------------------------------------------------------------
# Season-aware date resolution
# ---------------------------------------------------------------------------


def test_month_after_july_cutover_resolves_to_season_start_year():
    assert resolve_return_date(5, 9, SEASON_YEAR) == date(2026, 9, 5)


def test_month_before_july_cutover_resolves_to_following_calendar_year():
    assert resolve_return_date(14, 2, SEASON_YEAR) == date(2027, 2, 14)


def test_february_date_in_august_start_season_lands_next_year():
    signal = _signal("Cruciate ligament injury - Expected back 14 Feb")

    assert signal.return_date == date(2027, 2, 14)


def test_season_year_defaults_to_the_current_season():
    signal = _signal("Calf injury - Expected back 5 Sep", season_year=None, gameweeks=[])

    assert signal.return_date == date(get_season_year(), 9, 5)


# ---------------------------------------------------------------------------
# Gameweek mapping
# ---------------------------------------------------------------------------


def test_date_between_deadlines_maps_to_the_later_gameweek():
    assert gameweek_for_date(date(2026, 9, 5), GAMEWEEKS) == 4


def test_date_on_a_deadline_maps_to_that_gameweek():
    assert gameweek_for_date(date(2026, 9, 19), GAMEWEEKS) == 5


def test_date_in_the_three_week_gap_maps_to_gameweek_six():
    assert gameweek_for_date(date(2026, 9, 27), GAMEWEEKS) == 6


def test_date_after_the_final_deadline_maps_to_no_gameweek():
    assert gameweek_for_date(date(2026, 11, 1), GAMEWEEKS) is None


def test_signal_carries_the_mapped_gameweek():
    signal = _signal("Calf injury - Expected back 27 Sep")

    assert signal.return_gameweek == 6


def test_gameweek_mapping_ignores_events_without_a_usable_deadline():
    events: list[dict[str, Any]] = [
        {"id": 1, "deadline_time": None},
        {"id": 2, "deadline_time": "not-a-timestamp"},
        {"id": 3, "deadline_time": "2026-08-29T10:00:00Z"},
    ]

    assert gameweek_for_date(date(2026, 8, 20), events) == 3


def test_no_gameweeks_yields_no_gameweek_but_still_a_date():
    signal = _signal("Calf injury - Expected back 5 Sep", gameweeks=[])

    assert signal.return_date == date(2026, 9, 5)
    assert signal.return_gameweek is None


# ---------------------------------------------------------------------------
# Lapsed returns
# ---------------------------------------------------------------------------


def test_date_before_the_current_deadline_lapses_to_date_unknown():
    signal = _signal("Calf injury - Expected back 5 Sep", now=POST_GW5_NOW)

    assert signal.lapsed is True
    assert signal.has_return_date is False
    assert signal.return_gameweek is None
    # The original date survives for display and week-over-week diffing.
    assert signal.return_date == date(2026, 9, 5)
    assert signal.source == SOURCE_FPL_NEWS


def test_date_after_the_current_deadline_does_not_lapse():
    signal = _signal("Calf injury - Expected back 10 Oct", now=POST_GW5_NOW)

    assert signal.lapsed is False
    assert signal.has_return_date is True
    assert signal.return_gameweek == 6


def test_nothing_lapses_before_the_first_deadline():
    signal = _signal("Calf injury - Expected back 20 Aug", now=PRESEASON_NOW)

    assert signal.lapsed is False
    assert signal.has_return_date is True


def test_date_unknown_signal_is_never_marked_lapsed():
    signal = _signal("Knee injury - Unknown return date", now=POST_GW5_NOW)

    assert signal.lapsed is False
    assert signal.has_return_date is False


# ---------------------------------------------------------------------------
# News age
# ---------------------------------------------------------------------------


def test_news_age_is_computed_from_news_added():
    assert news_age_days("2026-09-15T09:30:00Z", now=POST_GW5_NOW) == 5


def test_news_age_is_none_when_the_field_is_absent():
    assert news_age_days(None, now=POST_GW5_NOW) is None
    assert news_age_days("", now=POST_GW5_NOW) is None


def test_news_age_is_none_when_the_stamp_is_unparseable():
    assert news_age_days("last tuesday", now=POST_GW5_NOW) is None


def test_news_age_of_a_future_stamp_clamps_to_zero():
    assert news_age_days("2026-09-25T09:30:00Z", now=POST_GW5_NOW) == 0


def test_naive_news_stamp_is_read_as_utc():
    assert news_age_days("2026-09-15T09:30:00", now=POST_GW5_NOW) == 5


def test_signal_carries_news_age():
    signal = _signal(
        "Knee injury - Unknown return date",
        news_added="2026-09-15T09:30:00Z",
        now=POST_GW5_NOW,
    )

    assert signal.news_age_days == 5


def test_signal_news_age_is_none_without_a_stamp():
    signal = _signal("Knee injury - Unknown return date", now=POST_GW5_NOW)

    assert signal.news_age_days is None


def test_default_now_does_not_raise():
    player = make_player(status=PlayerStatus.INJURED, news="Knee injury - Unknown return date")

    signal = build_return_signal(player, gameweeks=GAMEWEEKS)

    assert signal.has_return_date is False


# ---------------------------------------------------------------------------
# Whole-grammar sweep (U1 definition of done)
# ---------------------------------------------------------------------------

# Every distinct shape measured on a live bootstrap-static snapshot, with the
# date each one should yield. `None` means date-unknown, which the measurement
# says is the common case rather than the error case.
GRAMMAR_CASES: list[tuple[str, date | None]] = [
    ("Calf injury - Expected back 5 Sep", date(2026, 9, 5)),
    ("Hamstring injury - Expected back 12 Sep", date(2026, 9, 12)),
    ("Knock - Expected back 19 Sep", date(2026, 9, 19)),
    ("Ankle injury - Expected back 10 Oct", date(2026, 10, 10)),
    ("Cruciate ligament injury - Expected back 14 Feb", date(2027, 2, 14)),
    ("Suspended until 19 Sep", date(2026, 9, 19)),
    ("Suspended until 10 Oct", date(2026, 10, 10)),
    ("Thigh injury - 75% chance of playing", None),
    ("Groin injury - 25% chance of playing", None),
    ("Illness - 50% chance of playing", None),
    ("Knee injury - Unknown return date", None),
    ("Foot injury - Unknown return date", None),
    ("Loan - Unknown return date", None),
    ("Has joined Getafe permanently", None),
    ("", None),
]


@pytest.mark.parametrize(("news", "expected"), GRAMMAR_CASES)
def test_measured_grammar_produces_the_expected_date_split(news: str, expected: date | None):
    signal = _signal(news)

    assert signal.return_date == expected
    assert signal.news == news
    assert (signal.source == SOURCE_FPL_NEWS) is (expected is not None)


def test_return_signal_is_frozen():
    signal = _signal("Knee injury - Unknown return date")

    with pytest.raises(AttributeError):
        signal.return_date = date(2026, 9, 5)  # type: ignore[misc]
