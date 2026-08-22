"""Tests for fpl_cli/utils/markdown.py.

The shared heading matcher is the single point where LLM-authored heading
drift is either tolerated or rejected, so the must-match and must-not-match
sets are pinned here rather than only through its consumers.
"""

from __future__ import annotations

import pytest

from fpl_cli.utils.markdown import (
    HeadingMatcher,
    fence_flags,
    find_section,
    has_heading,
    leaf_body,
    parse_heading,
    section_body,
)

# -- Drift shapes the matcher must tolerate -----------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "#### Starting XI",
        "#### Starting XI (3-4-3)",
        "#### Starting XI (Formation: 3-4-3)",
        "#### Starting XI [provisional]",
        "#### Starting XI: 3-4-3",
        "#### Starting XI — 3-4-3",
        "#### Starting XI 3-4-3",
        "#### starting xi",
        "#### STARTING XI (3-4-3)",
        "#### (3-4-3) Starting XI",
        "#### 3-4-3 Starting XI",
        "#### **Starting XI**",
        "####   Starting   XI  ",
        "  #### Starting XI",
    ],
)
def test_matches_tolerated_drift(line):
    assert HeadingMatcher("#### Starting XI").matches(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "#### Starting XI Reserve",  # an extra word is a different heading
        "#### Provisional Starting XI",
        "### Starting XI",  # wrong depth
        "##### Starting XI",
        "#### Starting",
        "####Starting XI",  # no space: not an ATX heading
        "Starting XI",
        "| Starting XI |",
        "",
    ],
)
def test_does_not_match_different_heading(line):
    assert HeadingMatcher("#### Starting XI").matches(line) is False


@pytest.mark.parametrize(
    "line",
    [
        "#### Bench Order",
        "#### Bench-Warmers",
        "#### Bench.Deprecated",
        "#### Benched",
    ],
)
def test_prefix_sharing_headings_stay_distinct(line):
    """A glued or extra-word continuation is a different heading, not a
    qualified '#### Bench' — the collision class behind issue #63."""
    assert HeadingMatcher("#### Bench").matches(line) is False


# -- Aliases ------------------------------------------------------------------


def test_aliases_match_at_the_same_depth():
    matcher = HeadingMatcher("#### Starting XI", aliases=("XI", "Starting Eleven"))
    assert matcher.matches("#### XI") is True
    assert matcher.matches("#### Starting Eleven (3-4-3)") is True
    assert matcher.matches("### XI") is False


@pytest.mark.parametrize(
    "line",
    [
        "#### Starting 11",
        "#### Starting 11 (3-4-3)",
        "#### Starting 11 (revised)",
        "#### 3-4-3 Starting 11",
    ],
)
def test_digit_suffixed_alias_still_matches_with_annotations(line):
    """A trailing digit that's part of the alias itself (not an annotation)
    must survive further drift layered on top of it."""
    matcher = HeadingMatcher(
        "#### Starting XI", aliases=("XI", "Starting Eleven", "Starting 11")
    )
    assert matcher.matches(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "#### Starting XI-3-4-3",
        "#### Starting XI:3-4-3",
    ],
)
def test_glued_punctuation_then_digit_qualifier_strips_fully(line):
    """A glued qualifier starting with punctuation immediately followed by a
    digit must strip the punctuation along with the digits, not just the
    digits -- otherwise the punctuation is left stranded on the core text."""
    assert HeadingMatcher("#### Starting XI").matches(line) is True


def test_alias_does_not_widen_to_prefix_matches():
    matcher = HeadingMatcher("## Draft League", aliases=("Draft",))
    assert matcher.matches("## Draft") is True
    assert matcher.matches("## Draft League (Provisional)") is True
    assert matcher.matches("## Draft Rankings") is False


def test_rejects_non_heading_and_empty_heading():
    with pytest.raises(ValueError):
        HeadingMatcher("Starting XI")
    with pytest.raises(ValueError):
        HeadingMatcher("####")


def test_parse_heading_returns_depth_and_text():
    assert parse_heading("### Waiver Recommendations") == (3, "Waiver Recommendations")
    assert parse_heading("not a heading") is None


# -- Fence awareness ----------------------------------------------------------


def test_fenced_heading_is_not_a_heading():
    lines = [
        "## Notes",
        "```markdown",
        "## Classic Squad",
        "```",
        "## Classic Squad",
        "body",
    ]
    assert find_section(lines, "## Classic Squad") == (4, 6)


def test_fence_flags_mark_delimiters_and_content():
    lines = ["a", "```py", "x = 1", "```", "b"]
    assert list(fence_flags(lines)) == [False, True, True, True, False]


def test_tilde_fence_is_not_closed_by_a_backtick_fence():
    lines = ["~~~", "```", "## Classic Squad", "~~~", "## Classic Squad"]
    assert find_section(lines, "## Classic Squad") == (4, 5)


def test_shorter_fence_of_the_same_character_does_not_close_a_longer_one():
    """Per CommonMark, a fence only closes on a run at least as long as the one
    that opened it -- a nested example fenced with fewer backticks than the
    outer fence is still fenced content, not a closer."""
    lines = ["````", "```", "## Classic Squad", "````", "## Classic Squad", "body"]
    assert list(fence_flags(lines)) == [True, True, True, True, False, False]
    assert find_section(lines, "## Classic Squad") == (4, 6)


# -- Section boundaries -------------------------------------------------------


def test_section_ends_at_next_same_depth_heading():
    lines = ["## A", "one", "## B", "two"]
    assert find_section(lines, "## A") == (0, 2)


def test_section_ends_at_shallower_heading():
    lines = ["### A", "one", "# Top", "two"]
    assert find_section(lines, "### A") == (0, 2)


def test_deeper_headings_stay_inside_the_section():
    lines = ["## A", "#### deep", "one", "## B"]
    assert find_section(lines, "## A") == (0, 3)


def test_repeated_heading_extends_rather_than_truncates():
    lines = ["## A", "one", "## A (again)", "two", "## B"]
    assert find_section(lines, "## A") == (0, 4)


def test_section_runs_to_eof_when_nothing_follows():
    lines = ["## A", "one"]
    assert find_section(lines, "## A") == (0, 2)


def test_find_section_returns_none_when_absent():
    assert find_section(["## B", "x"], "## A") is None
    assert has_heading(["## B", "x"], "## A") is False
    assert section_body(["## B", "x"], "## A") is None


def test_section_body_drops_repeated_headings_only():
    lines = ["## A", "one", "## A", "two", "## B"]
    assert section_body(lines, "## A") == ["one", "two"]


def test_section_body_preserves_fenced_lookalike_lines():
    lines = ["## A", "```", "## A", "```", "## B"]
    assert section_body(lines, "## A") == ["```", "## A", "```"]


# -- leaf_body ------------------------------------------------------------


def test_leaf_body_stops_at_a_nested_heading():
    """Unlike section_body, a nested heading ends the body even though it
    doesn't end the section -- a leaf section is a table or note, not a
    container, so a nested heading marks drift, not more of its data."""
    lines = ["### A", "one", "#### nested", "two", "### B"]
    assert leaf_body(lines, "### A") == ["one"]
    assert section_body(lines, "### A") == ["one", "#### nested", "two"]


def test_leaf_body_returns_full_body_when_no_nested_heading():
    lines = ["### A", "one", "two", "### B"]
    assert leaf_body(lines, "### A") == ["one", "two"]


def test_leaf_body_returns_none_when_absent():
    assert leaf_body(["## B", "x"], "## A") is None
