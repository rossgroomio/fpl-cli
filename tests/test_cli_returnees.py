"""Tests for `fpl returnees` command."""

from __future__ import annotations

import json
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.api.providers import LLMResponse, ProviderError, ProviderNotConfiguredError, TokenUsage
from fpl_cli.cli import main
from fpl_cli.models.player import PlayerPosition, PlayerStatus
from fpl_cli.season import season_label
from fpl_cli.services import returnee_radar
from fpl_cli.services.player_prior import PlayerPrior
from tests.conftest import make_player, make_team

# The radar reads the clock for lapsing and news age, and the season for
# resolving a bare "5 Sep" into a year. Neither is a CLI flag, so the tests
# pin both by wrapping the service entry point -- everything inside it
# (quality bar, window, snapshot store) still runs for real.
SEASON_YEAR = 2026
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
NEXT_GW = 3

_FIRST_DEADLINE = datetime(2026, 8, 14, 17, 30, tzinfo=timezone.utc)


def _deadline(gw: int) -> datetime:
    return _FIRST_DEADLINE + timedelta(days=7 * (gw - 1))


GAMEWEEKS: list[dict[str, Any]] = [
    {
        "id": gw,
        "deadline_time": _deadline(gw).isoformat().replace("+00:00", "Z"),
        "is_next": gw == NEXT_GW,
    }
    for gw in range(1, 11)
]


def _returning_in_gw(gw: int) -> str:
    """News text whose stated date resolves to exactly *gw*."""
    day = _deadline(gw).date()
    return f"Hamstring injury - Expected back {day.day} {day:%b}"


_REAL_RUN_RADAR = returnee_radar.run_radar


