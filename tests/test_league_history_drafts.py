"""The draft-picks snapshot kept beside the league-history ledger (#461).

The draft API serves only a league's latest draft, so a held draft's picks
disappear from it once a redraft is scheduled. These pin down the snapshot that
keeps them: written once, never rewritten, and loud when it disagrees with the
API or when a draft's picks are already gone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from fpl_cli.cli._league_recap_history import (
    HISTORY_WARNING_DRAFT_PICKS_CONFLICT,
    HISTORY_WARNING_DRAFT_PICKS_LOST,
    HISTORY_WARNING_DRAFT_PICKS_UNCHECKED,
    HISTORY_WARNING_DRAFT_PICKS_UNSAVED,
    DraftPicksCapture,
    capture_draft_picks,
)
from fpl_cli.cli._league_recap_types import LeagueRecapData, RecapAwards
from fpl_cli.paths import user_data_dir
from fpl_cli.services.league_history import LeagueHistoryStore, partition_dir
from fpl_cli.services.league_history_drafts import (
    LEAGUE_DRAFT_KEY,
    DraftSnapshotOutcome,
    DraftSnapshotStatus,
    completed_drafts,
    draft_snapshot_path,
    has_saved_picks,
    is_recognised_draft,
    lost_draft_path,
    snapshot_draft_choices,
)
from tests.conftest import make_history_row

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


HELD_AT = "2026-08-13T19:47:59.528867Z"


def _league_draft(
    draft_id: int = DRAFT_ID, *, completed: bool | str | None = HELD_AT, event: int = 1,
) -> dict[str, Any]:
    return {
        "id": draft_id,
        "event": event,
        "draft_dt": "2026-08-10T18:00:00Z",
        "draft_completed": completed,
        "order_method": "random",
    }


def _snapshot(league_drafts: list[Any], choices: dict[str, Any]) -> list[DraftSnapshotOutcome]:
    return snapshot_draft_choices(SEASON, LEAGUE_ID, completed_drafts(league_drafts), choices)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _snapshot_file(draft_id: int = DRAFT_ID) -> Path:
    return draft_snapshot_path(SEASON, LEAGUE_ID, draft_id)


class TestSnapshotPath:
    def test_lives_in_the_draft_ledger_partition_beside_the_gameweek_files(self):
        path = _snapshot_file()

        assert path == partition_dir(SEASON, "draft", LEAGUE_ID) / "drafts" / f"draft-{DRAFT_ID}.json"
        assert path.is_relative_to(user_data_dir() / "league_history")
        assert lost_draft_path(SEASON, LEAGUE_ID, DRAFT_ID).parent == path.parent

    def test_follows_a_data_dir_override_set_after_import(self, tmp_path, monkeypatch):
        """Resolved at point of use, like every path in `paths.py`'s care."""
        before = _snapshot_file()
        moved = tmp_path / "moved-data"
        monkeypatch.setenv("FPL_CLI_DATA_DIR", str(moved))
        user_data_dir.cache_clear()

        after = _snapshot_file()

        assert after != before
        assert after == moved / "league_history" / SEASON / f"draft-{LEAGUE_ID}" / "drafts" / f"draft-{DRAFT_ID}.json"

    def test_a_snapshot_does_not_read_as_a_captured_gameweek(self):
        _snapshot([_league_draft()], _response(*_picks()))

        assert LeagueHistoryStore(SEASON, "draft", LEAGUE_ID).captured_gameweeks() == []

    def test_a_snapshot_alone_does_not_make_the_partition_exist(self):
        """A hand-saved copy, or one written on a run whose cohort wrote no
        rows, lands before any gameweek. The next recap must still find the
        partition new and say where it is recording."""
        store = LeagueHistoryStore(SEASON, "draft", LEAGUE_ID)
        _snapshot([_league_draft()], _response(*_picks()))

        assert store.partition_dir().is_dir()
        assert store.partition_exists() is False

        store.append_rows(5, [make_history_row(gameweek=5, gross_points=50)])
        assert store.partition_exists() is True


