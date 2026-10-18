"""The draft-picks snapshot kept beside the league-history ledger (#461).

The draft API serves only a league's latest draft, so a held draft's picks
disappear from it once a redraft is scheduled. These pin down the snapshot that
keeps them: written once, never rewritten, and loud when it disagrees with the
API or when a draft's picks are already gone.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from fpl_cli.cli._league_recap_history import (
    HISTORY_WARNING_DRAFT_PICKS_CONFLICT,
    HISTORY_WARNING_DRAFT_PICKS_LOST,
    HISTORY_WARNING_DRAFT_PICKS_UNSAVED,
    capture_draft_picks,
)
from fpl_cli.cli._league_recap_types import LeagueRecapData, RecapAwards
from fpl_cli.paths import user_data_dir
from fpl_cli.services.league_history import partition_dir
from fpl_cli.services.league_history_drafts import (
    LEAGUE_DRAFT_KEY,
    DraftSnapshotStatus,
    completed_drafts,
    draft_snapshot_path,
    snapshot_draft_choices,
)

SEASON = "2026-27"
LEAGUE_ID = 42
DRAFT_ID = 901
REDRAFT_ID = 902


def _choice(index: int, *, draft: int = DRAFT_ID, entry: int = 100, **extra: Any) -> dict[str, Any]:
    """One pick row, shaped as the choices endpoint serves it."""
    return {
        "id": 5000 + index,
        "draft": draft,
        "index": index,
        "round": (index - 1) // 2 + 1,
        "pick": (index - 1) % 2 + 1,
        "entry": entry,
        "element": 300 + index,
        "was_auto": False,
        "choice_time": f"2026-08-10T18:{index:02d}:00Z",
        "entry_name": "Team A",
        **extra,
    }


def _response(*rows: dict[str, Any], owner: int = 100) -> dict[str, Any]:
    """A choices response; `owner` stands in for today's ownership beside the picks."""
    return {
        "choices": list(rows),
        "element_status": [
            {"element": 301, "owner": owner, "status": "o", "in_accepted_trade": False},
        ],
        "idle": [],
    }


def _picks(draft: int = DRAFT_ID) -> list[dict[str, Any]]:
    return [_choice(1, draft=draft), _choice(2, draft=draft, entry=101), _choice(3, draft=draft)]


def _league_draft(draft_id: int = DRAFT_ID, *, completed: bool = True, event: int = 1) -> dict[str, Any]:
    return {
        "id": draft_id,
        "event": event,
        "draft_dt": "2026-08-10T18:00:00Z",
        "draft_completed": completed,
        "order_method": "random",
    }


def _snapshot(
    league_drafts: list[dict[str, Any]], choices: dict[str, Any],
) -> list[Any]:
    return snapshot_draft_choices(SEASON, LEAGUE_ID, league_drafts, choices)


class TestSnapshotPath:
    def test_lives_in_the_draft_ledger_partition_beside_the_gameweek_files(self):
        path = draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)

        assert path == partition_dir(SEASON, "draft", LEAGUE_ID) / "drafts" / f"draft-{DRAFT_ID}.json"
        assert path.is_relative_to(user_data_dir() / "league_history")

    def test_follows_a_data_dir_override_set_after_import(self, tmp_path, monkeypatch):
        """Resolved at point of use, like every path in `paths.py`'s care."""
        before = draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)
        moved = tmp_path / "moved-data"
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(moved))
        user_data_dir.cache_clear()

        after = draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)

        assert after != before
        assert after == moved / "league_history" / SEASON / f"draft-{LEAGUE_ID}" / "drafts" / f"draft-{DRAFT_ID}.json"

    def test_a_snapshot_does_not_read_as_a_captured_gameweek(self):
        from fpl_cli.services.league_history import LeagueHistoryStore

        _snapshot([_league_draft()], _response(*_picks()))

        assert LeagueHistoryStore(SEASON, "draft", LEAGUE_ID).captured_gameweeks() == []


class TestCompletedDrafts:
    def test_keeps_only_held_drafts_with_an_id(self):
        drafts = [
            _league_draft(DRAFT_ID),
            _league_draft(REDRAFT_ID, completed=False),
            {"event": 1, "draft_completed": True},
            {"id": True, "draft_completed": True},
            "not a draft",
        ]

        assert [d["id"] for d in completed_drafts(drafts)] == [DRAFT_ID]


