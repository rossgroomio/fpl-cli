"""Tests for fpl_cli.utils.gameweek."""

import pytest

from fpl_cli.utils.gameweek import format_gameweek_list, is_opening_gameweek


@pytest.mark.parametrize(
    ("gameweek", "expected"),
    [
        (1, True),
        (2, False),
        (38, False),
        (None, False),
    ],
)
def test_is_opening_gameweek(gameweek, expected):
    assert is_opening_gameweek(gameweek) is expected


class TestFormatGameweekList:
    """Shared by the recap's coverage report and the season fines tally, so
    both name a gameweek set the same way."""

    def test_contiguous_runs_collapse_and_gaps_split(self):
        assert format_gameweek_list([1, 2, 3, 7, 9, 10]) == "GW1-3, GW7, GW9-10"

    def test_a_single_gameweek_renders_alone(self):
        assert format_gameweek_list([4]) == "GW4"

    def test_an_empty_list_renders_as_nothing(self):
        assert format_gameweek_list([]) == ""

    def test_unsorted_input_is_ordered_first(self):
        assert format_gameweek_list([3, 1, 2]) == "GW1-3"

    def test_duplicates_collapse_rather_than_repeating(self):
        assert format_gameweek_list([2, 2, 3]) == "GW2-3"
