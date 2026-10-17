"""FPL's post-gameweek processing feed: the model, both clients, the CLI helpers (issue #424)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pydantic
import pytest

from fpl_cli.api.fpl import FPLClient
from fpl_cli.api.fpl_draft import FPLDraftClient
from fpl_cli.cli._gameweek_status import fetch_gameweek_status, settled_flags
from fpl_cli.models.gameweek_status import GameweekStatus
from tests.conftest import make_gameweek_status

# Verbatim from both feeds on 2026-10-17, with GW6 fully processed.
CLASSIC_SETTLED = {
    "status": [
        {"bonus_added": True, "date": "2026-10-10", "event": 6, "points": "r"},
        {"bonus_added": True, "date": "2026-10-11", "event": 6, "points": "r"},
        {"bonus_added": True, "date": "2026-10-12", "event": 6, "points": "r"},
    ],
    "leagues": "Updated",
}
DRAFT_SETTLED = {
    "status": [
        {"bonus_added": True, "date": "2026-10-10", "event": 6, "leagues_updated": True, "points": "r"},
        {"bonus_added": True, "date": "2026-10-11", "event": 6, "leagues_updated": True, "points": "r"},
        {"bonus_added": True, "date": "2026-10-12", "event": 6, "leagues_updated": True, "points": "r"},
    ],
    "leagues": "Updated",
}


def _with_last_day(payload: dict, **changes) -> dict:
    days = [dict(day) for day in payload["status"]]
    days[-1].update(changes)
    return {**payload, "status": days}


class TestGameweekStatusModel:
    @pytest.mark.parametrize("payload", [CLASSIC_SETTLED, DRAFT_SETTLED], ids=["classic", "draft"])
    def test_the_settled_feeds_read_as_settled(self, payload):
        status = GameweekStatus.model_validate(payload)

        assert status.bonus_confirmed(6) is True
        assert status.league_tables_updated(6) is True
        assert status.still_processing(6) is None

    def test_updating_is_not_updated(self):
        status = GameweekStatus.model_validate({**CLASSIC_SETTLED, "leagues": "Updating"})

        assert status.league_tables_updated(6) is False
        assert status.still_processing(6) == "league tables are still updating"

    @pytest.mark.parametrize("leagues", ["", None, "Processing"])
    def test_an_unrecognised_state_is_unknown_not_a_no(self, leagues):
        """Only a definite signal is evidence of a stale table: the empty
        string reported between gameweeks says nothing either way."""
        status = GameweekStatus.model_validate({**CLASSIC_SETTLED, "leagues": leagues})

        assert status.league_tables_updated(6) is None
        assert status.still_processing(6) is None

    def test_one_match_day_without_bonus_holds_the_gameweek_back(self):
        status = GameweekStatus.model_validate(_with_last_day(CLASSIC_SETTLED, bonus_added=False))

        assert status.bonus_confirmed(6) is False
        assert status.still_processing(6) == "bonus is not yet confirmed for every match day"

    def test_a_draft_day_not_yet_in_the_tables_overrides_the_headline(self):
        status = GameweekStatus.model_validate(_with_last_day(DRAFT_SETTLED, leagues_updated=False))

        assert status.league_tables_updated(6) is False

    def test_a_gameweek_the_feed_has_no_day_for_is_unknown(self):
        status = GameweekStatus.model_validate(CLASSIC_SETTLED)

        assert status.bonus_confirmed(5) is None
        assert status.league_tables_updated(5) is None
        assert status.still_processing(5) is None

    def test_another_gameweeks_day_neither_silences_nor_settles_this_one(self):
        """A day from a second gameweek (a rescheduled fixture, say) is
        judged against its own gameweek, so GW6 still reads as updating."""
        payload = _with_last_day(CLASSIC_SETTLED, event=7, bonus_added=False)
        status = GameweekStatus.model_validate({**payload, "leagues": "Updating"})

        assert status.bonus_confirmed(6) is True
        assert status.still_processing(6) == "league tables are still updating"
        assert status.bonus_confirmed(7) is False

    @pytest.mark.parametrize("payload", [
        {"status": [{"event": 6, "bonus_added": True}], "league_state": "Updated"},
        {"status": [{"event": 6, "bonus": True}], "leagues": "Updated"},
        {"days": [{"event": 6, "bonus_added": True}], "leagues": "Updated"},
    ], ids=["leagues-renamed", "bonus-renamed", "status-renamed"])
    def test_a_reshaped_feed_fails_validation_rather_than_reading_as_a_no(self, payload):
        with pytest.raises(pydantic.ValidationError):
            GameweekStatus.model_validate(payload)


class TestClientEndpoints:
    async def test_classic_reads_event_status(self):
        client = FPLClient()
        with patch.object(client, "_get", new_callable=AsyncMock, return_value=CLASSIC_SETTLED) as get:
            status = await client.get_gameweek_status()

        get.assert_called_once_with("event-status/")
        assert status.bonus_confirmed(6) is True

    async def test_draft_reads_its_own_feed(self):
        client = FPLDraftClient()
        with patch.object(client, "_get", new_callable=AsyncMock, return_value=DRAFT_SETTLED) as get:
            status = await client.get_gameweek_status()

        get.assert_called_once_with("pl/event-status")
        assert status.league_tables_updated(6) is True


class TestFetchDegrades:
    """A feed that cannot be read is a missing caveat, never a failed command."""

    @pytest.mark.parametrize("error", [
        httpx.ConnectError("connection refused"),
        ValueError("not JSON"),
    ], ids=["unreachable", "malformed"])
    async def test_a_failed_read_is_none(self, error):
        client = MagicMock()
        client.get_gameweek_status = AsyncMock(side_effect=error)

        assert await fetch_gameweek_status(client) is None

    async def test_a_reshaped_feed_is_none(self):
        client = FPLClient()
        with patch.object(client, "_get", new_callable=AsyncMock, return_value={"status": "Updated"}):
            assert await fetch_gameweek_status(client) is None

    async def test_a_stalled_feed_is_given_up_on(self):
        """The feed is advisory, so it does not get the client's 30s."""
        async def _stall():
            await asyncio.sleep(5)

        client = MagicMock()
        client.get_gameweek_status = _stall
        with patch("fpl_cli.cli._gameweek_status.FEED_TIMEOUT_SECONDS", 0.01):
            assert await fetch_gameweek_status(client) is None


class TestSettledFlags:
    def test_one_settled_feed(self):
        assert settled_flags([make_gameweek_status(6)], 6) == (True, True)

    def test_one_game_still_updating_is_a_no(self):
        statuses = [make_gameweek_status(6), make_gameweek_status(6, leagues="Updating")]

        assert settled_flags(statuses, 6) == (True, False)

    def test_a_no_outranks_an_unread_feed(self):
        statuses = [None, make_gameweek_status(6, leagues="Updating")]

        assert settled_flags(statuses, 6) == (None, False)

    def test_an_unread_feed_leaves_a_yes_unknown(self):
        """True only when every game shown says so."""
        assert settled_flags([make_gameweek_status(6), None], 6) == (None, None)

    def test_a_feed_with_no_day_for_the_gameweek_is_unknown(self):
        assert settled_flags([make_gameweek_status(7)], 6) == (None, None)

    def test_no_feeds_is_unknown(self):
        assert settled_flags([], 6) == (None, None)
