"""Tests for fpl_cli/season — season detection and format helpers."""

from datetime import date

from fpl_cli.season import (
    CHIP_SPLIT_GW,
    TOTAL_GAMEWEEKS,
    core_insights_season,
    get_season_year,
    season_label,
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
