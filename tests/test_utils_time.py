"""Tests for UK-local timestamp formatting helpers."""

from datetime import datetime, timezone

from fpl_cli.utils.time import (
    UK_TZ,
    format_deadline,
    format_generated_at,
    format_kickoff,
    now_uk,
)


class TestFormatDeadline:
    def test_summer_timestamp_displays_as_bst(self):
        # 17:30 UTC in April → 18:30 BST
        assert format_deadline("2026-04-18T17:30:00Z") == "Sat 18 Apr, 18:30 BST"

    def test_winter_timestamp_displays_as_gmt(self):
        # 18:30 UTC in January → 18:30 GMT
        assert format_deadline("2026-01-03T18:30:00Z") == "Sat 03 Jan, 18:30 GMT"

    def test_dst_transition_day_resolves_without_error(self):
        # Spring-forward Sunday 2026-03-29: 01:00 UK local jumps to 02:00. A UTC timestamp
        # after the transition should resolve cleanly to a BST string.
        result = format_deadline("2026-03-29T02:30:00Z")
        assert "BST" in result
        assert "29 Mar" in result

    def test_empty_string_returns_unchanged(self):
        assert format_deadline("") == ""

    def test_malformed_input_returns_unchanged(self):
        assert format_deadline("not-a-date") == "not-a-date"

    def test_accepts_explicit_offset_suffix(self):
        # FPL API uses 'Z' but fromisoformat works with '+00:00' too
        assert format_deadline("2026-04-18T17:30:00+00:00") == "Sat 18 Apr, 18:30 BST"


class TestFormatKickoff:
    def test_summer_kickoff_displays_as_bst(self):
        # 14:00 UTC Sunday in April → 15:00 BST
        assert format_kickoff("2026-04-19T14:00:00Z") == "Sun 15:00 BST"

    def test_winter_kickoff_displays_as_gmt(self):
        assert format_kickoff("2026-01-03T15:00:00Z") == "Sat 15:00 GMT"

    def test_empty_string_returns_unchanged(self):
        assert format_kickoff("") == ""

    def test_malformed_input_returns_unchanged(self):
        assert format_kickoff("TBC") == "TBC"


class TestNowUk:
    def test_returns_timezone_aware_datetime_in_uk(self):
        now = now_uk()
        assert now.tzinfo is not None
        assert now.tzinfo.key == "Europe/London"  # type: ignore[attr-defined]


class TestFormatGeneratedAt:
    def test_default_uses_current_uk_time(self):
        result = format_generated_at()
        assert result.endswith("BST") or result.endswith("GMT")

    def test_aware_utc_input_converts_to_uk(self):
        dt = datetime(2026, 4, 18, 13, 32, tzinfo=timezone.utc)
        assert format_generated_at(dt) == "2026-04-18 14:32 BST"

    def test_aware_winter_utc_input(self):
        dt = datetime(2026, 1, 3, 14, 32, tzinfo=timezone.utc)
        assert format_generated_at(dt) == "2026-01-03 14:32 GMT"

    def test_naive_input_assumed_uk_local(self):
        dt = datetime(2026, 4, 18, 14, 32)
        # Treated as already-UK, so just formatted with tz label appended
        result = format_generated_at(dt)
        assert result.startswith("2026-04-18 14:32")
        assert result.endswith("BST") or result.endswith("GMT")


class TestUkTz:
    def test_is_europe_london(self):
        assert UK_TZ.key == "Europe/London"
