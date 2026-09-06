"""Setup health check: dead IDs, stale per-team data, and directory resolution.

The failure modes this command exists for are uniformly silent (#57): a
recycled draft entry ID resolves to a stranger's team, a classic entry or
league ID from last season resolves to somebody else's (FPL reissues both from
a sequence that restarts each July), a per-team file rebuilt in August happily
describes last season's twenty clubs, and quality scores keep normalising
against calibrated anchors measured on a cohort that has since turned over.
None of them error -- they produce plausible output. Every check here reports
the identity it resolved (team name, league name, season) so a wrong-but-valid
value is visible, and distinguishes "broken, fix this" from "stale, will
self-correct".
"""
# Pattern: direct-api

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import click
import httpx
import yaml
from rich.markup import escape as rich_escape
from rich.panel import Panel

from fpl_cli.cli._context import console, fpl_config, load_settings
from fpl_cli.cli._json import emit_json, output_format_option
from fpl_cli.paths import (
    SHIPPED_CONFIG_DIR,
    UserDirError,
    user_cache_dir,
    user_config_dir,
    user_config_file,
    user_data_dir,
    user_data_file,
)
from fpl_cli.season import get_season_year, previous_season_label, season_label
from fpl_cli.utils.teams import describe_team_set_mismatch


class CheckStatus(StrEnum):
    OK = "ok"
    BROKEN = "broken"  # wrong answers now; needs a manual fix
    STALE = "stale"  # self-corrects, or needs one routine refresh
    SKIPPED = "skipped"  # not configured / not present
    UNCHECKED = "unchecked"  # the data needed to check was unreachable


@dataclasses.dataclass
class CheckResult:
    name: str
    status: CheckStatus
    detail: str
    fix: str | None = None


_STATUS_ICONS: dict[CheckStatus, tuple[str, str]] = {
    CheckStatus.OK: ("✓", "green"),
    CheckStatus.BROKEN: ("✗", "red"),
    CheckStatus.STALE: ("⚠", "yellow"),
    CheckStatus.SKIPPED: ("−", "dim"),
    CheckStatus.UNCHECKED: ("?", "yellow"),
}


def _season_of_timestamp(value: Any) -> str | None:
    """Season label a timestamp falls in, or None when it cannot be parsed."""
    if isinstance(value, datetime):
        return season_label(get_season_year(value.date()))
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return season_label(get_season_year(parsed.date()))


def _manager_name(data: dict[str, Any]) -> str:
    return " ".join(
        part for part in (data.get("player_first_name"), data.get("player_last_name")) if part
    )


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _environment_checks() -> list[CheckResult]:
    """Report where config/data/cache resolved and whether overrides are in effect."""
    results: list[CheckResult] = []
    for label, env_var, resolver in (
        ("config dir", "FPL_CLI_CONFIG_DIR", user_config_dir),
        ("data dir", "FPL_CLI_DATA_DIR", user_data_dir),
        ("cache dir", "FPL_CLI_CACHE_DIR", user_cache_dir),
    ):
        overridden = bool(os.environ.get(env_var))
        try:
            path = resolver()
        except UserDirError as exc:
            results.append(CheckResult(label, CheckStatus.BROKEN, str(exc)))
            continue
        source = f"{env_var} override" if overridden else "platform default"
        results.append(CheckResult(label, CheckStatus.OK, f"{path} ({source})"))

    try:
        settings_file = user_config_file("settings.yaml")
    except UserDirError:
        return results  # already reported as the config dir failure above
    if settings_file.exists():
        results.append(CheckResult("settings.yaml", CheckStatus.OK, str(settings_file)))
    else:
        results.append(
            CheckResult(
                "settings.yaml",
                CheckStatus.STALE,
                "not found — running on shipped defaults only",
                "run `fpl init`",
            )
        )
    return results


# ---------------------------------------------------------------------------
# IDs in settings.yaml
# ---------------------------------------------------------------------------


