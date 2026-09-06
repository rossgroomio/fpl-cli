"""Deriving a past gameweek's clubs from the gameweek itself (issue #177)."""

from __future__ import annotations

import httpx

from fpl_cli.services.player_clubs import (
    GameweekClubResolver,
    club_from_player_detail,
    derived_player_codes,
    gameweek_club,
    narrow_clubs,
    resolve_player_fixtures,
    settle_clubs,
)
from tests.conftest import make_fixture, make_player, make_team

# GW10: club 1 hosts club 2, club 3 hosts club 4, and club 1 plays twice.
FIXTURES = [
    make_fixture(id=71, home_team_id=1, away_team_id=2, finished=True, started=True),
    make_fixture(id=72, home_team_id=3, away_team_id=4, finished=True, started=True),
    make_fixture(id=73, home_team_id=1, away_team_id=4, finished=True, started=True),
]


def _live(*explains: tuple[int, list[int]]) -> dict:
    return {"elements": [
        {"id": player_id, "stats": {}, "explain": [{"fixture": f, "stats": []} for f in fixture_ids]}
        for player_id, fixture_ids in explains
    ]}


class TestResolvePlayerFixtures:
    def test_a_finished_gameweek_names_each_players_own_fixtures(self):
        live = _live((10, [71, 73]), (20, [72]), (30, []))
        assert resolve_player_fixtures(live, FIXTURES) == {
            10: frozenset({71, 73}), 20: frozenset({72}),
        }

    def test_a_gameweek_still_in_play_declines_to_answer(self):
        fixtures = [*FIXTURES, make_fixture(id=74, finished=False, started=False)]
        assert resolve_player_fixtures(_live((10, [71])), fixtures) is None

    def test_a_payload_whose_explains_have_not_populated_declines_too(self):
        assert resolve_player_fixtures(_live((10, []), (20, [])), FIXTURES) is None

    def test_no_fixtures_at_all_declines_to_answer(self):
        assert resolve_player_fixtures(_live((10, [71])), []) is None

    def test_every_explain_id_is_kept_so_the_had_fixture_answer_cannot_drift(self):
        """`frozenset(result)` has to equal `resolve_players_with_fixture`'s
        own answer, which counts an `explain` entry whatever it names. An id
        with no pair in this gameweek drops out in `narrow_clubs` instead."""
        assert resolve_player_fixtures(_live((10, [71, 999])), FIXTURES) == {
            10: frozenset({71, 999}),
        }


class TestNarrowClubs:
    def test_a_double_gameweek_settles_the_club_outright(self):
        """Two different fixtures share exactly the club whose fixtures they are."""
        assert narrow_clubs({10: frozenset({71, 73})}, FIXTURES) == {10: frozenset({1})}

    def test_a_single_fixture_leaves_both_its_clubs(self):
        assert narrow_clubs({10: frozenset({71})}, FIXTURES) == {10: frozenset({1, 2})}

    def test_a_fixture_from_another_gameweek_constrains_nothing(self):
        """A stray id would otherwise widen an intersection rather than
        narrow it, or empty it outright."""
        assert narrow_clubs({10: frozenset({71, 999})}, FIXTURES) == {10: frozenset({1, 2})}

    def test_a_player_whose_fixtures_are_all_unknown_drops_out(self):
        assert narrow_clubs({10: frozenset({999})}, FIXTURES) == {}


class TestSettleClubs:
    def test_a_double_settles_without_consulting_todays_club(self):
        settled, moved = settle_clubs({10: frozenset({1})}, {10: 4})
        assert settled == {10: 1}
        assert moved == frozenset()

    def test_todays_club_inside_the_pair_settles_nothing(self):
        """It reads as "he has not moved", but a player who moved between
        those exact two clubs is the same shape and the pair cannot tell them
        apart. Answering would put a guess where the ladder wants silence."""
        settled, moved = settle_clubs({10: frozenset({1, 2})}, {10: 2})
        assert settled == {}
        assert moved == frozenset()

    def test_a_transfer_to_the_opponent_of_his_own_fixture_is_never_asserted(self):
        """He was at club 1 in a week club 1 played club 2, and has since
        moved to club 2. Reading today's club as the answer would record club
        2 for a gameweek he spent at club 1 -- and it could never be flagged,
        since the wrong answer equals today's club by construction. Declining
        leaves the recorded club (then today's) to stand."""
        settled, moved = settle_clubs({10: frozenset({1, 2})}, {10: 2})
        assert 10 not in settled
        assert 10 not in moved

    def test_todays_club_outside_the_pair_is_proof_he_moved(self):
        settled, moved = settle_clubs({10: frozenset({1, 2})}, {10: 4})
        assert settled == {}
        assert moved == frozenset({10})

    def test_a_player_the_bootstrap_does_not_know_is_left_alone(self):
        settled, moved = settle_clubs({10: frozenset({1, 2})}, {})
        assert settled == {}
        assert moved == frozenset()


