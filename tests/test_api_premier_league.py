"""Tests for the Premier League's awards feed client (issue #492).

The parser is held to responses recorded from the live feed on 2026-10-28
(`tests/fixtures/premier_league_awards/`), so a test passes the way the live
feed would, plus the drifted shapes it must refuse rather than skip.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from fpl_cli.api.premier_league import (
    AWARD_REQUIRED_KEYS,
    BASE_URL,
    MONTHLY_PLAYER_AWARDS,
    PLAYER_OF_THE_MONTH,
    AwardsFeedError,
    PremierLeagueClient,
    award_month_key,
    parse_monthly_awards,
)

FIXTURES = Path(__file__).parent / "fixtures" / "premier_league_awards"


def _recorded(season: int) -> dict:
    return json.loads((FIXTURES / f"season-{season}.json").read_text(encoding="utf-8"))


class TestRecordedResponses:
    def test_this_seasons_players_of_the_month_are_read_with_their_fpl_codes(self):
        """João Pedro's FPL `code` is 475168 and Groß's 60307: the feed's id
        is FPL's code, checked against the live bootstrap when recorded."""
        potm = [a for a in parse_monthly_awards(_recorded(2026)) if a.type == PLAYER_OF_THE_MONTH]

        assert [(a.month_key, a.code, a.name) for a in potm] == [
            ("2026-08", 475168, "João Pedro"),
            ("2026-09", 60307, "Pascal Groß"),
        ]

    def test_the_monthly_awards_are_kept_and_the_season_awards_left_out(self):
        awards = parse_monthly_awards(_recorded(2025))

        assert {a.type for a in awards} == MONTHLY_PLAYER_AWARDS
        assert len([a for a in awards if a.type == PLAYER_OF_THE_MONTH]) == 9
        # August 2025 to April 2026, and no May: the season's awards are made instead.
        assert [a.month_key for a in awards if a.type == PLAYER_OF_THE_MONTH] == [
            "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
            "2026-01", "2026-02", "2026-03", "2026-04",
        ]

    def test_each_award_keeps_the_entry_as_served(self):
        groß = next(a for a in parse_monthly_awards(_recorded(2026)) if a.code == 60307)

        assert groß.entry["currentTeam"]["shortName"] == "Brighton"
        assert groß.entry["id"] == "60307"

    def test_the_contract_is_what_the_live_entries_carry(self):
        """The keys the parser reads are on every recorded player award."""
        for season in (2025, 2026):
            for entry in _recorded(season)["player_awards"]:
                assert AWARD_REQUIRED_KEYS <= set(entry)


class TestDrift:
    @pytest.mark.parametrize("payload", [None, [], {"awards": []}, {"player_awards": {}}])
    def test_a_response_without_the_award_list_is_refused(self, payload):
        with pytest.raises(AwardsFeedError, match="no `player_awards` list"):
            parse_monthly_awards(payload)

    def test_an_entry_saying_nothing_of_what_it_is_is_refused(self):
        with pytest.raises(AwardsFeedError, match="entry 2 carries no award `type`"):
            parse_monthly_awards({"player_awards": [
                {"type": "POTS", "date": "2026-5"}, {"date": "2026-9", "id": "60307"},
            ]})

    @pytest.mark.parametrize(("entry", "match"), [
        ({"type": "POTM", "date": "2026-9"}, "is missing id"),
        ({"type": "POTM", "id": "60307"}, "is missing date"),
        ({"type": "POTM", "date": "2026-09-01", "id": "60307"}, "naming no month"),
        ({"type": "POTM", "date": "2026-13", "id": "60307"}, "naming no month"),
        ({"type": "SOTM", "date": "2026-9", "id": "p154561"}, "no player id"),
        ({"type": "POTM", "date": "2026-9", "id": True}, "no player id"),
        ({"type": "GOTM", "date": "2026-9", "id": "0"}, "no player id"),
        ("POTM", "is not an object"),
    ], ids=["no-id", "no-date", "a-day", "month-13", "opta-prefix", "bool-id", "zero-id", "not-an-object"])
    def test_a_monthly_entry_the_parser_cannot_read_is_refused_not_skipped(self, entry, match):
        """A skipped award would read as a month with none."""
        with pytest.raises(AwardsFeedError, match=match):
            parse_monthly_awards({"player_awards": [entry]})

    def test_a_season_award_is_never_read_so_its_shape_is_its_own(self):
        assert parse_monthly_awards({"player_awards": [{"type": "GB", "date": "2026-5"}]}) == []

    def test_an_int_id_and_a_missing_name_still_read(self):
        (award,) = parse_monthly_awards({"player_awards": [{"type": "POTM", "date": "2026-10", "id": 60307}]})

        assert (award.code, award.name, award.month_key) == (60307, None, "2026-10")

    def test_a_name_without_a_display_falls_back_to_first_and_last(self):
        (award,) = parse_monthly_awards({"player_awards": [
            {"type": "POTM", "date": "2026-10", "id": "1", "name": {"first": "Pascal", "last": "Groß"}},
        ]})

        assert award.name == "Pascal Groß"


def test_a_month_key_is_zero_padded():
    assert award_month_key(2026, 9) == "2026-09"
    assert award_month_key(2027, 11) == "2027-11"


class TestClient:
    @respx.mock
    async def test_the_season_is_asked_for_by_its_start_year(self):
        route = respx.get(f"{BASE_URL}/competitions/8/seasons/2026/awards").mock(
            return_value=Response(200, json=_recorded(2026)),
        )

        async with PremierLeagueClient() as client:
            awards = await client.get_monthly_awards("2026-27")

        assert route.called
        assert "origin" not in route.calls.last.request.headers
        assert len([a for a in awards if a.type == PLAYER_OF_THE_MONTH]) == 2

    @respx.mock
    async def test_a_refusal_raises_for_the_caller_to_report(self):
        respx.get(f"{BASE_URL}/competitions/8/seasons/2026/awards").mock(return_value=Response(503))

        async with PremierLeagueClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_monthly_awards("2026-27")

    @respx.mock
    async def test_a_page_that_is_not_json_raises_a_value_error(self):
        respx.get(f"{BASE_URL}/competitions/8/seasons/2026/awards").mock(
            return_value=Response(200, text="<html>moved</html>"),
        )

        async with PremierLeagueClient() as client:
            with pytest.raises(ValueError):
                await client.get_monthly_awards("2026-27")

    async def test_a_season_that_is_not_a_label_is_refused_before_any_request(self):
        async with PremierLeagueClient() as client:
            with pytest.raises(ValueError, match="not a season label"):
                await client.get_awards("2026")