class TestDraftRecognition:
    def test_keeps_only_held_drafts_with_an_id(self):
        drafts = [
            _league_draft(DRAFT_ID),
            _league_draft(REDRAFT_ID, completed=None),
            {"event": 1, "draft_completed": HELD_AT},
            {"id": True, "draft_completed": HELD_AT},
            "not a draft",
        ]

        assert [d["id"] for d in completed_drafts(drafts)] == [DRAFT_ID]

    def test_the_live_api_shape_a_timestamp_when_held_and_null_when_scheduled(self):
        drafts = [
            _league_draft(DRAFT_ID, completed=HELD_AT),
            _league_draft(REDRAFT_ID, completed=None, event=19),
        ]

        assert all(is_recognised_draft(d) for d in drafts)
        assert [d["id"] for d in completed_drafts(drafts)] == [DRAFT_ID]

    def test_a_held_draft_with_a_timestamp_is_snapshotted(self):
        [outcome] = _snapshot(
            [_league_draft(DRAFT_ID, completed=HELD_AT), _league_draft(REDRAFT_ID, completed=None)],
            _response(*_picks()),
        )

        assert outcome.status is DraftSnapshotStatus.SAVED

    @pytest.mark.parametrize(
        ("entry", "recognised"),
        [
            pytest.param(_league_draft(completed=True), True, id="held-bool"),
            pytest.param(_league_draft(completed=False), True, id="scheduled-bool"),
            pytest.param(_league_draft(completed=HELD_AT), True, id="held-timestamp"),
            pytest.param(_league_draft(completed=None), True, id="scheduled-null"),
            pytest.param({"id": DRAFT_ID}, False, id="no-completed-flag"),
            pytest.param({"id": DRAFT_ID, "draft_completed": 1}, False, id="flag-an-int"),
            pytest.param({"id": DRAFT_ID, "draft_completed": ""}, False, id="flag-an-empty-string"),
            pytest.param({"id": DRAFT_ID, "draft_completed": "false"}, False, id="flag-not-a-timestamp"),
            pytest.param({"id": DRAFT_ID, "draft_completed": "0"}, False, id="flag-a-zero-string"),
            pytest.param({"id": DRAFT_ID, "draft_completed": {}}, False, id="flag-an-object"),
            pytest.param({"id": "901", "draft_completed": True}, False, id="id-not-a-number"),
            pytest.param({"draft_id": DRAFT_ID, "draft_completed": True}, False, id="id-renamed"),
            pytest.param(None, False, id="not-an-object"),
        ],
    )
    def test_an_entry_is_recognised_only_in_the_shape_it_is_read_in(self, entry, recognised):
        assert is_recognised_draft(entry) is recognised


