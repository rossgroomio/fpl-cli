"""Tests for fpl_cli/season — season detection and format helpers."""

from datetime import date
from pathlib import Path

import pytest

from fpl_cli.season import (
    CHIP_SPLIT_GW,
    TOTAL_GAMEWEEKS,
    core_insights_season,
    get_season_year,
    is_season_label,
    season_label,
    season_partition,
    season_start_year,
    season_year_from_gameweeks,
    understat_season,
    vaastav_season,
    vaastav_season_range,
)

# -- Constants ---------------------------------------------------------------

def test_total_gameweeks_is_38():
    assert TOTAL_GAMEWEEKS == 38


def test_chip_split_is_half():
    assert CHIP_SPLIT_GW == TOTAL_GAMEWEEKS // 2


# -- get_season_year ---------------------------------------------------------

class TestGetSeasonYear:
    """July cutover: month >= 7 -> current year, else previous year."""

    def test_january_resolves_to_previous_year(self):
        assert get_season_year(date(2026, 1, 15)) == 2025

    def test_june_30_resolves_to_previous_year(self):
        assert get_season_year(date(2026, 6, 30)) == 2025

    def test_july_1_resolves_to_current_year(self):
        assert get_season_year(date(2026, 7, 1)) == 2026

    def test_august_resolves_to_current_year(self):
        assert get_season_year(date(2026, 8, 15)) == 2026

    def test_december_resolves_to_current_year(self):
        assert get_season_year(date(2026, 12, 31)) == 2026

    def test_defaults_to_today(self):
        # Smoke test: should return an int without error.
        result = get_season_year()
        assert isinstance(result, int)


# -- season_year_from_gameweeks -----------------------------------------------

class TestSeasonYearFromGameweeks:
    """#91: the year comes from GW1's deadline, not the clock, so a season
    that overruns the July cutover (2019-20, delayed into July 2020 by
    COVID) still resolves to the year it started."""

    def test_reads_gw1s_deadline_year(self):
        gameweeks = [{"id": 1, "deadline_time": "2019-08-09T18:00:00Z"}]
        assert season_year_from_gameweeks(gameweeks) == 2019

    def test_a_season_overrunning_into_july_still_resolves_to_its_start_year(self):
        """The regression itself: 2019-20 finished in July 2020, but its
        GW1 deadline still says 2019 -- unlike `get_season_year()`, which a
        July-or-later clock would resolve to 2020."""
        gameweeks = [
            {"id": 1, "deadline_time": "2019-08-09T18:00:00Z"},
            {"id": 38, "deadline_time": "2020-07-26T15:00:00Z"},
        ]
        assert season_year_from_gameweeks(gameweeks) == 2019

    def test_ignores_other_gameweeks_deadlines(self):
        gameweeks = [
            {"id": 1, "deadline_time": "2026-08-15T11:00:00Z"},
            {"id": 2, "deadline_time": "2026-08-22T11:00:00Z"},
        ]
        assert season_year_from_gameweeks(gameweeks) == 2026

    def test_missing_gw1_returns_none(self):
        gameweeks = [{"id": 2, "deadline_time": "2026-08-22T11:00:00Z"}]
        assert season_year_from_gameweeks(gameweeks) is None

    def test_empty_payload_returns_none(self):
        assert season_year_from_gameweeks([]) is None

    def test_gw1_with_no_deadline_returns_none(self):
        """Pre-season, before fixtures are released: GW1 exists but carries
        no deadline yet."""
        assert season_year_from_gameweeks([{"id": 1, "deadline_time": None}]) is None

    def test_gw1_with_unparseable_deadline_returns_none(self):
        assert season_year_from_gameweeks([{"id": 1, "deadline_time": "not-a-date"}]) is None

    def test_a_plain_date_without_time_still_parses(self):
        assert season_year_from_gameweeks([{"id": 1, "deadline_time": "2026-08-14"}]) == 2026


# -- understat_season --------------------------------------------------------

class TestUnderstatSeason:
    def test_explicit_year(self):
        assert understat_season(2025) == "2025"

    def test_defaults_to_current(self):
        assert isinstance(understat_season(), str)


# -- core_insights_season ----------------------------------------------------

class TestCoreInsightsSeason:
    def test_explicit_year(self):
        assert core_insights_season(2025) == "2025-2026"

    def test_century_boundary(self):
        assert core_insights_season(2099) == "2099-2100"

    def test_defaults_to_current(self):
        assert core_insights_season() == core_insights_season(get_season_year())


# -- vaastav_season ----------------------------------------------------------

class TestVaastavSeason:
    def test_standard_year(self):
        assert vaastav_season(2025) == "2025-26"

    def test_century_boundary(self):
        assert vaastav_season(2099) == "2099-00"

    def test_defaults_to_current(self):
        result = vaastav_season()
        assert "-" in result


# -- vaastav_season_range ----------------------------------------------------

