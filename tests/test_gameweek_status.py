"""FPL's post-gameweek processing feed: the model, both clients, the CLI helpers (issue #424)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from fpl_cli.api.fpl import FPLClient
from fpl_cli.api.fpl_draft import FPLDraftClient
from fpl_cli.cli._gameweek_status import (
    fetch_draft_gameweek_status,
    fetch_gameweek_status,
    gameweek_still_processing,
    settled_flags,
)
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


class TestGameweekStatusModel:
    @pytest.mark.parametrize("payload", [CLASSIC_SETTLED, DRAFT_SETTLED], ids=["classic", "draft"])
    def test_the_settled_feeds_read_as_settled(self, payload):
        status = GameweekStatus.model_validate(payload)

        assert status.gameweek == 6
        assert status.bonus_confirmed
        assert status.league_tables_updated

    @pytest.mark.parametrize("leagues", ["Updating", "", None])
    def test_anything_but_updated_is_not_updated(self, leagues):
        status = GameweekStatus.model_validate({**CLASSIC_SETTLED, "leagues": leagues})

        assert not status.league_tables_updated
        assert status.bonus_confirmed

    def test_one_match_day_without_bonus_holds_the_gameweek_back(self):
        days = [dict(day) for day in CLASSIC_SETTLED["status"]]
        days[-1]["bonus_added"] = False

        status = GameweekStatus.model_validate({**CLASSIC_SETTLED, "status": days})

        assert not status.bonus_confirmed

    def test_a_draft_day_not_yet_in_the_tables_overrides_the_headline(self):
        days = [dict(day) for day in DRAFT_SETTLED["status"]]
        days[-1]["leagues_updated"] = False

        status = GameweekStatus.model_validate({**DRAFT_SETTLED, "status": days})

        assert not status.league_tables_updated

    def test_an_empty_feed_covers_no_gameweek_and_confirms_nothing(self):
        status = GameweekStatus.model_validate({"status": [], "leagues": ""})

        assert status.gameweek is None
        assert not status.bonus_confirmed

    def test_a_feed_naming_two_gameweeks_covers_neither(self):
        days = [dict(day) for day in CLASSIC_SETTLED["status"]]
        days[-1]["event"] = 7

        assert GameweekStatus.model_validate({**CLASSIC_SETTLED, "status": days}).gameweek is None


class TestClientEndpoints:
    async def test_classic_reads_event_status(self):
        client = FPLClient()
        with patch.object(client, "_get", new_callable=AsyncMock, return_value=CLASSIC_SETTLED) as get:
            status = await client.get_gameweek_status()

        get.assert_called_once_with("event-status/")
        assert status.gameweek == 6

    async def test_draft_reads_its_own_feed(self):
        client = FPLDraftClient()
        with patch.object(client, "_get", new_callable=AsyncMock, return_value=DRAFT_SETTLED) as get:
            status = await client.get_gameweek_status()

        get.assert_called_once_with("pl/event-status")
        assert status.league_tables_updated


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

    async def test_a_draft_client_that_cannot_open_is_none(self):
        draft = MagicMock()
        draft.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        draft.__aexit__ = AsyncMock(return_value=False)

        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft):
            assert await fetch_draft_gameweek_status() is None


class TestStillProcessing:
    def test_settled_is_none(self):
        assert gameweek_still_processing(make_gameweek_status(6), 6) is None

    def test_league_tables_updating_says_so(self):
        reason = gameweek_still_processing(make_gameweek_status(6, leagues="Updating"), 6)

        assert reason == "league tables are still updating"

    def test_unconfirmed_bonus_is_named_first(self):
        status = make_gameweek_status(6, bonus_added=False, leagues="Updating")

        assert gameweek_still_processing(status, 6) == "bonus is not yet confirmed for every match day"

    def test_a_feed_on_another_gameweek_says_nothing_about_this_one(self):
        """The feed moves to the next gameweek at its deadline, so an older
        recap, or one run after the next gameweek started, is not warned."""
        assert gameweek_still_processing(make_gameweek_status(7, leagues="Updating"), 6) is None

    def test_an_unread_feed_is_not_evidence_of_a_stale_table(self):
        assert gameweek_still_processing(None, 6) is None


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
        """Settled only when every game the user plays says so."""
        assert settled_flags([make_gameweek_status(6), None], 6) == (None, None)

    def test_a_feed_on_another_gameweek_is_unknown(self):
        assert settled_flags([make_gameweek_status(7)], 6) == (None, None)

    def test_no_feeds_is_unknown(self):
        assert settled_flags([], 6) == (None, None)