class TestSnapshotWrite:
    def test_a_completed_draft_is_saved_verbatim_with_its_league_entry(self):
        response = _response(*_picks())

        [outcome] = _snapshot([_league_draft()], response)

        assert outcome.status is DraftSnapshotStatus.SAVED
        assert outcome.path == draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)
        stored = json.loads(outcome.path.read_text(encoding="utf-8"))
        assert stored == {**response, LEAGUE_DRAFT_KEY: _league_draft()}

    def test_an_identical_second_run_writes_nothing(self):
        _snapshot([_league_draft()], _response(*_picks()))
        path = draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)
        before = path.read_bytes()

        with patch("fpl_cli.services.league_history_drafts.atomic_write_text") as write:
            [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.UNCHANGED
        write.assert_not_called()
        assert path.read_bytes() == before

    def test_todays_ownership_and_team_names_moving_on_are_not_a_difference(self):
        """`element_status` is every player's owner today and changes with each
        waiver round; a team rename changes the rows' display names. The picks
        are what was held, and they have not changed."""
        _snapshot([_league_draft()], _response(*_picks()))
        path = draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)
        before = path.read_bytes()
        moved_on = _response(
            *[{**row, "entry_name": "Renamed FC"} for row in reversed(_picks())], owner=101,
        )

        [outcome] = _snapshot([_league_draft()], moved_on)

        assert outcome.status is DraftSnapshotStatus.UNCHANGED
        assert path.read_bytes() == before

    def test_differing_picks_are_refused_and_the_file_kept(self):
        _snapshot([_league_draft()], _response(*_picks()))
        path = draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)
        before = path.read_bytes()
        changed = _picks()
        changed[1] = {**changed[1], "element": 999}

        [outcome] = _snapshot([_league_draft()], _response(*changed))

        assert outcome.status is DraftSnapshotStatus.DIFFERS
        assert outcome.detail is not None
        assert "3 picks" in outcome.detail
        assert "1 of them not in the file" in outcome.detail
        assert path.read_bytes() == before

    def test_an_unreadable_file_is_left_alone(self):
        path = draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)
        path.parent.mkdir(parents=True)
        path.write_text('{"choices": [', encoding="utf-8")

        [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.UNREADABLE
        assert path.read_text(encoding="utf-8") == '{"choices": ['

    def test_a_file_with_no_choices_list_is_unreadable(self):
        path = draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)
        path.parent.mkdir(parents=True)
        path.write_text('{"detail": "Not found."}', encoding="utf-8")

        [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.UNREADABLE

    def test_a_manual_capture_of_the_endpoint_counts_as_saved(self):
        """The fallback the raw format exists for: the response saved straight
        to the path, compact and without `league_draft`, is the same picks and
        is kept exactly as it is."""
        path = draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)
        path.parent.mkdir(parents=True)
        manual = json.dumps(_response(*_picks()), separators=(",", ":"))
        path.write_text(manual, encoding="utf-8")

        [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.UNCHANGED
        assert path.read_text(encoding="utf-8") == manual

    def test_a_scheduled_but_unheld_draft_writes_nothing(self):
        assert _snapshot([_league_draft(completed=False)], _response()) == []

        assert not draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID).exists()

    def test_a_draft_under_way_is_not_saved_until_it_completes(self):
        """A partial draft saved now would read as a conflict once it finished."""
        assert _snapshot([_league_draft(completed=False)], _response(*_picks()[:1])) == []

        assert not draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID).exists()

    def test_a_write_failure_is_an_outcome_not_an_exception(self):
        with patch(
            "fpl_cli.services.league_history_drafts.atomic_write_text",
            side_effect=OSError("No space left on device"),
        ):
            [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.WRITE_FAILED
        assert outcome.detail == "No space left on device"


class TestDraftsNoLongerServed:
    def test_a_completed_draft_with_no_rows_and_no_snapshot_is_lost(self):
        """The redraft is scheduled: the endpoint serves nothing for the draft
        that was held, and nobody saved it."""
        drafts = [_league_draft(), _league_draft(REDRAFT_ID, completed=False, event=20)]

        [outcome] = _snapshot(drafts, _response())

        assert outcome.status is DraftSnapshotStatus.LOST
        assert outcome.draft_id == DRAFT_ID
        assert outcome.served_draft_ids == ()
        assert not outcome.path.exists()

    def test_after_the_redraft_the_earlier_draft_is_kept_and_the_new_one_saved(self):
        drafts = [_league_draft(), _league_draft(REDRAFT_ID, event=20)]
        _snapshot([_league_draft()], _response(*_picks()))

        kept, saved = _snapshot(drafts, _response(*_picks(REDRAFT_ID)))

        assert (kept.draft_id, kept.status) == (DRAFT_ID, DraftSnapshotStatus.KEPT)
        assert (saved.draft_id, saved.status) == (REDRAFT_ID, DraftSnapshotStatus.SAVED)
        first = json.loads(kept.path.read_text(encoding="utf-8"))
        assert {row["draft"] for row in first["choices"]} == {DRAFT_ID}

    def test_a_lost_draft_names_the_draft_served_instead(self):
        drafts = [_league_draft(), _league_draft(REDRAFT_ID, event=20)]

        lost, saved = _snapshot(drafts, _response(*_picks(REDRAFT_ID)))

        assert lost.status is DraftSnapshotStatus.LOST
        assert lost.served_draft_ids == (REDRAFT_ID,)
        assert saved.status is DraftSnapshotStatus.SAVED


def _draft_data(league_drafts: list[dict[str, Any]] | None, **overrides: Any) -> LeagueRecapData:
    data = LeagueRecapData(
        gameweek=5,
        league_name="Test League",
        fpl_format=overrides.get("fpl_format", "draft"),
        managers=[],
        awards=RecapAwards(),
        league_id=overrides.get("league_id", LEAGUE_ID),
    )
    if league_drafts is not None:
        data["league_drafts"] = league_drafts
    if "league_id" in overrides and overrides["league_id"] is None:
        del data["league_id"]
    return data


class TestCaptureDraftPicks:
    async def test_saves_and_says_where(self, capsys):
        fetch = AsyncMock(return_value=_response(*_picks()))

        warnings = await capture_draft_picks(
            _draft_data([_league_draft()]), season=SEASON, fetch_choices=fetch,
        )

        assert warnings == []
        fetch.assert_awaited_once_with(LEAGUE_ID)
        assert draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID).is_file()
        assert f"Saved the picks of draft {DRAFT_ID}" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(_draft_data([_league_draft()], fpl_format="classic"), id="classic"),
            pytest.param(_draft_data([_league_draft(completed=False)]), id="no-draft-held-yet"),
            pytest.param(_draft_data(None), id="no-drafts-list"),
            pytest.param(_draft_data([_league_draft()], league_id=None), id="no-league-id"),
        ],
    )
    async def test_asks_the_api_nothing_when_there_is_nothing_to_keep(self, data):
        fetch = AsyncMock()

        assert await capture_draft_picks(data, season=SEASON, fetch_choices=fetch) == []
        fetch.assert_not_awaited()

    async def test_differing_picks_raise_the_conflict_warning(self, capsys):
        _snapshot([_league_draft()], _response(*_picks()))
        changed = [{**row, "entry": 102} for row in _picks()]

        warnings = await capture_draft_picks(
            _draft_data([_league_draft()]), season=SEASON,
            fetch_choices=AsyncMock(return_value=_response(*changed)),
        )

        [warning] = warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_CONFLICT
        assert f"draft {DRAFT_ID}" in warning["message"]
        assert "left as it is" in warning["message"]
        assert "mv '" in warning["message"]
        assert "differ from the ones the draft API serves now" in capsys.readouterr().err.replace("\n", "")

    async def test_an_unreadable_file_raises_the_conflict_warning(self):
        path = draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")

        [warning] = await capture_draft_picks(
            _draft_data([_league_draft()]), season=SEASON,
            fetch_choices=AsyncMock(return_value=_response(*_picks())),
        )

        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_CONFLICT
        assert "could not be read" in warning["message"]
        assert path.read_text(encoding="utf-8") == "not json"

    async def test_a_lost_draft_is_named(self, capsys):
        [warning] = await capture_draft_picks(
            _draft_data([_league_draft(), _league_draft(REDRAFT_ID, completed=False)]),
            season=SEASON, fetch_choices=AsyncMock(return_value=_response()),
        )

        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_LOST
        assert f"Draft {DRAFT_ID} has been held" in warning["message"]
        assert str(draft_snapshot_path(SEASON, LEAGUE_ID, DRAFT_ID)) in warning["message"]
        assert f"Draft {DRAFT_ID} has been held" in capsys.readouterr().err.replace("\n", "")

    async def test_a_kept_draft_says_nothing(self, capsys):
        _snapshot([_league_draft()], _response(*_picks()))
        capsys.readouterr()

        warnings = await capture_draft_picks(
            _draft_data([_league_draft(), _league_draft(REDRAFT_ID, event=20)]),
            season=SEASON, fetch_choices=AsyncMock(return_value=_response(*_picks(REDRAFT_ID))),
        )

        assert warnings == []
        assert f"draft {DRAFT_ID}" not in capsys.readouterr().err

    @pytest.mark.parametrize(
        "failure",
        [httpx.ConnectError("unreachable"), ValueError("not JSON")],
        ids=["unreachable", "not-json"],
    )
    async def test_an_unreadable_api_warns_while_a_draft_is_unsaved(self, failure):
        [warning] = await capture_draft_picks(
            _draft_data([_league_draft()]), season=SEASON,
            fetch_choices=AsyncMock(side_effect=failure),
        )

        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_UNSAVED
        assert f"draft {DRAFT_ID} are still not saved" in warning["message"]

    async def test_a_response_that_is_not_an_object_warns_like_a_failed_read(self):
        [warning] = await capture_draft_picks(
            _draft_data([_league_draft()]), season=SEASON,
            fetch_choices=AsyncMock(return_value=[]),
        )

        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_UNSAVED

    async def test_an_unreadable_api_costs_nothing_once_every_draft_is_saved(self):
        _snapshot([_league_draft()], _response(*_picks()))

        warnings = await capture_draft_picks(
            _draft_data([_league_draft()]), season=SEASON,
            fetch_choices=AsyncMock(side_effect=httpx.ConnectError("unreachable")),
        )

        assert warnings == []

    async def test_a_write_failure_warns_and_the_next_run_retries(self):
        with patch(
            "fpl_cli.services.league_history_drafts.atomic_write_text",
            side_effect=OSError("Read-only file system"),
        ):
            [warning] = await capture_draft_picks(
                _draft_data([_league_draft()]), season=SEASON,
                fetch_choices=AsyncMock(return_value=_response(*_picks())),
            )

        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_UNSAVED
        assert "Read-only file system" in warning["message"]
        assert "next run tries again" in warning["message"]
