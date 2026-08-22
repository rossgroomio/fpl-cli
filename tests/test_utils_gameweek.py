"""Tests for fpl_cli.utils.gameweek."""

import pytest

from fpl_cli.utils.gameweek import is_opening_gameweek


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