async def _resolve(
    name: str,
    lookup: Awaitable[dict[str, Any]],
    missing: str,
    fix: str | None,
) -> tuple[dict[str, Any] | None, CheckResult | None]:
    """Await one API lookup, mapping HTTP failures to a CheckResult."""
    try:
        return await lookup, None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None, CheckResult(name, CheckStatus.BROKEN, missing, fix)
        return None, CheckResult(
            name,
            CheckStatus.UNCHECKED,
            f"the API returned HTTP {exc.response.status_code} — could not check",
        )
    except httpx.HTTPError as exc:
        return None, CheckResult(
            name,
            CheckStatus.UNCHECKED,
            f"could not reach the API ({exc.__class__.__name__}) — could not check",
        )


_CLASSIC_ENTRY_FIX = (
    "update classic_entry_id in settings.yaml — classic entry IDs are reissued "
    "each season (or run `fpl init`)"
)

# `fpl init` prompts only for draft_entry_id and derives the league ID from it
# (`_fetch_draft_league_id`), so the league hint leads with the command rather
# than with the manual edit. Both hints appear twice below -- the 404 branch and
# the membership/season branch -- and shared constants stop the pairs drifting.
_DRAFT_LEAGUE_FIX = (
    "run `fpl init` — it derives draft_league_id from your draft_entry_id "
    "(or update settings.yaml by hand; draft league IDs change each season)"
)

_DRAFT_ENTRY_FIX = (
    "update draft_entry_id in settings.yaml — draft entry IDs are reissued "
    "each season (or run `fpl init`)"
)


def _classic_league_ids(entry: dict[str, Any]) -> set[int]:
    """IDs of every classic league the entry payload says it plays in."""
    leagues = (entry.get("leagues") or {}).get("classic") or []
    return {
        league["id"]
        for league in leagues
        if isinstance(league, dict) and isinstance(league.get("id"), int)
    }


def _membership_gap(
    league_id_setting: str, noun: str, league_id: int | None, membership: object
) -> str:
    if not league_id:
        return f"{league_id_setting} is not set"
    if not membership:
        return f"the entry listed no {noun} leagues"
    return f"{league_id_setting} failed its own check"


async def _classic_entry_check(
    client: Any, entry_id: int, league_id: int | None, league_verified: bool
) -> CheckResult:
    name = "classic_entry_id"
    entry, failure = await _resolve(
        name,
        client.get_manager_entry(entry_id),
        f"{entry_id} does not resolve — no FPL team has this entry ID",
        _CLASSIC_ENTRY_FIX,
    )
    if failure or entry is None:
        return failure or CheckResult(name, CheckStatus.UNCHECKED, "no data returned")
    team = entry.get("name") or "?"
    owner = _manager_name(entry) or "?"
    membership = _classic_league_ids(entry)
    if league_id and league_id in membership:
        # Membership comes from the entry's own `leagues.classic`, so it holds
        # even when the league lookup itself could not run.
        return CheckResult(
            name,
            CheckStatus.OK,
            f'{entry_id} → "{team}" ({owner}), in classic league {league_id} — '
            "check the name is yours",
        )
    if league_id and league_verified and membership:
        # The classic half of #57: a reissued entry ID resolves to a live team
        # that belongs to someone else, so resolution proves nothing and only
        # membership can fail it. Sound only when the league itself checked out
        # -- otherwise the stale ID may be the league's, and this would condemn
        # a correct entry. An empty membership set means the payload changed
        # shape, not that the entry left the league, so it never condemns.
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f'{entry_id} → "{team}" ({owner}), which is not in classic league {league_id} — '
            "likely a reissued ID pointing at someone else's team",
            _CLASSIC_ENTRY_FIX,
        )
    return CheckResult(
        name,
        CheckStatus.OK,
        f'{entry_id} → "{team}" ({owner}) — league membership not checked '
        f"({_membership_gap('classic_league_id', 'classic', league_id, membership)})",
    )


async def _classic_league_check(client: Any, league_id: int) -> CheckResult:
    name = "classic_league_id"
    data, failure = await _resolve(
        name,
        client.get_classic_league_standings(league_id),
        f"{league_id} does not resolve — no classic league has this ID",
        "update classic_league_id in settings.yaml (or run `fpl init`)",
    )
    if failure or data is None:
        return failure or CheckResult(name, CheckStatus.UNCHECKED, "no data returned")
    league = data.get("league") or {}
    league_name = league.get("name") or "?"
    # No season assertion, but not because the ID is stable: classic league
    # IDs come from a sequence that restarts each July, so `created` always
    # lands in the current season. Last season's ID does not go dead -- it
    # resolves to a *different* league created weeks ago, which is why the
    # stamp is worthless as a staleness signal here while `draft_dt` works for
    # draft. What proves the ID is still yours is `_classic_entry_check`
    # finding this league in the entry's own `leagues.classic`.
    return CheckResult(
        name, CheckStatus.OK, f'{league_id} → "{league_name}" — check the name is yours'
    )