class TestClubFromPlayerDetail:
    def test_was_home_names_the_side_he_was_on(self):
        detail = {"history": [
            {"fixture": 71, "was_home": False, "round": 10, "opponent_team": 1},
        ]}
        assert club_from_player_detail(detail, frozenset({71}), FIXTURES) == 2

    def test_the_home_side_resolves_the_other_way(self):
        detail = {"history": [{"fixture": 71, "was_home": True, "round": 10}]}
        assert club_from_player_detail(detail, frozenset({71}), FIXTURES) == 1

    def test_history_about_other_gameweeks_is_ignored(self):
        detail = {"history": [
            {"fixture": 72, "was_home": True, "round": 10},
            {"fixture": 71, "was_home": False, "round": 10},
        ]}
        assert club_from_player_detail(detail, frozenset({71}), FIXTURES) == 2

    def test_a_history_that_says_nothing_about_the_fixture_gives_no_answer(self):
        assert club_from_player_detail({"history": []}, frozenset({71}), FIXTURES) is None


class TestGameweekClub:
    _TEAMS = {t.id: t for t in (
        make_team(id=1, name="Alpha", short_name="ALP"),
        make_team(id=2, name="Beta", short_name="BET"),
    )}

    def test_the_gameweeks_answer_beats_the_club_the_player_is_at_today(self):
        club = gameweek_club(10, 1, clubs={10: 2}, teams=self._TEAMS)
        assert club is not None and club.short_name == "BET"

    def test_a_player_the_gameweek_could_not_place_keeps_todays_club(self):
        club = gameweek_club(10, 1, clubs={}, teams=self._TEAMS)
        assert club is not None and club.short_name == "ALP"

    def test_without_an_answer_at_all_todays_club_stands(self):
        club = gameweek_club(10, 1, clubs=None, teams=self._TEAMS)
        assert club is not None and club.short_name == "ALP"

    def test_a_player_with_no_main_game_id_falls_back_to_his_club(self):
        """An unmatched draft player has nothing to look up in the live data."""
        club = gameweek_club(None, 1, clubs={10: 2}, teams=self._TEAMS)
        assert club is not None and club.short_name == "ALP"

    def test_an_unresolvable_club_is_no_club_rather_than_a_crash(self):
        assert gameweek_club(10, 99, clubs=None, teams=self._TEAMS) is None
        assert gameweek_club(None, None, clubs=None, teams=self._TEAMS) is None


class TestDerivedPlayerCodes:
    def test_every_player_the_gameweek_placed_is_named(self):
        players = [
            make_player(id=10, code=1000, team_id=4),
            make_player(id=20, code=2000, team_id=3),
        ]
        assert derived_player_codes(players, {10: 1}) == [1000]

    def test_a_derived_club_matching_todays_is_still_named(self):
        """The case a "differs from today's club" test would drop: he left and
        came back, so the gameweek's club and today's agree -- while the row on
        disk holds a stale capture-time stamp that must not win."""
        players = [make_player(id=10, code=1000, team_id=1)]
        assert derived_player_codes(players, {10: 1}) == [1000]

    def test_a_player_the_gameweek_could_not_place_is_not_named(self):
        players = [make_player(id=10, code=1000, team_id=4)]
        assert derived_player_codes(players, {20: 1}) == []

    def test_no_answer_names_nobody(self):
        players = [make_player(id=10, code=1000, team_id=4)]
        assert derived_player_codes(players, None) == []

    def test_a_player_with_no_code_cannot_be_named(self):
        players = [make_player(id=10, code=0, team_id=4)]
        assert derived_player_codes(players, {10: 1}) == []


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid/")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request),
    )


class _DetailClient:
    """Stands in for FPLClient's one method the moved-player lookup calls."""

    def __init__(self, details: dict[int, dict], error: Exception | None = None) -> None:
        self._details = details
        self._error = error or _http_error(404)
        self.calls: list[int] = []

    async def get_player_detail(self, player_id: int, /) -> dict:
        self.calls.append(player_id)
        if player_id not in self._details:
            raise self._error
        return self._details[player_id]