class TestSnapshotWrite:
    def test_a_completed_draft_is_saved_verbatim_with_its_league_entry(self):
        response = _response(*_picks())

        [outcome] = _snapshot([_league_draft()], response)

        assert outcome.status is DraftSnapshotStatus.SAVED
        assert outcome.path == _snapshot_file()
        stored = json.loads(outcome.path.read_text(encoding="utf-8"))
        assert stored == {**response, LEAGUE_DRAFT_KEY: _league_draft()}
        assert has_saved_picks(SEASON, LEAGUE_ID, DRAFT_ID)

    def test_an_identical_second_run_writes_nothing(self):
        _snapshot([_league_draft()], _response(*_picks()))
        before = _snapshot_file().read_bytes()

        with patch("fpl_cli.services.league_history_drafts.atomic_write_text") as write:
            [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.UNCHANGED
        write.assert_not_called()
        assert _snapshot_file().read_bytes() == before

    def test_todays_ownership_and_team_names_moving_on_are_not_a_difference(self):
        """`element_status` is every player's owner today and changes with each
        waiver round; a team rename changes the rows' display names. The picks
        are what was held, and they have not changed."""
        _snapshot([_league_draft()], _response(*_picks()))
        before = _snapshot_file().read_bytes()
        moved_on = _response(
            *[{**row, "entry_name": "Renamed FC"} for row in reversed(_picks())], owner=101,
        )

        [outcome] = _snapshot([_league_draft()], moved_on)

        assert outcome.status is DraftSnapshotStatus.UNCHANGED
        assert _snapshot_file().read_bytes() == before

    def test_differing_picks_are_refused_and_the_file_kept(self):
        _snapshot([_league_draft()], _response(*_picks()))
        before = _snapshot_file().read_bytes()
        changed = _picks()
        changed[1] = {**changed[1], "element": 999}

        [outcome] = _snapshot([_league_draft()], _response(*changed))

        assert outcome.status is DraftSnapshotStatus.DIFFERS
        assert outcome.detail == (
            f"holds 3 picks for draft {DRAFT_ID} where the API serves 3: 1 served are not in "
            "the file, and 1 in the file are not served"
        )
        assert _snapshot_file().read_bytes() == before

    def test_a_difference_is_described_in_both_directions(self):
        """The file holding a pick the API no longer serves is a difference
        too, and must not read as "0 of them not in the file"."""
        _snapshot([_league_draft()], _response(*_picks()))

        [outcome] = _snapshot([_league_draft()], _response(*_picks()[:2]))

        assert outcome.status is DraftSnapshotStatus.DIFFERS
        assert outcome.detail is not None
        assert "where the API serves 2: 0 served are not in the file, and 1 in the file are not served" in outcome.detail

    def test_a_file_holding_none_of_a_served_drafts_picks_differs(self):
        """A capture taken after the redraft was scheduled: valid, but empty."""
        _write(_snapshot_file(), json.dumps(_response()))

        [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.DIFFERS
        assert json.loads(_snapshot_file().read_text(encoding="utf-8")) == _response()

    @pytest.mark.parametrize(
        ("text", "detail"),
        [
            pytest.param('{"choices": [', "is not valid JSON", id="truncated"),
            pytest.param('{"detail": "Not found."}', "holds no `choices` list", id="error-body"),
        ],
    )
    def test_an_unreadable_file_is_left_alone(self, text, detail):
        _write(_snapshot_file(), text)

        [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.UNREADABLE
        assert outcome.detail is not None and outcome.detail.startswith(detail)
        assert _snapshot_file().read_text(encoding="utf-8") == text

    def test_a_permission_error_reading_the_file_is_an_outcome(self):
        with patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
            [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.UNREADABLE
        assert outcome.detail is not None and "Permission denied" in outcome.detail

    def test_a_manual_capture_of_the_endpoint_counts_as_saved(self):
        """The fallback the raw format exists for: the response saved straight
        to the path, compact and without `league_draft`, is the same picks and
        is kept exactly as it is."""
        manual = json.dumps(_response(*_picks()), separators=(",", ":"))
        _write(_snapshot_file(), manual)

        [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.UNCHANGED
        assert _snapshot_file().read_text(encoding="utf-8") == manual

    def test_a_scheduled_but_unheld_draft_writes_nothing(self):
        assert _snapshot([_league_draft(completed=None)], _response()) == []

        assert not _snapshot_file().exists()

    def test_a_draft_under_way_is_not_saved_until_it_completes(self):
        """A partial draft saved now would read as a conflict once it finished."""
        assert _snapshot([_league_draft(completed=None)], _response(*_picks()[:1])) == []

        assert not _snapshot_file().exists()

    @pytest.mark.parametrize(
        "failure",
        [OSError("No space left on device"), UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")],
        ids=["disk-full", "unencodable"],
    )
    def test_a_write_failure_is_an_outcome_not_an_exception(self, failure):
        with patch("fpl_cli.services.league_history_drafts.atomic_write_text", side_effect=failure):
            [outcome] = _snapshot([_league_draft()], _response(*_picks()))

        assert outcome.status is DraftSnapshotStatus.WRITE_FAILED
        assert outcome.detail == str(failure)

    def test_a_response_that_cannot_be_encoded_is_not_written_half_way(self):
        unencodable = [{**_picks()[0], "entry_name": "\ud800"}]

        [outcome] = _snapshot([_league_draft()], _response(*unencodable))

        assert outcome.status is DraftSnapshotStatus.WRITE_FAILED
        assert not _snapshot_file().exists()
        assert list(_snapshot_file().parent.iterdir()) == []


class TestDraftsNoLongerServed:
    def test_a_completed_draft_with_no_rows_and_no_snapshot_is_lost(self):
        """The redraft is scheduled: the endpoint serves nothing for the draft
        that was held, and nobody saved it."""
        drafts = [_league_draft(), _league_draft(REDRAFT_ID, completed=None, event=20)]

        [outcome] = _snapshot(drafts, _response())

        assert outcome.status is DraftSnapshotStatus.LOST
        assert outcome.draft_id == DRAFT_ID
        assert outcome.served_draft_ids == ()
        assert outcome.detail is None
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

    @pytest.mark.parametrize(
        ("text", "detail"),
        [
            pytest.param("garbage", "is not valid JSON", id="garbage"),
            pytest.param('{"choices": []}', "holds none of its picks", id="empty-capture"),
            pytest.param(
                json.dumps(_response(*_picks(REDRAFT_ID))), "holds none of its picks", id="wrong-draft",
            ),
            pytest.param("<html>Service unavailable</html>", "is not valid JSON", id="error-page"),
        ],
    )
    def test_a_file_without_the_picks_is_no_copy(self, text, detail):
        """A hand-saved curl taken after the redraft was scheduled, or an
        error page, must not pass as the saved copy and silence the loss."""
        _write(_snapshot_file(), text)

        [outcome] = _snapshot([_league_draft()], _response())

        assert outcome.status is DraftSnapshotStatus.LOST
        assert outcome.detail is not None and outcome.detail.startswith(detail)
        assert _snapshot_file().read_text(encoding="utf-8") == text
        assert not has_saved_picks(SEASON, LEAGUE_ID, DRAFT_ID)

    def test_a_loss_is_recorded_once(self):
        [first] = _snapshot([_league_draft()], _response(*_picks(REDRAFT_ID)))
        [second] = _snapshot([_league_draft()], _response(*_picks(REDRAFT_ID)))

        assert first.status is DraftSnapshotStatus.LOST
        assert second.status is DraftSnapshotStatus.LOST_NOTED
        marker = json.loads(lost_draft_path(SEASON, LEAGUE_ID, DRAFT_ID).read_text(encoding="utf-8"))
        assert marker["draft_id"] == DRAFT_ID
        assert marker[LEAGUE_DRAFT_KEY] == _league_draft()
        assert marker["served_draft_ids"] == [REDRAFT_ID]
        assert marker["noticed_at"]

    def test_a_copy_put_in_place_later_is_kept_over_the_recorded_loss(self):
        _snapshot([_league_draft()], _response())
        _write(_snapshot_file(), json.dumps(_response(*_picks())))

        [outcome] = _snapshot([_league_draft()], _response())

        assert outcome.status is DraftSnapshotStatus.KEPT

    def test_a_loss_that_cannot_be_recorded_is_reported_again(self):
        with patch.object(Path, "is_file", side_effect=PermissionError(13, "Permission denied")):
            [outcome] = _snapshot([_league_draft()], _response())

        assert outcome.status is DraftSnapshotStatus.LOST
        assert not lost_draft_path(SEASON, LEAGUE_ID, DRAFT_ID).exists()


def _draft_data(league_drafts: Any = ..., **overrides: Any) -> LeagueRecapData:
    """A draft recap's data, `league_drafts` absent unless passed."""
    data = LeagueRecapData(
        gameweek=5,
        league_name="Test League",
        fpl_format=overrides.get("fpl_format", "draft"),
        managers=[],
        awards=RecapAwards(),
        league_id=LEAGUE_ID,
    )
    if league_drafts is not ...:
        data["league_drafts"] = league_drafts
    if overrides.get("no_league_id"):
        del data["league_id"]
    return data


async def _capture(data: LeagueRecapData, fetch: AsyncMock) -> DraftPicksCapture:
    return await capture_draft_picks(data, season=SEASON, fetch_choices=fetch)


class TestCaptureDraftPicks:
    async def test_saves_and_says_where(self, capsys):
        fetch = AsyncMock(return_value=_response(*_picks()))

        result = await _capture(_draft_data([_league_draft()]), fetch)

        assert result.warnings == []
        assert result.saved == [_snapshot_file()]
        fetch.assert_awaited_once_with(LEAGUE_ID)
        assert f"Saved the picks of draft {DRAFT_ID}" in capsys.readouterr().err

    async def test_a_second_run_saves_nothing_new(self):
        await _capture(_draft_data([_league_draft()]), AsyncMock(return_value=_response(*_picks())))

        result = await _capture(_draft_data([_league_draft()]), AsyncMock(return_value=_response(*_picks())))

        assert result == DraftPicksCapture()

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(_draft_data([_league_draft()], fpl_format="classic"), id="classic"),
            pytest.param(_draft_data([_league_draft(completed=None)]), id="no-draft-held-yet"),
            pytest.param(_draft_data([]), id="no-drafts-yet"),
            pytest.param(_draft_data(), id="details-never-read"),
            pytest.param(_draft_data([_league_draft()], no_league_id=True), id="no-league-id"),
        ],
    )
    async def test_asks_the_api_nothing_when_there_is_nothing_to_keep(self, data):
        fetch = AsyncMock()

        assert await _capture(data, fetch) == DraftPicksCapture()
        fetch.assert_not_awaited()

    async def test_a_missing_drafts_list_is_said_rather_than_skipped(self, capsys):
        fetch = AsyncMock()

        result = await _capture(_draft_data(None), fetch)

        [warning] = result.warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_UNCHECKED
        assert "no `league.drafts` list" in warning["message"]
        fetch.assert_not_awaited()
        assert "no `league.drafts` list" in capsys.readouterr().err.replace("\n", "")

    async def test_an_unrecognised_draft_is_said_and_the_rest_still_saved(self):
        drafts = [{"id": REDRAFT_ID, "completed": True}, _league_draft()]

        result = await _capture(_draft_data(drafts), AsyncMock(return_value=_response(*_picks())))

        [warning] = result.warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_UNCHECKED
        assert "1 of the 2 drafts" in warning["message"]
        assert result.saved == [_snapshot_file()]

    async def test_differing_picks_raise_the_conflict_warning(self, capsys):
        _snapshot([_league_draft()], _response(*_picks()))
        changed = [{**row, "entry": 102} for row in _picks()]

        result = await _capture(_draft_data([_league_draft()]), AsyncMock(return_value=_response(*changed)))

        [warning] = result.warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_CONFLICT
        assert f"draft {DRAFT_ID}" in warning["message"]
        assert "3 served are not in the file, and 3 in the file are not served" in warning["message"]
        assert "left as it is" in warning["message"]
        assert "mv '" in warning["message"]
        assert "differ from the ones the draft API serves now" in capsys.readouterr().err.replace("\n", "")

    async def test_an_unreadable_file_raises_the_conflict_warning(self):
        _write(_snapshot_file(), "not json")

        result = await _capture(_draft_data([_league_draft()]), AsyncMock(return_value=_response(*_picks())))

        [warning] = result.warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_CONFLICT
        assert "is not valid JSON" in warning["message"]
        assert _snapshot_file().read_text(encoding="utf-8") == "not json"

    async def test_a_lost_draft_is_named_once(self, capsys):
        data = _draft_data([_league_draft(), _league_draft(REDRAFT_ID, completed=None)])

        first = await _capture(data, AsyncMock(return_value=_response()))
        second = await _capture(data, AsyncMock(return_value=_response()))

        [warning] = first.warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_LOST
        assert f"Draft {DRAFT_ID} has been held" in warning["message"]
        assert "none were saved while it did" in warning["message"]
        assert str(_snapshot_file()) in warning["message"]
        assert f"lost-draft-{DRAFT_ID}.json" in warning["message"]
        assert second.warnings == []
        assert capsys.readouterr().err.replace("\n", "").count(f"Draft {DRAFT_ID} has been held") == 1

    async def test_a_lost_draft_with_a_useless_file_says_what_is_wrong_with_it(self):
        _write(_snapshot_file(), '{"choices": []}')

        result = await _capture(_draft_data([_league_draft()]), AsyncMock(return_value=_response()))

        [warning] = result.warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_LOST
        assert f"the file at {_snapshot_file()} holds none of its picks" in warning["message"]

    async def test_a_kept_draft_says_nothing(self, capsys):
        _snapshot([_league_draft()], _response(*_picks()))
        capsys.readouterr()

        result = await _capture(
            _draft_data([_league_draft(), _league_draft(REDRAFT_ID, event=20)]),
            AsyncMock(return_value=_response(*_picks(REDRAFT_ID))),
        )

        assert result.warnings == []
        assert f"draft {DRAFT_ID}" not in capsys.readouterr().err

    @pytest.mark.parametrize(
        "fetch",
        [
            pytest.param(AsyncMock(side_effect=httpx.ConnectError("unreachable")), id="unreachable"),
            pytest.param(AsyncMock(side_effect=httpx.InvalidURL("bad url")), id="invalid-url"),
            pytest.param(AsyncMock(side_effect=ValueError("not JSON")), id="not-json"),
            pytest.param(AsyncMock(return_value=[]), id="not-an-object"),
            pytest.param(AsyncMock(return_value={"detail": "maintenance"}), id="no-choices-list"),
            pytest.param(AsyncMock(return_value={"choices": None}), id="choices-not-a-list"),
        ],
    )
    async def test_an_unreadable_api_warns_while_a_draft_is_unsaved(self, fetch):
        """A failed read, a reshaped body or a maintenance object is a retry,
        never "no picks served": read as that, it would report the draft as
        gone for good."""
        result = await _capture(_draft_data([_league_draft()]), fetch)

        [warning] = result.warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_UNSAVED
        assert f"draft {DRAFT_ID} are still not saved" in warning["message"]
        assert not lost_draft_path(SEASON, LEAGUE_ID, DRAFT_ID).exists()

    async def test_an_unreadable_api_costs_nothing_once_every_draft_is_saved(self):
        _snapshot([_league_draft()], _response(*_picks()))

        result = await _capture(
            _draft_data([_league_draft()]), AsyncMock(side_effect=httpx.ConnectError("unreachable")),
        )

        assert result.warnings == []

    async def test_a_file_without_the_picks_does_not_count_as_saved_when_the_api_is_down(self):
        _write(_snapshot_file(), '{"choices": []}')

        result = await _capture(
            _draft_data([_league_draft()]), AsyncMock(side_effect=httpx.ConnectError("unreachable")),
        )

        [warning] = result.warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_UNSAVED

    async def test_a_permission_error_on_the_saved_copy_does_not_escape(self):
        with patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
            result = await _capture(
                _draft_data([_league_draft()]), AsyncMock(side_effect=httpx.ConnectError("unreachable")),
            )

        [warning] = result.warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_UNSAVED

    async def test_a_write_failure_warns_and_the_next_run_retries(self):
        with patch(
            "fpl_cli.services.league_history_drafts.atomic_write_text",
            side_effect=OSError("Read-only file system"),
        ):
            result = await _capture(
                _draft_data([_league_draft()]), AsyncMock(return_value=_response(*_picks())),
            )

        [warning] = result.warnings
        assert warning["code"] == HISTORY_WARNING_DRAFT_PICKS_UNSAVED
        assert "Read-only file system" in warning["message"]
        assert "next run tries again" in warning["message"]
        assert result.saved == []