async def _draft_league_check(draft_client: Any, league_id: int) -> CheckResult:
    name = "draft_league_id"
    data, failure = await _resolve(
        name,
        draft_client.get_league_details(league_id),
        f"{league_id} does not resolve — no draft league has this ID",
        _DRAFT_LEAGUE_FIX,
    )
    if failure or data is None:
        return failure or CheckResult(name, CheckStatus.UNCHECKED, "no data returned")
    league = data.get("league") or {}
    league_name = league.get("name") or "?"
    draft_season = _season_of_timestamp(league.get("draft_dt"))
    if draft_season and draft_season != season_label():
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f'{league_id} → "{league_name}", drafted in {draft_season} — '
            "not this season's league",
            _DRAFT_LEAGUE_FIX,
        )
    return CheckResult(name, CheckStatus.OK, f'{league_id} → "{league_name}"')


async def _draft_entry_check(
    draft_client: Any, entry_id: int, league_id: int | None, league_verified: bool
) -> CheckResult:
    name = "draft_entry_id"
    data, failure = await _resolve(
        name,
        draft_client.get_entry_profile(entry_id),
        f"{entry_id} does not resolve — no draft team has this entry ID",
        _DRAFT_ENTRY_FIX,
    )
    if failure or data is None:
        return failure or CheckResult(name, CheckStatus.UNCHECKED, "no data returned")
    entry = data.get("entry") or {}
    team = entry.get("name") or "?"
    owner = _manager_name(entry) or "?"
    league_set = entry.get("league_set") or []
    if league_id and league_id in league_set:
        # Membership comes from the entry's own league_set, so it holds even
        # when the league lookup itself could not run.
        return CheckResult(
            name,
            CheckStatus.OK,
            f'{entry_id} → "{team}" ({owner}), in draft league {league_id} — '
            "check the name is yours",
        )
    if league_id and league_verified and league_set:
        # A recycled entry ID resolves fine, in a different league (#57) --
        # membership, not resolution, is what proves the ID is still yours.
        # Sound only when the league itself checked out: otherwise the stale
        # ID may be the league's, and this would condemn a correct entry. An
        # empty league_set means the payload changed shape, not that the
        # entry left the league, so it never condemns (mirrors the classic
        # check's empty-`leagues.classic` guard).
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f'{entry_id} → "{team}" ({owner}), which is not in draft league {league_id} — '
            "likely a recycled ID pointing at someone else's team",
            _DRAFT_ENTRY_FIX,
        )
    return CheckResult(
        name,
        CheckStatus.OK,
        f'{entry_id} → "{team}" ({owner}) — league membership not checked '
        f"({_membership_gap('draft_league_id', 'draft', league_id, league_set)})",
    )


# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------


def _team_ratings_check(teams: list[str] | None) -> CheckResult:
    from fpl_cli.services.team_ratings import TeamRatingsService

    name = "team_ratings.yaml"
    service = TeamRatingsService()
    if not service.config_path.exists():
        return CheckResult(
            name,
            CheckStatus.STALE,
            "not generated yet — fixture difficulty stays neutral until ratings exist",
            "run `fpl ratings update` to seed estimates from last season's prior",
        )
    try:
        if teams is not None:
            service.check_team_set(teams)
        warning = service.get_staleness_warning()
        days = service.days_since_update()
    except (yaml.YAMLError, OSError, AttributeError, TypeError, ValueError) as exc:
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"unreadable: {exc}",
            "delete the file and run `fpl ratings update`",
        )
    if warning:
        # An early-season blend that is still mostly last season is a note on a
        # healthy file, not a fault: `fpl ratings update` cannot clear it and
        # the next few gameweeks will. Reporting it STALE with no remedy is the
        # dead-end #138 was filed about, so it stays OK and carries the note.
        if service.advisory_warning():
            return CheckResult(name, CheckStatus.OK, warning.removeprefix("⚠️").strip())
        # The service already ignores or rebuilds bad ratings, so every other
        # problem it reports is the stale kind, not the broken kind.
        return CheckResult(name, CheckStatus.STALE, warning.removeprefix("⚠️").strip())
    return CheckResult(name, CheckStatus.OK, f"current season, updated {days} days ago")


