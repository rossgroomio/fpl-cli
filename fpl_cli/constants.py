"""Shared constants used across agents and modules."""

# The canonical repository URL, the one `pyproject.toml` publishes as
# `Repository`; `tests/test_league_recap.py` holds the two together.
REPOSITORY_URL = "https://github.com/rossgroomio/fpl-cli"

MIN_MINUTES_FOR_PER90 = 200  # ~2 full matches; below this, per-90 stats are unreliable

# Most managers, moves or picks one line of the recap names before it counts
# the rest instead: the awards' tie details, the Standout Players lines and a
# month's withheld reason (`league_history_months`) alike, so no two of them
# name a different number side by side. A wide tie in a fifty-manager classic
# league, or a template player there, would otherwise read as a roll call of
# the league.
DETAIL_CAP = 3