def _pinned_run_radar(players: Any, **kwargs: Any) -> Any:
    kwargs["now"] = NOW
    kwargs["season_year"] = SEASON_YEAR
    return _REAL_RUN_RADAR(players, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _flagged(
    pid: int,
    web_name: str,
    *,
    news: str = "Knee injury - Unknown return date",
    status: PlayerStatus = PlayerStatus.INJURED,
    chance: int | None = None,
    now_cost: int = 100,
    news_days: int = 2,
) -> Any:
    return make_player(
        id=pid,
        code=1000 + pid,
        web_name=web_name,
        team_id=1,
        position=PlayerPosition.MIDFIELDER,
        now_cost=now_cost,
        status=status,
        news=news,
        news_added=(NOW - timedelta(days=news_days)).isoformat().replace("+00:00", "Z"),
        chance_of_playing_next_round=chance,
    )


def _default_players() -> list[Any]:
    """Three flagged players plus one fit one, spanning the radar's branches."""
    return [
        _flagged(1, "Sidelined", chance=25),
        _flagged(2, "Duesoon", news=_returning_in_gw(4), chance=75),
        _flagged(3, "Duelater", news=_returning_in_gw(7)),
        _flagged(4, "Journeyman", now_cost=45),
        make_player(id=5, code=1005, web_name="Fitasafiddle", team_id=1,
                    position=PlayerPosition.MIDFIELDER, status=PlayerStatus.AVAILABLE),
    ]


def _default_priors() -> dict[int, PlayerPrior]:
    """Journeyman (4) is the only flagged player below the quality bar."""
    strong = PlayerPrior(prior_strength=0.9, confidence=1.0, source="history")
    return {
        1: strong,
        2: strong,
        3: strong,
        4: PlayerPrior(prior_strength=0.1, confidence=1.0, source="history"),
        5: strong,
    }


_UNSET: Any = object()


def _scoring_data(
    players: list[Any] | None = None, priors: Any = _UNSET, next_gw_id: int = NEXT_GW,
) -> Any:
    """A ScoringData stand-in. `priors=None` is the degraded-priors run."""
    data = MagicMock()
    data.players = _default_players() if players is None else players
    data.player_priors = _default_priors() if priors is _UNSET else priors
    data.teams = [make_team(id=1, name="Liverpool", short_name="LIV")]
    data.next_gw_id = next_gw_id
    return data


def _make_client() -> Any:
    client = MagicMock()
    client.get_gameweeks = AsyncMock(return_value=GAMEWEEKS)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@asynccontextmanager
async def _fake_provider(profiles: dict[int, Any] | None = None):
    provider = MagicMock()
    provider.get_all_player_histories = AsyncMock(return_value=profiles or {})
    yield provider


def _make_understat() -> Any:
    client = MagicMock()
    client.get_league_players = AsyncMock(return_value=[])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _run(args: list[str] | None = None, *, scoring_data: Any = None,
         prepare_error: Exception | None = None,
         provider_factory: Any = None,
         settings: dict[str, Any] | None = None) -> Any:
    """Invoke `fpl returnees` with every network seam stubbed.

    `provider_factory` stands in for `get_llm_provider`, so no test can reach
    a live LLM endpoint. The default raises the missing-key error, which is
    what an unconfigured machine sees.
    """
    if scoring_data is None:
        scoring_data = _scoring_data()
    if provider_factory is None:
        provider_factory = _unconfigured_factory()

    prepare = AsyncMock(side_effect=prepare_error) if prepare_error is not None \
        else AsyncMock(return_value=scoring_data)

    runner = CliRunner()
    with ExitStack() as stack:
        stack.enter_context(patch(
            "fpl_cli.api.providers.get_llm_provider", new=provider_factory,
        ))
        if settings is not None:
            stack.enter_context(patch(
                "fpl_cli.cli.returnees.load_settings", return_value=settings,
            ))
        stack.enter_context(patch("fpl_cli.api.fpl.FPLClient", return_value=_make_client()))
        stack.enter_context(patch("fpl_cli.services.scoring.prepare_scoring_data", new=prepare))
        stack.enter_context(patch(
            "fpl_cli.api.historical.make_historical_provider",
            side_effect=lambda *a, **kw: _fake_provider(),
        ))
        stack.enter_context(patch(
            "fpl_cli.api.understat.UnderstatClient",
            side_effect=lambda *a, **kw: _make_understat(),
        ))
        stack.enter_context(patch(
            "fpl_cli.services.returnee_radar.run_radar", new=_pinned_run_radar,
        ))
        return runner.invoke(
            main, ["returnees"] + (args or []), env={"COLUMNS": "220"},
        )


def _unconfigured_factory() -> Any:
    """A `get_llm_provider` stand-in for a machine with no research API key."""
    return MagicMock(side_effect=ProviderNotConfiguredError(
        "PERPLEXITY_API_KEY not set. Get your key from https://example.invalid",
    ))


def _stub_provider(by_name: dict[str, Any] | None = None, default: Any = None) -> Any:
    """A research provider that answers per player, keyed on the prompt.

    Keying on the name rather than on call order keeps each expectation
    attached to the player it is about. An answer that is an exception is
    raised instead of returned, so a provider failure runs through the same
    seam as a successful one.
    """
    answers = by_name or {}

    async def _query(prompt: str = "", system_prompt: str | None = None, **kwargs: Any) -> Any:
        answer = next(
            (value for name, value in answers.items() if name in prompt),
            default if default is not None else _intel(),
        )
        if isinstance(answer, Exception):
            raise answer
        return answer

    provider = MagicMock()
    provider.query = AsyncMock(side_effect=_query)
    provider.post_process = lambda text: text
    provider.close = AsyncMock()
    return provider


def _provider_factory(provider: Any) -> Any:
    return MagicMock(return_value=provider)


def _intel(
    expected_return: str | None = None,
    *,
    summary: str = "Back in full training",
    confidence: str = "medium",
    citations: list[str] | None = None,
) -> LLMResponse:
    """One provider answer, shaped as the prompt asks for it."""
    body = json.dumps({
        "expected_return": expected_return,
        "summary": summary,
        "confidence": confidence,
    })
    return LLMResponse(
        content=body,
        model="sonar-pro",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
        citations=citations if citations is not None else [],
    )


def _enrichment_scoring_data(next_gw_id: int = NEXT_GW) -> Any:
    """The default pool plus a player FPL dated a long time ago.

    Spans the shortlist rule in one fixture: date-unknown (Sidelined), freshly
    dated (Duesoon, Duelater) and dated-but-stale (Stalenews).
    """
    players = [*_default_players(), _flagged(6, "Stalenews", news=_returning_in_gw(6),
                                             news_days=30)]
    priors = _default_priors()
    priors[6] = PlayerPrior(prior_strength=0.9, confidence=1.0, source="history")
    return _scoring_data(players=players, priors=priors, next_gw_id=next_gw_id)


def _entries_by_name(payload: dict[str, Any]) -> dict[str, Any]:
    return {entry["web_name"]: entry for entry in payload["data"]["entries"]}


def _prompts(provider: Any) -> list[str]:
    """Every user prompt the run actually sent."""
    return [call.kwargs.get("prompt", "") for call in provider.query.await_args_list]


def _json_run(args: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Parse the envelope off stdout alone — notes belong on stderr."""
    result = _run(["--format", "json"] + (args or []), **kwargs)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _names(payload: dict[str, Any]) -> list[str]:
    return [entry["web_name"] for entry in payload["data"]["entries"]]


# ---------------------------------------------------------------------------
# Table output
# ---------------------------------------------------------------------------


class TestTableOutput:
    def test_renders_a_row_per_entry(self):
        result = _run()

        assert result.exit_code == 0, result.output
        for name in ("Sidelined", "Duesoon", "Duelater"):
            assert name in result.output
        # Team, position, expected return, chance and transition columns.
        assert "Liverpool" in result.output
        assert "MID" in result.output
        assert "GW4" in result.output
        assert "75%" in result.output
        assert "Change" in result.output

    def test_quality_bar_keeps_the_weak_player_out(self):
        result = _run()

        assert "Journeyman" not in result.output

    def test_date_unknown_entry_renders_an_explicit_unknown_marker(self):
        result = _run()

        # Sidelined has no parseable date: the cell must say so, not sit empty.
        assert "Unknown" in result.output

    def test_departures_are_reported_when_a_tracked_player_is_fit_again(self):
        returnee_radar.save_snapshot(returnee_radar.RadarSnapshot(
            season=season_label(SEASON_YEAR),
            gameweek=NEXT_GW - 1,
            players={5: returnee_radar.SnapshotRecord(status="i", web_name="Fitasafiddle")},
        ))

        result = _run()

        assert result.exit_code == 0, result.output
        assert "Fitasafiddle" in result.output


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_envelope_shape(self):
        payload = _json_run()

        assert payload["command"] == "returnees"
        assert "metadata" in payload
        assert "data" in payload
        assert payload["metadata"]["season"] == season_label()
        assert payload["metadata"]["gameweek"] == NEXT_GW

    def test_metadata_carries_window_and_availability_flags(self):
        payload = _json_run()

        metadata = payload["metadata"]
        assert metadata["window"] == 6
        assert metadata["transitions_available"] is False
        assert metadata["quality_bar_available"] is True
        assert metadata["quality_bar_applied"] is True

    def test_entries_carry_the_radar_fields(self):
        payload = _json_run()

        by_name = {e["web_name"]: e for e in payload["data"]["entries"]}
        due_soon = by_name["Duesoon"]
        assert due_soon["return_gameweek"] == 4
        assert due_soon["expected_return"] == _deadline(4).date().isoformat()
        assert due_soon["return_known"] is True
        assert due_soon["chance_of_playing"] == 75
        assert due_soon["team"] == "Liverpool"
        assert due_soon["position"] == "MID"
        assert due_soon["quality"]["basis"] == "prior"

        unknown = by_name["Sidelined"]
        assert unknown["expected_return"] is None
        assert unknown["return_known"] is False

    def test_table_and_json_are_driven_from_the_same_assembled_data(self):
        players = _default_players()
        players[0] = _flagged(1, "Renamedplayer")
        scoring_data = _scoring_data(players=players)

        table = _run(scoring_data=scoring_data)
        payload = _json_run(scoring_data=_scoring_data(players=players))

        assert "Renamedplayer" in table.output
        assert "Renamedplayer" in _names(payload)


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


class TestFlags:
    def test_window_narrows_the_result_set(self):
        default = _json_run()
        narrowed = _json_run(["--window", "2"])

        assert "Duelater" in _names(default)
        assert "Duelater" not in _names(narrowed)
        assert narrowed["metadata"]["window"] == 2

    def test_all_includes_an_entry_the_quality_bar_dropped(self):
        default = _json_run()
        everyone = _json_run(["--all"])

        assert "Journeyman" not in _names(default)
        assert "Journeyman" in _names(everyone)
        assert everyone["metadata"]["quality_bar_applied"] is False

    def test_all_does_not_persist_a_filter_bypassed_watchlist(self):
        _run(["--all"])
        bypassed = returnee_radar.snapshot_path().exists()
        _run()

        # Storing the wider list would make the next ordinary run report
        # everyone it re-excluded as having dropped off the watchlist.
        assert bypassed is False
        assert returnee_radar.snapshot_path().exists()


# ---------------------------------------------------------------------------
# Week-over-week deltas
# ---------------------------------------------------------------------------


class TestDeltas:
    def test_first_run_reports_deltas_unavailable_second_run_reports_them_present(self):
        first = _json_run()
        second = _json_run()

        assert first["metadata"]["transitions_available"] is False
        assert second["metadata"]["transitions_available"] is True

    def test_table_notes_the_first_run_has_nothing_to_diff(self):
        first = _run()
        second = _run()

        assert "first" in first.output.lower()
        assert "first" not in second.output.lower()


# ---------------------------------------------------------------------------
# Empty and degraded states
# ---------------------------------------------------------------------------


class TestEmptyAndDegraded:
    def test_no_flagged_players_prints_an_empty_state_and_exits_zero(self):
        fit_only = [make_player(id=5, code=1005, web_name="Fitasafiddle", team_id=1,
                                status=PlayerStatus.AVAILABLE)]
        scoring_data = _scoring_data(players=fit_only, priors={5: _default_priors()[5]})

        result = _run(scoring_data=scoring_data)

        assert result.exit_code == 0, result.output
        assert "No" in result.output

    def test_no_flagged_players_emits_an_empty_json_list(self):
        fit_only = [make_player(id=5, code=1005, web_name="Fitasafiddle", team_id=1,
                                status=PlayerStatus.AVAILABLE)]
        payload = _json_run(scoring_data=_scoring_data(
            players=fit_only, priors={5: _default_priors()[5]},
        ))

        assert payload["data"]["entries"] == []
        assert payload["metadata"]["quality_bar_available"] is True

    def test_degraded_priors_note_goes_to_stderr_and_exits_zero(self):
        result = _run(scoring_data=_scoring_data(priors=None))

        assert result.exit_code == 0, result.output
        assert "quality bar" in result.stderr
        assert "--all" in result.stderr

    def test_degraded_priors_sets_the_metadata_flag_false(self):
        payload = _json_run(scoring_data=_scoring_data(priors=None))

        assert payload["metadata"]["quality_bar_available"] is False
        assert payload["data"]["entries"] == []

    def test_all_still_lists_flagged_players_when_priors_are_unavailable(self):
        payload = _json_run(["--all"], scoring_data=_scoring_data(priors=None))

        assert "Sidelined" in _names(payload)
        assert payload["metadata"]["quality_bar_available"] is False


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestFailures:
    def test_upstream_failure_emits_the_json_error_envelope(self):
        result = _run(["--format", "json"], prepare_error=RuntimeError("bootstrap unreachable"))

        assert result.exit_code == 1
        error = json.loads(result.stderr)
        assert error["command"] == "returnees"
        assert "bootstrap unreachable" in error["error"]

    def test_upstream_failure_in_table_mode_exits_nonzero(self):
        result = _run(prepare_error=RuntimeError("bootstrap unreachable"))

        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# AI-search enrichment (--enrich)
# ---------------------------------------------------------------------------


class TestEnrichmentProviderGate:
    def test_missing_key_still_produces_the_deterministic_watchlist(self):
        result = _run(["--enrich"])

        assert result.exit_code == 0, result.output
        for name in ("Sidelined", "Duesoon", "Duelater"):
            assert name in result.output

    def test_missing_key_note_goes_to_stderr_not_stdout(self):
        result = _run(["--enrich"])

        assert "PERPLEXITY_API_KEY" in result.stderr
        assert "PERPLEXITY_API_KEY" not in result.stdout

    def test_missing_key_is_recorded_in_json_metadata_not_as_a_failed_envelope(self):
        payload = _json_run(["--enrich"])

        metadata = payload["metadata"]
        assert metadata["enrichment_requested"] is True
        assert metadata["enrichment_available"] is False
        assert "PERPLEXITY_API_KEY" in metadata["enrichment_note"]
        assert metadata["enrichment_count"] == 0
        assert payload["data"]["entries"], "the deterministic watchlist must stand on its own"

    def test_provider_is_never_constructed_without_the_flag(self):
        factory = _unconfigured_factory()

        result = _run(provider_factory=factory)

        assert result.exit_code == 0, result.output
        factory.assert_not_called()

    def test_metadata_reports_enrichment_unrequested_without_the_flag(self):
        metadata = _json_run()["metadata"]

        assert metadata["enrichment_requested"] is False
        assert metadata["enrichment_available"] is False
        assert metadata["enrichment_note"] is None


class TestEnrichmentShortlist:
    def test_only_date_unknown_and_stale_news_entries_are_queried(self):
        provider = _stub_provider()

        result = _run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                      provider_factory=_provider_factory(provider))

        assert result.exit_code == 0, result.output
        prompts = _prompts(provider)
        assert len(prompts) == 2
        assert any("Sidelined" in prompt for prompt in prompts)
        assert any("Stalenews" in prompt for prompt in prompts)
        # Freshly dated entries have nothing to top up.
        assert not any("Duesoon" in prompt or "Duelater" in prompt for prompt in prompts)

    def test_the_query_ceiling_bounds_the_shortlist(self):
        provider = _stub_provider()

        _run(["--enrich"], scoring_data=_enrichment_scoring_data(),
             provider_factory=_provider_factory(provider),
             settings={"returnee_radar": {"enrich_max_players": 1}})

        assert len(_prompts(provider)) == 1

    def test_the_prompt_names_the_player_and_their_current_news(self):
        provider = _stub_provider()

        _run(["--enrich"], scoring_data=_enrichment_scoring_data(),
             provider_factory=_provider_factory(provider))

        sidelined = next(p for p in _prompts(provider) if "Sidelined" in p)
        assert "Liverpool" in sidelined
        assert "Unknown return date" in sidelined


class TestEnrichmentAttachment:
    def test_enriched_intel_sits_beside_the_fpl_date_and_never_over_it(self):
        provider = _stub_provider({"Stalenews": _intel(
            _deadline(4).date().isoformat(),
            summary="Back in team training",
            citations=["https://example.invalid/report"],
        )})

        payload = _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                            provider_factory=_provider_factory(provider))

        stale = _entries_by_name(payload)["Stalenews"]
        assert stale["expected_return"] == _deadline(6).date().isoformat()
        assert stale["return_source"] == "fpl-news"
        intel = stale["enrichment"]
        assert intel["source"] == "ai-search"
        assert intel["expected_return"] == _deadline(4).date().isoformat()
        assert intel["return_gameweek"] == 4
        assert intel["summary"] == "Back in team training"
        assert intel["citations"] == ["https://example.invalid/report"]
        assert intel["cited"] is True

    def test_an_unenriched_entry_carries_no_intel(self):
        provider = _stub_provider()

        payload = _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                            provider_factory=_provider_factory(provider))

        assert _entries_by_name(payload)["Duesoon"]["enrichment"] is None

    def test_metadata_counts_the_entries_that_gained_intel(self):
        provider = _stub_provider()

        payload = _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                            provider_factory=_provider_factory(provider))

        metadata = payload["metadata"]
        assert metadata["enrichment_requested"] is True
        assert metadata["enrichment_available"] is True
        assert metadata["enrichment_note"] is None
        assert metadata["enrichment_count"] == 2

    def test_a_response_that_states_nothing_attaches_nothing(self):
        junk = LLMResponse(content="I could not find any update.", model="sonar-pro",
                           usage=TokenUsage(input_tokens=1, output_tokens=1))
        provider = _stub_provider(default=junk)

        payload = _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                            provider_factory=_provider_factory(provider))

        assert _entries_by_name(payload)["Sidelined"]["enrichment"] is None
        assert payload["metadata"]["enrichment_count"] == 0


class TestEscalationGate:
    """R16: an enriched date decides an irreversible action only when cited."""

    def test_an_fpl_stated_date_inside_the_window_needs_no_citation(self):
        payload = _json_run()

        due_soon = _entries_by_name(payload)["Duesoon"]
        assert due_soon["return_gameweek"] == 4
        assert due_soon["escalation_eligible"] is True
        assert due_soon["escalation_basis"] == "fpl-news"

    def test_a_date_unknown_entry_never_escalates_on_its_own(self):
        payload = _json_run()

        sidelined = _entries_by_name(payload)["Sidelined"]
        assert sidelined["escalation_eligible"] is False
        assert sidelined["escalation_basis"] is None

    def test_an_uncited_enriched_date_renders_but_does_not_escalate(self):
        provider = _stub_provider({"Sidelined": _intel(
            _deadline(4).date().isoformat(), citations=[],
        )})

        payload = _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                            provider_factory=_provider_factory(provider))

        sidelined = _entries_by_name(payload)["Sidelined"]
        assert sidelined["enrichment"]["expected_return"] == _deadline(4).date().isoformat()
        assert sidelined["enrichment"]["cited"] is False
        assert sidelined["escalation_eligible"] is False
        assert sidelined["escalation_basis"] is None

    def test_a_cited_enriched_date_escalates_and_names_its_provenance(self):
        provider = _stub_provider({"Sidelined": _intel(
            _deadline(4).date().isoformat(),
            citations=["https://example.invalid/presser"],
        )})

        payload = _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                            provider_factory=_provider_factory(provider))

        sidelined = _entries_by_name(payload)["Sidelined"]
        assert sidelined["escalation_eligible"] is True
        assert sidelined["escalation_basis"] == "ai-search"
        assert sidelined["enrichment"]["citations"] == ["https://example.invalid/presser"]

    def test_a_cited_date_beyond_the_escalation_window_does_not_escalate(self):
        provider = _stub_provider({"Sidelined": _intel(
            _deadline(8).date().isoformat(),
            citations=["https://example.invalid/presser"],
        )})

        payload = _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                            provider_factory=_provider_factory(provider))

        sidelined = _entries_by_name(payload)["Sidelined"]
        assert sidelined["enrichment"]["return_gameweek"] == 8
        assert sidelined["escalation_eligible"] is False

    def test_metadata_publishes_the_escalation_window(self):
        assert _json_run()["metadata"]["escalation_window"] == 2


class TestEnrichmentCache:
    def test_a_second_run_in_the_same_gameweek_queries_nothing(self):
        provider = _stub_provider()
        factory = _provider_factory(provider)

        first = _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                          provider_factory=factory)
        queried = provider.query.await_count
        second = _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                           provider_factory=factory)

        assert queried == 2
        assert provider.query.await_count == queried
        assert second["metadata"]["enrichment_count"] == first["metadata"]["enrichment_count"]
        assert second["metadata"]["enrichment_count"] == 2

    def test_a_later_gameweek_does_not_serve_the_earlier_cache(self):
        provider = _stub_provider()
        factory = _provider_factory(provider)

        _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                  provider_factory=factory)
        queried = provider.query.await_count
        _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(next_gw_id=NEXT_GW + 1),
                  provider_factory=factory)

        assert provider.query.await_count > queried


class TestEnrichmentFailure:
    def test_a_provider_failure_degrades_to_the_deterministic_watchlist(self):
        provider = _stub_provider(default=ProviderError("Perplexity request timed out"))

        payload = _json_run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                            provider_factory=_provider_factory(provider))

        assert _names(payload), "the deterministic watchlist must survive a failed query"
        metadata = payload["metadata"]
        assert metadata["enrichment_available"] is True
        assert metadata["enrichment_count"] == 0
        assert "timed out" in metadata["enrichment_note"]

    def test_a_provider_failure_keeps_the_table_run_at_exit_zero(self):
        provider = _stub_provider(default=ProviderError("Perplexity request timed out"))

        result = _run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                      provider_factory=_provider_factory(provider))

        assert result.exit_code == 0, result.output
        assert "Sidelined" in result.output

    def test_a_failed_query_does_not_poison_the_cache(self):
        failing = _stub_provider(default=ProviderError("Perplexity request timed out"))
        _run(["--enrich"], scoring_data=_enrichment_scoring_data(),
             provider_factory=_provider_factory(failing))

        working = _stub_provider()
        _run(["--enrich"], scoring_data=_enrichment_scoring_data(),
             provider_factory=_provider_factory(working))

        assert working.query.await_count == 2


class TestEnrichmentTable:
    def test_intel_appears_in_its_own_column_beside_the_fpl_date(self):
        provider = _stub_provider({"Sidelined": _intel(
            _deadline(3).date().isoformat(),
            citations=["https://example.invalid/presser"],
        )})

        result = _run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                      provider_factory=_provider_factory(provider))

        assert result.exit_code == 0, result.output
        assert "Intel" in result.output
        # The enriched date renders on its own, without displacing the FPL cell.
        assert "28 Aug" in result.output
        assert "Unknown" in result.output

    def test_uncited_intel_is_marked_as_such(self):
        provider = _stub_provider({"Sidelined": _intel(
            _deadline(3).date().isoformat(), citations=[],
        )})

        result = _run(["--enrich"], scoring_data=_enrichment_scoring_data(),
                      provider_factory=_provider_factory(provider))

        assert "uncited" in result.output

    def test_no_intel_column_without_the_flag(self):
        result = _run(scoring_data=_enrichment_scoring_data())

        assert "Intel" not in result.output


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    @pytest.mark.parametrize("custom_analysis", [False, True])
    def test_listed_in_the_general_section_of_help(self, custom_analysis: bool):
        runner = CliRunner()
        settings = {"fpl": {}, "custom_analysis": custom_analysis}
        with patch("fpl_cli.cli._context.load_settings", return_value=settings):
            result = runner.invoke(main, ["--help"], env={"COLUMNS": "220"})

        assert result.exit_code == 0, result.output
        general = result.output.split("General Commands:")[1].split(" Commands:")[0]
        assert "returnees" in general