def _team_managers_check(teams: list[str] | None) -> CheckResult:
    name = "team_managers.yaml"
    try:
        shipped = _read_yaml_mapping(SHIPPED_CONFIG_DIR / "team_managers.yaml")
        user_copy = _read_yaml_mapping(user_config_file("team_managers.yaml"))
    except (yaml.YAMLError, OSError) as exc:
        return CheckResult(name, CheckStatus.BROKEN, f"unreadable: {exc}")
    merged = {**shipped, **user_copy}
    if not teams:
        return CheckResult(
            name, CheckStatus.UNCHECKED, "could not fetch the live team list to compare against"
        )
    mismatch = describe_team_set_mismatch(name, merged, teams, verb="lists")
    if mismatch:
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            mismatch,
            "update the file — a copy in your config dir overrides the shipped one per club",
        )
    return CheckResult(name, CheckStatus.OK, f"covers all {len(teams)} clubs")


def _read_yaml_mapping(path: Any) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _previews_check(teams: list[str] | None) -> CheckResult:
    from fpl_cli.services.season_previews import SeasonPreviewsService, unknown_teams

    name = "previews/"
    service = SeasonPreviewsService()
    if not service.previews_path.is_dir():
        return CheckResult(
            name, CheckStatus.SKIPPED, "not present — optional; created by `fpl intel init`"
        )
    previews = service.get_previews()
    warnings = service.load_warnings
    if not previews and not warnings:
        return CheckResult(
            name, CheckStatus.SKIPPED, "no preview files yet — optional; see `fpl intel schema`"
        )
    # `not teams` and not `is None`: an empty live list would report every
    # valid preview as covering an unknown club, so it means "cannot check",
    # exactly like the managers check's helper treats an empty league.
    unknown = unknown_teams(previews, set(teams)) if teams else []
    if unknown:
        # Unlike a file the loader skipped, these load and count toward the
        # coverage gate, so a leftover relegated-club file can flip full-use
        # on for a set that does not really cover the league.
        detail = f"covers {', '.join(unknown)}, not in the league this season"
        if warnings:
            detail += f"; {len(warnings)} other file(s) skipped"
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            detail,
            "fix or remove the file — `fpl intel` names every problem",
        )
    if warnings:
        return CheckResult(
            name,
            CheckStatus.STALE,
            f"{len(warnings)} file(s) skipped and not influencing decisions",
            "run `fpl intel` for the reasons; re-ingest for the current season",
        )
    if not teams:
        return CheckResult(
            name, CheckStatus.UNCHECKED, "could not fetch the live team list to compare against"
        )
    return CheckResult(name, CheckStatus.OK, f"{len(previews)} of {len(teams)} clubs covered")


def _team_finances_check() -> CheckResult:
    name = "team_finances.json"
    path = user_data_file(name)
    if not path.exists():
        return CheckResult(
            name, CheckStatus.SKIPPED, "not present — created by `fpl squad sell-prices --refresh`"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        scraped_at = datetime.fromisoformat(str(data.get("scraped_at", "")))
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"unreadable: {exc}",
            "re-scrape with `fpl squad sell-prices --refresh`",
        )
    except (ValueError, AttributeError):
        return CheckResult(
            name,
            CheckStatus.STALE,
            "has no readable scraped_at stamp",
            "re-scrape with `fpl squad sell-prices --refresh`",
        )
    file_season = season_label(get_season_year(scraped_at.date()))
    if file_season != season_label():
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"scraped {scraped_at.date().isoformat()} ({file_season}) — "
            "a previous season's squad and prices",
            "re-scrape with `fpl squad sell-prices --refresh`",
        )
    return CheckResult(name, CheckStatus.OK, f"scraped {scraped_at.date().isoformat()}")