class TestVaastavSeasonRange:
    def test_four_season_window(self):
        assert vaastav_season_range(2025, count=4) == (
            "2022-23",
            "2023-24",
            "2024-25",
            "2025-26",
        )

    def test_single_season(self):
        assert vaastav_season_range(2025, count=1) == ("2025-26",)

    def test_ordering_is_chronological(self):
        seasons = vaastav_season_range(2025, count=3)
        assert seasons == ("2023-24", "2024-25", "2025-26")

    def test_defaults_to_current(self):
        result = vaastav_season_range()
        assert len(result) == 4
        assert all("-" in s for s in result)


# -- Rollover ----------------------------------------------------------------

class TestSeasonRollover:
    """Every source format crosses the July cutover together.

    Frozen dates rather than the clock: fixtures that pin a season instead of
    deriving it hold only until the next 1 July, which is how 47 tests broke
    silently on rollover.
    """

    def test_formats_agree_before_cutover(self):
        year = get_season_year(date(2027, 6, 30))
        assert (understat_season(year), season_label(year), core_insights_season(year)) == (
            "2026",
            "2026-27",
            "2026-2027",
        )

    def test_formats_agree_after_cutover(self):
        year = get_season_year(date(2027, 7, 1))
        assert (understat_season(year), season_label(year), core_insights_season(year)) == (
            "2027",
            "2027-28",
            "2027-2028",
        )

    def test_defaults_track_current_season_year(self):
        """No-argument calls resolve to the same season as get_season_year()."""
        year = get_season_year()
        assert understat_season() == understat_season(year)
        assert season_label() == season_label(year)
        assert core_insights_season() == core_insights_season(year)


# -- season_partition --------------------------------------------------------

class TestSeasonPartition:
    """Report filenames carry a gameweek but no season (#85), so the season
    lives in the directory. These tests pin the partition that makes a
    cross-season collision structurally impossible."""

    def test_appends_the_season_label(self):
        assert season_partition(Path("01_Reports"), season="2026-27") == Path("01_Reports/2026-27")

    def test_defaults_to_the_current_season(self):
        assert season_partition(Path("01_Reports")) == Path("01_Reports") / season_label()

    def test_is_idempotent_on_an_already_partitioned_base(self):
        """A user who points reports.output_dir at a season directory by hand,
        or a caller that feeds a resolved path back in, must not get
        `2026-27/2026-27`."""
        once = season_partition(Path("01_Reports"), season="2026-27")
        assert season_partition(once, season="2026-27") == once

    def test_a_season_named_segment_mid_path_is_not_treated_as_the_tail(self):
        """Only the final segment counts -- an archive tree that happens to
        contain the label deeper up still gets its own partition."""
        assert season_partition(
            Path("archive/2026-27/reports"), season="2026-27",
        ) == Path("archive/2026-27/reports/2026-27")

    def test_two_seasons_never_share_a_directory(self):
        """The regression the issue is about: same gameweek, different season,
        different file."""
        a = season_partition(Path("01_Reports"), season="2025-26") / "gw21-review.md"
        b = season_partition(Path("01_Reports"), season="2026-27") / "gw21-review.md"
        assert a != b

    def test_preserves_an_absolute_base(self):
        assert season_partition(
            Path("/vault/01_Reports"), season="2026-27",
        ) == Path("/vault/01_Reports/2026-27")


# -- is_season_label ---------------------------------------------------------

class TestIsSeasonLabel:
    """Exact, not shape-matching: used to tell a directory left over from last
    season apart from an ordinary one, so a false positive would suppress the
    stale-directory warning."""

    def test_accepts_a_real_label(self):
        assert is_season_label("2026-27")

    def test_accepts_a_century_rollover_label(self):
        assert is_season_label(season_label(2099))

    def test_rejects_a_mismatched_second_year(self):
        assert not is_season_label("2026-28")

    def test_rejects_an_unpadded_second_year(self):
        assert not is_season_label("2026-7")

    def test_rejects_a_plain_directory_name(self):
        assert not is_season_label("reports")

    def test_rejects_a_name_that_merely_contains_a_year(self):
        assert not is_season_label("reports-2026")

    def test_rejects_a_bare_year(self):
        assert not is_season_label("2026")

    def test_every_generated_label_round_trips(self):
        assert all(is_season_label(season_label(y)) for y in range(1995, 2100))


# -- season_start_year -------------------------------------------------------

class TestSeasonStartYear:
    def test_parses_the_leading_year(self):
        assert season_start_year("2025-26") == 2025

    def test_round_trips_season_label(self):
        assert all(season_start_year(season_label(y)) == y for y in range(1995, 2100))

    def test_rejects_anything_that_is_not_a_season_label(self):
        for bad in ("2025-2026", "2026-28", "reports", "2025", ""):
            with pytest.raises(ValueError):
                season_start_year(bad)