class TestGameweekClubResolver:
    async def test_a_double_needs_no_lookup_at_all(self):
        client = _DetailClient({})
        resolved = await GameweekClubResolver(client).resolve(
            _live((10, [71, 73])), FIXTURES, {10: 4},
        )
        assert resolved is not None
        assert resolved.clubs == {10: 1}
        assert client.calls == []

    async def test_a_moved_player_is_placed_by_his_own_history(self):
        client = _DetailClient({10: {"history": [{"fixture": 71, "was_home": False}]}})
        resolved = await GameweekClubResolver(client).resolve(
            _live((10, [71]), (20, [72])), FIXTURES, {10: 4, 20: 3},
        )
        assert resolved is not None
        # Player 20's club is one of his own fixture's pair, so the gameweek
        # declines rather than asserting today's club (see settle_clubs).
        assert resolved.clubs == {10: 2}
        assert client.calls == [10]

    async def test_only_the_drifted_player_costs_a_call(self):
        client = _DetailClient({10: {"history": [{"fixture": 71, "was_home": True}]}})
        live = _live((10, [71]), (20, [71]), (30, [72]))
        await GameweekClubResolver(client).resolve(live, FIXTURES, {10: 4, 20: 1, 30: 3})
        assert client.calls == [10]

    async def test_a_failed_lookup_leaves_the_player_on_todays_club(self):
        client = _DetailClient({})
        resolved = await GameweekClubResolver(client).resolve(
            _live((10, [71])), FIXTURES, {10: 4},
        )
        assert resolved is not None
        assert resolved.clubs == {}

    async def test_the_detail_fetch_is_shared_across_replayed_gameweeks(self):
        """A backfill replays every gameweek and the same players moved for all
        of them; `element-summary` answers for the whole season in one call."""
        client = _DetailClient({10: {"history": [
            {"fixture": 71, "was_home": False},
            {"fixture": 73, "was_home": True},
        ]}})
        resolver = GameweekClubResolver(client)
        # Club 3 today, so he is outside both fixtures' pairs and each
        # gameweek needs him placed -- one response settles both.
        first = await resolver.resolve(_live((10, [71])), FIXTURES, {10: 3})
        second = await resolver.resolve(_live((10, [73])), FIXTURES, {10: 3})
        assert first is not None and second is not None
        assert first.clubs == {10: 2}
        assert second.clubs == {10: 1}
        assert client.calls == [10]

    async def test_a_permanently_failed_lookup_is_not_retried_every_gameweek(self):
        client = _DetailClient({})
        resolver = GameweekClubResolver(client)
        await resolver.resolve(_live((10, [71])), FIXTURES, {10: 4})
        await resolver.resolve(_live((10, [71])), FIXTURES, {10: 4})
        assert client.calls == [10]

    async def test_a_transient_failure_is_retried_on_the_next_gameweek(self):
        """One resolver serves a whole backfill, so caching a timeout would
        let one blip cost this player every gameweek after it."""
        client = _DetailClient({}, error=httpx.ReadTimeout("slow"))
        resolver = GameweekClubResolver(client)
        await resolver.resolve(_live((10, [71])), FIXTURES, {10: 4})
        await resolver.resolve(_live((10, [71])), FIXTURES, {10: 4})
        assert client.calls == [10, 10]

    async def test_a_server_error_is_transient_too(self):
        client = _DetailClient({}, error=_http_error(503))
        resolver = GameweekClubResolver(client)
        await resolver.resolve(_live((10, [71])), FIXTURES, {10: 4})
        await resolver.resolve(_live((10, [71])), FIXTURES, {10: 4})
        assert client.calls == [10, 10]

    async def test_an_unreadable_history_costs_one_player_not_the_batch(self):
        """`club_from_player_detail` runs outside the fetch's own guard, and
        one raise there used to abort every other lookup and the recap."""
        client = _DetailClient({
            10: {"history": "not a list of rows"},
            20: {"history": [{"fixture": 72, "was_home": True}]},
        })
        resolved = await GameweekClubResolver(client).resolve(
            _live((10, [71]), (20, [72])), FIXTURES, {10: 4, 20: 1},
        )
        assert resolved is not None
        assert resolved.clubs == {20: 3}

    async def test_the_with_fixture_set_comes_off_the_same_pass(self):
        """`had_fixture` and the club stamping read the same `explain`
        entries, so one scan answers both."""
        client = _DetailClient({})
        resolved = await GameweekClubResolver(client).resolve(
            _live((10, [71, 73]), (20, [72]), (30, [])), FIXTURES, {10: 1, 20: 3},
        )
        assert resolved is not None
        assert resolved.with_fixture == frozenset({10, 20})

    async def test_a_gameweek_that_cannot_answer_declines_rather_than_guessing(self):
        client = _DetailClient({})
        fixtures = [*FIXTURES, make_fixture(id=74, finished=False)]
        assert await GameweekClubResolver(client).resolve(
            _live((10, [71])), fixtures, {10: 4},
        ) is None