def _player_prior_check() -> CheckResult:
    name = "player_prior.yaml"
    path = user_data_file(name)
    if not path.exists():
        return CheckResult(name, CheckStatus.SKIPPED, "not generated yet — built on demand")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        file_season = data.get("metadata", {}).get("season")
    except (yaml.YAMLError, OSError, AttributeError) as exc:
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"unreadable: {exc}",
            "delete the file — it is rebuilt on next use",
        )
    if file_season != season_label():
        return CheckResult(
            name,
            CheckStatus.STALE,
            f"season label is {file_season!r} — ignored and rebuilt automatically on next use",
        )
    return CheckResult(name, CheckStatus.OK, f"season {file_season}")


def _returnee_snapshot_check() -> CheckResult:
    from fpl_cli.services.returnee_radar import SNAPSHOT_FILENAME

    name = SNAPSHOT_FILENAME
    path = user_data_file(name)
    if not path.exists():
        return CheckResult(
            name, CheckStatus.SKIPPED, "not present — written by `fpl returnees`"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        metadata = data.get("metadata", {})
        file_season = metadata.get("season")
        gameweek = metadata.get("gameweek")
    except (json.JSONDecodeError, OSError, AttributeError) as exc:
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"unreadable: {exc}",
            "delete the file — the next `fpl returnees` run rebuilds it",
        )
    if file_season != season_label():
        # The radar reads this itself and treats a season mismatch as a first
        # run, so the only cost is one week of missing week-over-week changes.
        return CheckResult(
            name,
            CheckStatus.STALE,
            f"season label is {file_season!r} — discarded and rebuilt on the next "
            "`fpl returnees` run",
        )
    written_for = f", written for GW{gameweek}" if isinstance(gameweek, int) else ""
    return CheckResult(name, CheckStatus.OK, f"season {file_season}{written_for}")


# ---------------------------------------------------------------------------
# Scoring calibration
# ---------------------------------------------------------------------------


def _quality_ceilings_check() -> CheckResult:
    """Report whether the calibrated quality anchors still describe a current cohort.

    The one season-derived artefact in the tool that is frozen in code rather
    than regenerated from a file, so none of the checks above can see it: the
    anchors in QUALITY_CEILINGS were measured against a completed season, and
    a July rollover with no `--write` re-run leaves them describing a cohort
    that has since turned over -- with every code-side input unchanged, so the
    fingerprint drift guard stays green and the scores merely stop meaning
    what the docs say (#128). Anchors from last season while this season runs
    is the intended steady state and reports OK: last season's is the newest
    cohort anyone can have measured.
    """
    from fpl_cli.services.scoring.constants import (
        CALIBRATION_SEASON,
        calibration_seasons_behind,
    )

    name = "quality_ceilings"
    # One read of the clock for both, so the label reported and the verdict
    # cannot straddle a July cutover that lands between two calls.
    today = date.today()
    newest_completed = previous_season_label(today)
    try:
        behind = calibration_seasons_behind(today)
    except ValueError:
        # A label the season helpers cannot parse -- only reachable by hand
        # editing the generated block, since the write path refuses one. It is
        # a finding like any other here: the command that exists to report a
        # broken setup must not be the one that dies on it.
        return CheckResult(
            name,
            CheckStatus.BROKEN,
            f"records {CALIBRATION_SEASON!r} as its calibration season, which is "
            "not a season label — the generated block has been hand-edited",
            "reinstall fplkit, or re-run scripts/calibrate_quality_ceilings.py "
            "--write to regenerate the block",
        )
    if behind < 0:
        # Only reachable from a hand-edited block or a machine whose clock is
        # a year behind. Stale rather than broken either way: a wrong clock
        # must not exit the command non-zero, and a season that has not
        # finished yet does self-correct once it does.
        return CheckResult(
            name,
            CheckStatus.STALE,
            f"calibrated against {CALIBRATION_SEASON}, which has not completed — "
            "the generated block was hand-edited, or this machine's clock is behind",
        )
    if behind:
        seasons = "season" if behind == 1 else "seasons"
        return CheckResult(
            name,
            CheckStatus.STALE,
            f"calibrated against {CALIBRATION_SEASON}, {behind} completed {seasons} "
            f"behind {newest_completed} — player quality scores are normalised "
            "against a cohort that has since turned over",
            "upgrade fplkit (`pip install -U fplkit`) for a release calibrated "
            f"against {newest_completed}; from a checkout, re-run "
            "scripts/calibrate_quality_ceilings.py --write",
        )
    return CheckResult(
        name,
        CheckStatus.OK,
        f"calibrated against {CALIBRATION_SEASON}, the newest completed season",
    )


def _file_checks(teams: list[str] | None) -> list[CheckResult]:
    """Run the data-file checks, containing an unusable FPL_CLI_* override.

    Each check resolves the config or data dir itself, so a broken override
    raising UserDirError here would otherwise abort the whole command --
    discarding the results already produced (including the directory check
    that diagnosed the override) and, under --format json, the envelope.
    """
    checks: list[tuple[str, Callable[[], CheckResult]]] = [
        ("team_ratings.yaml", lambda: _team_ratings_check(teams)),
        ("team_managers.yaml", lambda: _team_managers_check(teams)),
        ("previews/", lambda: _previews_check(teams)),
        ("team_finances.json", _team_finances_check),
        ("player_prior.yaml", _player_prior_check),
        ("returnee_snapshot.json", _returnee_snapshot_check),
    ]
    results: list[CheckResult] = []
    for name, check in checks:
        try:
            results.append(check())
        except UserDirError as exc:
            results.append(CheckResult(name, CheckStatus.BROKEN, str(exc)))
    return results


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _render_section(title: str, results: list[CheckResult]) -> None:
    console.print(f"\n[bold]{title}[/bold]")
    for result in results:
        icon, colour = _STATUS_ICONS[result.status]
        console.print(
            f"  [{colour}]{icon}[/{colour}] [bold]{rich_escape(result.name)}[/bold] — "
            f"{rich_escape(result.detail)}"
        )
        if result.fix:
            console.print(f"      [dim]fix: {rich_escape(result.fix)}[/dim]")


def _print_summary(broken: int, stale: int, unchecked: int) -> None:
    console.print()
    if broken:
        console.print(f"[red]{broken} problem(s) need attention.[/red]")
    if stale:
        console.print(
            f"[yellow]{stale} stale item(s) — will self-correct or need a routine "
            "refresh.[/yellow]"
        )
    if unchecked:
        console.print(f"[yellow]{unchecked} check(s) could not run.[/yellow]")
    if not broken and not stale and not unchecked:
        console.print("[green]Everything checks out.[/green]")


def _status_counts(results: list[CheckResult]) -> tuple[int, int, int]:
    broken = sum(1 for r in results if r.status == CheckStatus.BROKEN)
    stale = sum(1 for r in results if r.status == CheckStatus.STALE)
    unchecked = sum(1 for r in results if r.status == CheckStatus.UNCHECKED)
    return broken, stale, unchecked


@click.command("doctor")
@click.option(
    "--providers",
    "providers_only",
    is_flag=True,
    help="Probe the external data sources instead of the local setup: live shape "
    "and volume checks against the FPL and Draft APIs, the historical datasets, "
    "Understat, and football-data.org.",
)
@output_format_option
def doctor_command(providers_only: bool, output_format: str) -> None:
    """Check your FPL setup for dead IDs, stale data, and config problems.

    Verifies each ID in settings.yaml still resolves to your team and league
    this season, that per-team data files describe the current clubs, that the
    scoring scale was calibrated against the newest completed season, and
    shows which directories are in use. Problems that need a fix today are
    reported separately from stale data that corrects itself. Exits non-zero
    when something needs fixing.

    With --providers, checks the external data sources instead: that each
    still serves data of the expected shape and size, and that every club
    resolves across sources — the drift that otherwise surfaces as plausible
    but wrong output.
    """
    # Deliberately not format-gated: the point of the command is auditing
    # configuration, so every ID is reported -- an unset one shows as skipped
    # rather than being hidden by the format that unset ID implies.

    async def _run() -> None:
        from fpl_cli.api.fpl import FPLClient as _FPLClient

        if providers_only:
            from fpl_cli.cli.doctor_providers import provider_checks

            provider_results = await provider_checks()
            broken, stale, unchecked = _status_counts(provider_results)
            if output_format == "json":
                emit_json(
                    "doctor",
                    {"providers": [dataclasses.asdict(r) for r in provider_results]},
                    metadata={
                        "season": season_label(),
                        "broken": broken,
                        "stale": stale,
                        "unchecked": unchecked,
                    },
                )
            else:
                console.print(
                    Panel.fit(f"[bold blue]FPL Doctor — season {season_label()}[/bold blue]")
                )
                _render_section("Providers", provider_results)
                _print_summary(broken, stale, unchecked)
            if broken:
                raise SystemExit(1)
            return

        try:
            settings: dict[str, Any] | None = load_settings()
        except UserDirError:
            # The unusable config dir is itself the finding, reported by the
            # directories section -- keep going and check what needs no settings.
            settings = None
        fpl_cfg = fpl_config(settings or {})
        classic_entry_id = fpl_cfg.get("classic_entry_id")
        classic_league_id = fpl_cfg.get("classic_league_id")
        draft_league_id = fpl_cfg.get("draft_league_id")
        draft_entry_id = fpl_cfg.get("draft_entry_id")
        unset_detail = (
            "not set"
            if settings is not None
            else "settings could not be read — fix the config dir first"
        )

        env_results = _environment_checks()

        id_results: list[CheckResult] = []
        teams: list[str] | None
        async with _FPLClient() as client:
            try:
                teams = [t.short_name for t in await client.get_teams()]
            except httpx.HTTPError:
                teams = None

            # The league runs first because the entry's reissued-ID verdict
            # depends on it, but both rows keep their long-standing order in
            # the report.
            classic_league_result: CheckResult | None = None
            if classic_league_id:
                classic_league_result = await _classic_league_check(client, classic_league_id)
            if classic_entry_id:
                classic_league_verified = (
                    classic_league_result is not None
                    and classic_league_result.status is CheckStatus.OK
                )
                id_results.append(
                    await _classic_entry_check(
                        client, classic_entry_id, classic_league_id, classic_league_verified
                    )
                )
            else:
                id_results.append(CheckResult("classic_entry_id", CheckStatus.SKIPPED, unset_detail))
            id_results.append(
                classic_league_result
                or CheckResult("classic_league_id", CheckStatus.SKIPPED, unset_detail)
            )

        if draft_league_id or draft_entry_id:
            from fpl_cli.api.fpl_draft import FPLDraftClient as _FPLDraftClient

            async with _FPLDraftClient() as draft_client:
                league_result: CheckResult | None = None
                if draft_league_id:
                    league_result = await _draft_league_check(draft_client, draft_league_id)
                    id_results.append(league_result)
                else:
                    id_results.append(
                        CheckResult("draft_league_id", CheckStatus.SKIPPED, unset_detail)
                    )
                if draft_entry_id:
                    league_verified = (
                        league_result is not None and league_result.status is CheckStatus.OK
                    )
                    id_results.append(
                        await _draft_entry_check(
                            draft_client, draft_entry_id, draft_league_id, league_verified
                        )
                    )
                else:
                    id_results.append(CheckResult("draft_entry_id", CheckStatus.SKIPPED, unset_detail))
        else:
            id_results.append(CheckResult("draft_league_id", CheckStatus.SKIPPED, unset_detail))
            id_results.append(CheckResult("draft_entry_id", CheckStatus.SKIPPED, unset_detail))

        file_results = _file_checks(teams)
        calibration_results = [_quality_ceilings_check()]

        all_results = env_results + id_results + file_results + calibration_results
        broken, stale, unchecked = _status_counts(all_results)

        if output_format == "json":
            emit_json(
                "doctor",
                {
                    "environment": [dataclasses.asdict(r) for r in env_results],
                    "settings_ids": [dataclasses.asdict(r) for r in id_results],
                    "data_files": [dataclasses.asdict(r) for r in file_results],
                    "calibration": [dataclasses.asdict(r) for r in calibration_results],
                },
                metadata={
                    "season": season_label(),
                    "broken": broken,
                    "stale": stale,
                    "unchecked": unchecked,
                },
            )
        else:
            console.print(Panel.fit(f"[bold blue]FPL Doctor — season {season_label()}[/bold blue]"))
            _render_section("Directories", env_results)
            _render_section("Settings IDs", id_results)
            _render_section("Data files", file_results)
            _render_section("Scoring calibration", calibration_results)
            _print_summary(broken, stale, unchecked)

        if broken:
            raise SystemExit(1)

    asyncio.run(_run())
