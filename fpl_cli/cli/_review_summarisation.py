"""Review LLM summarisation and recommendation comparison."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml
from rich.markup import escape as rich_escape

from fpl_cli.cli._context import console, error_console
from fpl_cli.cli._fines import FinesLeagueData, FinesTeamPlayer, compute_bench_analysis, evaluate_fines
from fpl_cli.cli._fines_config import parse_fines_config
from fpl_cli.cli._helpers import _gw_position_with_half
from fpl_cli.cli._review_analysis import GlobalReviewData
from fpl_cli.cli._review_classic import _format_review_classic_player
from fpl_cli.cli._review_draft import _format_review_draft_player
from fpl_cli.models.player import Player
from fpl_cli.models.team import Team
from fpl_cli.paths import user_config_dir
from fpl_cli.utils.text import strip_diacritics

# Goals/assists strings: "Damsgaard (BRE), Thiago x2 (BRE)".
# Bonus: "Bruno (NEW, 3)". Red cards / own goals: "Player (XYZ)" (with optional
# " ⚠️ YOUR PLAYER" suffix). Match the trailing club code in either shape.
_NAME_FROM_FIXTURE_RE = re.compile(r"([^,(]+?)(?:\s+x\d+)?\s*\([A-Z]{3}(?:,\s*\d+)?\)")


def _names_from_fixture_strings(parts: list[str | None]) -> set[str]:
    """Extract player names from concatenated fixture goal/assist strings."""
    names: set[str] = set()
    for part in parts:
        if not part:
            continue
        for match in _NAME_FROM_FIXTURE_RE.finditer(part):
            name = match.group(1).strip()
            if name:
                names.add(name)
    return names


def _build_research_allowlists(
    global_data: GlobalReviewData,
    fixtures_data: list[dict[str, Any]],
    team_points_data: list[dict[str, Any]],
    draft_squad_points_data: list[dict[str, Any]],
) -> tuple[set[str] | None, set[str]]:
    """Build the table allowlist and the (broader) prose allowlist.

    - Table allowlist: Dream Team ∪ Blankers (what the table prompt allows).
    - Prose allowlist: table allowlist ∪ goalscorers/assisters in the GW results
      ∪ players in the user's classic & draft squads.

    The table allowlist matches the existing behaviour (None when both source
    lists are empty, so name validation is skipped). The prose allowlist is
    always built — it's only consumed when prose validation runs.
    """
    blankers_raw = global_data.get("blankers") or []
    dream_team_raw = global_data.get("dream_team") or []

    table_allowlist: set[str] | None = None
    if blankers_raw or dream_team_raw:
        table_allowlist = (
            {p["name"] for p in blankers_raw if p.get("name")}
            | {p["name"] for p in dream_team_raw if p.get("name")}
        )

    fixture_names = _names_from_fixture_strings(
        [f.get("goals") for f in fixtures_data]
        + [f.get("assists") for f in fixtures_data]
        + [f.get("bonus") for f in fixtures_data]
        + [f.get("red_cards") for f in fixtures_data]
        + [f.get("own_goals") for f in fixtures_data]
    )
    squad_names = (
        {p["name"] for p in team_points_data if p.get("name")}
        | {p["name"] for p in draft_squad_points_data if p.get("name")}
    )
    prose_allowlist = (table_allowlist or set()) | fixture_names | squad_names
    return table_allowlist, prose_allowlist


def _format_research_context(
    global_data: GlobalReviewData,
    collected_data: dict[str, Any],
    teams: dict[int, Team] | None = None,
) -> dict[str, str]:
    """Format context strings for the research prompt."""
    dream_team_str = ""
    dream_team = global_data.get("dream_team")
    if dream_team:
        dream_team_lines = ["| Player | Team | Pos | Pts |", "|--------|------|-----|-----|"]
        for p in dream_team:
            dream_team_lines.append(
                f"| {p['name']} | {p['team']} | {p['position']} | {p['points']} |"
            )
        dream_team_str = "\n".join(dream_team_lines)

    blankers_str = ""
    blankers = global_data.get("blankers")
    if blankers:
        blankers_lines = [
            "| Player | Team | Pos | Ownership | Pts |",
            "|--------|------|-----|-----------|-----|",
        ]
        for b in blankers:
            blankers_lines.append(
                f"| {b['name']} | {b['team']} | {b.get('position', '???')} | {b['ownership']:.1f}% | {b['points']} |"
            )
        blankers_str = "\n".join(blankers_lines)

    fixtures_data = collected_data.get("fixtures", [])
    match_results_str = ""
    if fixtures_data:
        total_goals = sum(
            (f.get("home_score") or 0) + (f.get("away_score") or 0) for f in fixtures_data
        )
        match_lines = [
            f"Summary: {len(fixtures_data)} fixtures, {total_goals} total goals "
            f"(use these exact counts - do not fabricate alternatives)."
        ]
        for f in fixtures_data:
            match_lines.append(
                f"{f['home_team']} {f['home_score']}-{f['away_score']} {f['away_team']}"
            )
            if f.get("goals"):
                match_lines.append(f"  Goals: {f['goals']}")
            if f.get("assists"):
                match_lines.append(f"  Assists: {f['assists']}")
        match_results_str = "\n".join(match_lines)

    manager_context_str = ""
    managers_path = user_config_dir() / "team_managers.yaml"
    if managers_path.exists():
        managers = yaml.safe_load(managers_path.read_text(encoding="utf-8")) or {}
        manager_context_str = "Current PL managers: " + ", ".join(
            f"{code}: {name}" for code, name in sorted(managers.items())
        )

    bgw_teams_str = ", ".join(sorted(global_data.get("bgw_team_names", set())))
    dgw_teams_str = ", ".join(sorted(global_data.get("dgw_team_names", set())))

    team_glossary_str = ""
    if teams:
        glossary_entries = sorted(
            (t.short_name, t.name) for t in teams.values()
        )
        team_glossary_str = ", ".join(f"{code} = {name}" for code, name in glossary_entries)

    # Format predicted future DGWs (already filtered to min_gw=gw+1 at fetch time)
    predicted_dgws = global_data.get("predicted_dgw_teams", [])
    predicted_dgw_lines = []
    for pred in predicted_dgws:
        teams_str = ", ".join(pred.teams)
        predicted_dgw_lines.append(f"GW{pred.gameweek}: {teams_str} ({pred.confidence.value} confidence)")
    predicted_dgw_str = "\n".join(predicted_dgw_lines)

    return {
        "dream_team": dream_team_str,
        "blankers": blankers_str,
        "match_results": match_results_str,
        "manager_context": manager_context_str,
        "bgw_teams": bgw_teams_str,
        "dgw_teams": dgw_teams_str,
        "predicted_dgw_teams": predicted_dgw_str,
        "team_glossary": team_glossary_str,
    }


def _format_classic_section(
    team_points_data: list[dict[str, Any]],
    automatic_subs: list[dict[str, Any]],
    player_map: dict[int, Player],
    classic_transfers_data: list[dict[str, Any]],
    active_chip: str | None = None,
) -> dict[str, str]:
    """Format classic team data for the synthesis prompt."""
    if team_points_data:
        starters = [p for p in team_points_data if p.get("contributed", True) or p.get("auto_sub_in")]
        bench = [p for p in team_points_data if not p.get("contributed", True) and not p.get("auto_sub_in")]
        classic_players_str = "### Starting XI\n" + "\n".join(_format_review_classic_player(p) for p in starters)
        if bench:
            classic_players_str += "\n### Bench\n" + "\n".join(_format_review_classic_player(p) for p in bench)
    else:
        classic_players_str = "No data"

    if automatic_subs:
        sub_details = []
        for sub in automatic_subs:
            in_player = player_map.get(sub["element_in"])
            out_player = player_map.get(sub["element_out"])
            if in_player and out_player:
                in_data = next((p for p in team_points_data if p["name"] == in_player.web_name), None)
                in_pts = in_data["points"] if in_data else 0
                sub_details.append(f"{in_player.web_name} on for {out_player.web_name} ({in_pts} pts)")
        if sub_details:
            suffix = (
                " (no points impact: Bench Boost active, all 15 players already scored)"
                if active_chip == "bboost" else ""
            )
            classic_players_str += f"\n\nAuto-subs: {', '.join(sub_details)}{suffix}"

    classic_bench = compute_bench_analysis(team_points_data) if team_points_data else None
    if classic_bench:
        classic_players_str += f"\n\nBench vs Starters (formation-valid swaps):\n{classic_bench}"

    classic_transfers_str = "\n".join([
        f"- {t['player_out']} ({t['player_out_points']} pts) → {t['player_in']}"
        f" ({t['player_in_points']} pts) = {'+' if t['net'] > 0 else ''}{t['net']} ({t['verdict']})"
        for t in classic_transfers_data
    ]) if classic_transfers_data else "No transfers this week"

    return {
        "players": classic_players_str,
        "transfers": classic_transfers_str,
    }


def _format_draft_section(
    draft_squad_points_data: list[dict[str, Any]],
    draft_automatic_subs: list[dict[str, Any]],
    draft_player_map: dict[int, dict[str, Any]],
    draft_transactions: list[dict[str, Any]],
) -> dict[str, str]:
    """Format draft squad data for the synthesis prompt."""
    draft_players_str = "\n".join([
        _format_review_draft_player(p) for p in draft_squad_points_data
    ]) if draft_squad_points_data else "No data"

    if draft_automatic_subs:
        sub_details = []
        for sub in draft_automatic_subs:
            in_player = draft_player_map.get(sub["element_in"])
            out_player = draft_player_map.get(sub["element_out"])
            if in_player and out_player:
                in_data = next(
                    (p for p in draft_squad_points_data
                     if p["id"] == sub["element_in"]),
                    None
                )
                in_pts = in_data["points"] if in_data else 0
                sub_details.append(
                    f"{in_player.get('web_name', 'Unknown')} on for "
                    f"{out_player.get('web_name', 'Unknown')} ({in_pts} pts)"
                )
        if sub_details:
            draft_players_str += f"\n\nAuto-subs: {', '.join(sub_details)}"

    draft_bench = compute_bench_analysis(draft_squad_points_data) if draft_squad_points_data else None
    if draft_bench:
        draft_players_str += f"\n\nBench vs Starters (formation-valid swaps):\n{draft_bench}"

    draft_transactions_str = "\n".join([
        f"- {t['player_out'] or 'Free agent'} ({t['player_out_points'] or 0} pts)"
        f" → {t['player_in']} ({t['player_in_points']} pts)"
        f" = {'+' if t['net'] > 0 else ''}{t['net']} ({t['verdict']})"
        for t in draft_transactions
    ]) if draft_transactions else "No waivers this week"

    return {
        "players": draft_players_str,
        "transactions": draft_transactions_str,
    }


def _format_league_context(
    classic_league_data: dict[str, Any] | None,
    draft_league_data: dict[str, Any] | None,
    team_points_data: list[dict[str, Any]],
    draft_squad_points_data: list[dict[str, Any]],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Format league context for the synthesis prompt."""
    classic_rivals_str = ""
    if classic_league_data and classic_league_data.get("nearby_rivals"):
        lines = []
        for r in classic_league_data["nearby_rivals"][:5]:
            name = "You" if r.get("is_user") else r.get("manager_name", "Unknown")
            lines.append(f"- {r.get('rank', '?')}. {name}: {r.get('total', 0):,} pts")
        classic_rivals_str = "\n".join(lines)

    classic_worst_performers_str = ""
    if classic_league_data and classic_league_data.get("worst_performers"):
        lines = []
        for p in classic_league_data["worst_performers"]:
            rank = p.get("rank_str", "?")
            name = "You" if p.get("is_user") else p.get("name", "Unknown")
            gross = p.get("gross_points", 0)
            cost = p.get("transfer_cost", 0)
            net = p.get("net_points", gross)
            if cost > 0:
                lines.append(f"{rank}. {name} - {net} net pts ({gross} gross, -{cost} hit)")
            else:
                lines.append(f"{rank}. {name} - {net} pts")
        classic_worst_performers_str = "\n".join(lines)

    classic_transfer_impact_str = classic_league_data.get("transfer_impact") if classic_league_data else None

    draft_worst_performers_str = ""
    if draft_league_data and draft_league_data.get("worst_performers"):
        lines = []
        for p in draft_league_data["worst_performers"]:
            rank = p.get("rank_str", "?")
            name = "You" if p.get("is_user") else p.get("name", "Unknown")
            pts = p.get("points", 0)
            lines.append(f"{rank}. {name} - {pts} pts")
        draft_worst_performers_str = "\n".join(lines)

    captain_pick = next((p for p in team_points_data if p.get("is_captain")), None)
    captain_name = captain_pick["name"] if captain_pick else "Unknown"
    if captain_pick:
        multiplier = 3 if captain_pick.get("is_triple_captain") else 2
        raw = captain_pick["points"]
        displayed = captain_pick["display_points"]
        captain_label = f"{captain_name} ({displayed} pts = {raw} raw × {multiplier})"
    else:
        multiplier = 2
        raw = 0
        captain_label = "Unknown (0 pts)"
    captain_points = captain_pick["display_points"] if captain_pick else 0

    # Hindsight-best captain: highest RAW scorer among players who could have
    # been captained pre-deadline. Compare raw-to-raw so the LLM stops assessing
    # captain choice against already-doubled totals.
    #
    # If the captain didn't play, the vice was auto-promoted and already got
    # the multiplier — baseline off the vice's raw score (the effective armband
    # holder), not the captain's 0, otherwise the swing is inflated.
    captain_hindsight = "N/A"
    if captain_pick and team_points_data:
        captain_contributed = captain_pick.get("contributed", True)
        if captain_contributed:
            baseline_raw = raw
            baseline_name = captain_name
            baseline_note = ""
        else:
            vice = next(
                (p for p in team_points_data if p.get("is_vice_active")),
                None,
            )
            if vice and vice.get("contributed", True):
                baseline_raw = vice["points"]
                baseline_name = vice["name"]
                baseline_note = (
                    f" (captain {captain_name} didn't play; vice {baseline_name} "
                    f"auto-got the armband)"
                )
            else:
                baseline_raw = None
                baseline_name = captain_name
                baseline_note = ""

        if baseline_raw is None:
            captain_hindsight = (
                f"{captain_name} didn't play and no vice took the armband — "
                f"no raw baseline to evaluate captain choice"
            )
        else:
            contributed_players = [
                p for p in team_points_data
                if (p.get("contributed", True) or p.get("auto_sub_in"))
                and not p.get("is_captain")
                and not p.get("is_vice_active")
            ]
            best_alt = max(contributed_players, key=lambda p: p["points"], default=None)
            if best_alt and best_alt["points"] > baseline_raw:
                # Swapping the armband only changes the *extra* multiplier on
                # each player's raw — the baseline still scores their raw
                # uncaptained, the alt is already counted once. Net swing is
                # (alt - baseline) × (m - 1), not × m.
                delta = (best_alt["points"] - baseline_raw) * (multiplier - 1)
                captain_hindsight = (
                    f"{best_alt['name']} would have been the optimal captain "
                    f"({best_alt['points']} raw vs {baseline_raw} raw for "
                    f"{baseline_name}; swapping the ×{multiplier} armband "
                    f"would have netted +{delta} pts){baseline_note}"
                )
            else:
                captain_hindsight = (
                    f"{baseline_name} was the optimal captain "
                    f"(highest raw score among contributors){baseline_note}"
                )

    fines_config = parse_fines_config(settings)
    fine_results_str = ""
    escalation_note: str | None = None
    if fines_config:
        escalation_note = fines_config.escalation_note
        fine_parts: list[str] = []
        if fines_config.classic:
            classic_league_name = (
                classic_league_data.get("league_name", "Classic League") if classic_league_data else "Classic League"
            )
            fine_parts.append(f"## Classic ({classic_league_name})")
            results = evaluate_fines(
                fines_config, "classic",
                cast(FinesLeagueData | None, classic_league_data),
                cast(list[FinesTeamPlayer], team_points_data),
                use_net_points=settings.get("use_net_points", False),
            )
            any_triggered = any(r.triggered for r in results)
            for r in results:
                fine_parts.append(f"- {r.message}")
            if not any_triggered:
                fine_parts.append("- No fines this week.")
            fine_parts.append("")
        if fines_config.draft:
            fine_parts.append("## Draft League")
            results = evaluate_fines(
                fines_config, "draft",
                cast(FinesLeagueData | None, draft_league_data),
                cast(list[FinesTeamPlayer], draft_squad_points_data),
            )
            any_triggered = any(r.triggered for r in results)
            for r in results:
                fine_parts.append(f"- {r.message}")
            if not any_triggered:
                fine_parts.append("- No fines this week.")
        fine_results_str = "\n".join(fine_parts)

    return {
        "classic_rivals": classic_rivals_str,
        "classic_worst_performers": classic_worst_performers_str,
        "classic_transfer_impact": classic_transfer_impact_str,
        "draft_worst_performers": draft_worst_performers_str,
        "captain_label": captain_label,
        "captain_points": captain_points,
        "captain_hindsight": captain_hindsight,
        "fine_results": fine_results_str,
        "escalation_note": escalation_note,
    }


async def _review_llm_summarise(
    *,
    gw: int,
    gw_data: dict[str, Any],
    collected_data: dict[str, Any],
    classic_team: dict[str, Any],
    classic_transfers_data: list[dict[str, Any]],
    classic_league_data: dict[str, Any] | None,
    draft_result: dict[str, Any],
    global_data: GlobalReviewData,
    player_map: dict[int, Player],
    teams: dict[int, Team],
    settings: dict[str, Any],
    dry_run: bool,
    debug: bool,
    research_provider: Any,
    synthesis_provider: Any,
) -> dict[str, Any]:
    """Run LLM summarisation (research + synthesis). Returns {research_summary, synthesis_summary}."""
    from fpl_cli.prompts.review import (
        REVIEW_RESEARCH_SYSTEM_PROMPT,
        get_review_research_prompt,
        get_review_synthesis_prompt,
        validate_research_prose,
        validate_research_teams,
    )

    if not dry_run and research_provider is None:
        raise ValueError("research_provider must be provided when dry_run=False")
    if not dry_run and synthesis_provider is None:
        raise ValueError("synthesis_provider must be provided when dry_run=False")

    # Unpack classic_team bundle
    my_entry_summary = classic_team["my_entry_summary"]
    team_points_data = classic_team["team_points_data"]
    automatic_subs = classic_team["automatic_subs"]
    active_chip = classic_team["active_chip"]
    # my_picks_data not needed here

    # Unpack draft_result bundle
    draft_league_data = draft_result["draft_league_data"]
    draft_league_name = draft_result["draft_league_name"]
    draft_squad_points_data = draft_result["draft_squad_points_data"]
    draft_automatic_subs = draft_result["draft_automatic_subs"]
    draft_player_map = draft_result["draft_player_map"]

    research_summary = None
    synthesis_summary = None
    table_corrections = 0
    prose_corrections_count = 0
    corrections_path: str | None = None

    if dry_run:
        console.print("\n[dim]Dry run: building prompts without calling LLMs...[/dim]")
    else:
        console.print("\n[dim]Generating LLM summaries...[/dim]")

    # Setup debug directory if needed (always for dry_run)
    debug_dir = None
    if debug or dry_run:
        import os

        debug_dir = Path("data/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            debug_dir.chmod(0o700)
        console.print(f"[dim]  Debug output → {debug_dir}/[/dim]")

    # Stage 1: Research - social + journalistic narrative
    research_ctx = _format_research_context(global_data, collected_data, teams)
    research_prompt = get_review_research_prompt(
        gw,
        dream_team=research_ctx["dream_team"],
        blankers=research_ctx["blankers"],
        match_results=research_ctx["match_results"],
        manager_context=research_ctx["manager_context"],
        bgw_teams=research_ctx["bgw_teams"],
        dgw_teams=research_ctx["dgw_teams"],
        predicted_dgw_teams=research_ctx["predicted_dgw_teams"],
        team_glossary=research_ctx["team_glossary"],
    )

    if dry_run:
        # Save prompts without calling API
        console.print("[dim]  Building research prompt...[/dim]")
        if debug_dir:
            (debug_dir / "research_system.txt").write_text(REVIEW_RESEARCH_SYSTEM_PROMPT, encoding="utf-8")
            (debug_dir / "research_prompt.txt").write_text(research_prompt, encoding="utf-8")
            console.print("[dim]    → Saved research_system.txt, research_prompt.txt[/dim]")
        research_summary = "[DRY RUN - research provider not called]"
    else:
        from fpl_cli.api.providers import ProviderError

        try:
            console.print("[dim]  Fetching community narrative...[/dim]")
            research_result = await research_provider.query(
                prompt=research_prompt,
                system_prompt=REVIEW_RESEARCH_SYSTEM_PROMPT,
            )
            research_summary = research_provider.post_process(research_result.content)
            table_allowlist, prose_allowlist = _build_research_allowlists(
                global_data,
                collected_data.get("fixtures", []),
                team_points_data,
                draft_squad_points_data,
            )
            research_summary, club_corrections = validate_research_teams(
                research_summary, player_map, teams, table_allowlist=table_allowlist
            )
            research_summary, prose_corrections = validate_research_prose(
                research_summary, player_map, prose_allowlist
            )
            all_corrections = club_corrections + prose_corrections
            table_corrections = len(club_corrections)
            prose_corrections_count = len(prose_corrections)
            if all_corrections and debug and debug_dir:
                corrections_file = debug_dir / "research_corrections.txt"
                corrections_file.write_text("\n".join(all_corrections), encoding="utf-8")
                corrections_path = str(corrections_file)
                console.print(
                    f"[dim]    → Corrected {len(club_corrections)} table cell(s), scrubbed "
                    f"{len(prose_corrections)} narrative sentence(s)[/dim]"
                )
            if research_summary:
                console.print("[green]  ✓[/green] Community narrative complete")
            else:
                error_console.print("[yellow]  ⚠ Research provider returned empty response[/yellow]")
                research_summary = "Community narrative unavailable: research provider returned an empty response."

            if debug and debug_dir:
                (debug_dir / "research_system.txt").write_text(REVIEW_RESEARCH_SYSTEM_PROMPT, encoding="utf-8")
                (debug_dir / "research_prompt.txt").write_text(research_prompt, encoding="utf-8")
                (debug_dir / "research_response.txt").write_text(research_result.content, encoding="utf-8")
                console.print("[dim]    → Saved research_*.txt[/dim]")
        except ProviderError as e:
            console.print(f"[red]  ✗ Research failed: {rich_escape(str(e))}[/red]")
            research_summary = f"Community narrative unavailable: {e}"
        except Exception as e:  # noqa: BLE001 — graceful degradation
            console.print(f"[red]  ✗ Research failed: {rich_escape(str(e))}[/red]")
            research_summary = "Community narrative unavailable: research provider error."

    # Stage 2: Synthesis - personal analysis
    classic_fmt = _format_classic_section(
        team_points_data, automatic_subs, player_map, classic_transfers_data, active_chip=active_chip,
    )
    draft_fmt = _format_draft_section(
        draft_squad_points_data, draft_automatic_subs, draft_player_map,
        collected_data.get("draft_transactions", []),
    )
    league_ctx = _format_league_context(
        classic_league_data, draft_league_data, team_points_data, draft_squad_points_data, settings,
    )

    synthesis_prompts = get_review_synthesis_prompt(
        gameweek=gw,
        research_summary=research_summary or "Not available",
        classic_points=my_entry_summary["points"] if my_entry_summary else 0,
        classic_average=gw_data.get("average_entry_score", 0),
        classic_highest=gw_data.get("highest_score", 0),
        classic_gw_rank=my_entry_summary["rank"] if my_entry_summary else 0,
        classic_overall_rank=my_entry_summary["overall_rank"] if my_entry_summary else 0,
        classic_captain=league_ctx["captain_label"],
        classic_captain_points=league_ctx["captain_points"],
        classic_captain_hindsight=league_ctx["captain_hindsight"],
        classic_players=classic_fmt["players"],
        classic_transfers=classic_fmt["transfers"],
        classic_league_name=classic_league_data["league_name"] if classic_league_data else "Unknown",
        classic_gw_position=_gw_position_with_half(
            classic_league_data.get("user_gw_rank", "?"),
            classic_league_data.get("total_entries", 0),
        ) if classic_league_data else "?",
        classic_position=classic_league_data.get("user_position", 0) if classic_league_data else 0,
        classic_total=classic_league_data.get("total_entries", 0) if classic_league_data else 0,
        classic_rivals=league_ctx["classic_rivals"],
        classic_worst_performers=league_ctx["classic_worst_performers"] or "No data",
        classic_transfer_impact=league_ctx["classic_transfer_impact"],
        draft_points=draft_league_data["user_gw_points"] if draft_league_data else 0,
        draft_league_name=draft_league_name,
        draft_players=draft_fmt["players"],
        draft_transactions=draft_fmt["transactions"],
        draft_gw_position=_gw_position_with_half(
            draft_league_data["user_gw_rank"],
            draft_league_data["total_entries"],
        ) if draft_league_data else "?",
        draft_position=draft_league_data["user_position"] if draft_league_data else 0,
        draft_total=draft_league_data["total_entries"] if draft_league_data else 0,
        draft_worst_performers=league_ctx["draft_worst_performers"] or "No data",
        fine_results=league_ctx["fine_results"],
        escalation_note=league_ctx["escalation_note"],
        active_chip=active_chip,
        use_net_points=settings.get("use_net_points", False),
        dgw_teams=research_ctx["dgw_teams"],
        bgw_teams=research_ctx["bgw_teams"],
    )
    synthesis_system, synthesis_prompt = synthesis_prompts

    if dry_run:
        console.print("[dim]  Building synthesis prompt...[/dim]")
        if debug_dir:
            (debug_dir / "synthesis_system.txt").write_text(synthesis_system, encoding="utf-8")
            (debug_dir / "synthesis_prompt.txt").write_text(synthesis_prompt, encoding="utf-8")
            console.print("[dim]    → Saved synthesis_system.txt, synthesis_prompt.txt[/dim]")
        synthesis_summary = ""
        console.print("[green]  ✓[/green] Prompts saved to data/debug/")
    else:
        try:
            console.print("[dim]  Generating personal analysis...[/dim]")
            synthesis_result = await synthesis_provider.query(
                prompt=synthesis_prompt,
                system_prompt=synthesis_system,
            )
            synthesis_summary = synthesis_result.content
            console.print("[green]  ✓[/green] Personal analysis complete")

            if debug and debug_dir:
                (debug_dir / "synthesis_system.txt").write_text(synthesis_system, encoding="utf-8")
                (debug_dir / "synthesis_prompt.txt").write_text(synthesis_prompt, encoding="utf-8")
                (debug_dir / "synthesis_response.txt").write_text(synthesis_summary, encoding="utf-8")
                console.print("[dim]    → Saved synthesis_*.txt[/dim]")
        except Exception as e:  # noqa: BLE001 — graceful degradation
            console.print(f"[red]  ✗ Synthesis failed: {rich_escape(str(e))}[/red]")
            synthesis_summary = ""

    return {
        "research_summary": research_summary,
        "synthesis_summary": synthesis_summary,
        "table_corrections": table_corrections,
        "prose_corrections": prose_corrections_count,
        "corrections_path": corrections_path,
    }


def _normalise_name(name: str) -> str:
    """Normalise a player name for fuzzy matching."""
    import re
    name = strip_diacritics(name).strip().lower()
    name = re.sub(r"\s*\(.*?\)\s*$", "", name)  # strip parentheticals
    name = re.sub(r"^[a-z]\.\s*", "", name)  # strip leading initials
    return name


def _names_match(a: str, b: str) -> bool:
    return _normalise_name(a) == _normalise_name(b)


def _find_player_gw_points(name: str, team_points_data: list[dict], pts_key: str = "points") -> int | None:
    """Find a player's GW points from team_points_data by name."""
    for p in team_points_data:
        if _names_match(p.get("name", ""), name):
            return p.get(pts_key, 0)
    return None


def _review_compare_recs(recs: dict, collected_data: dict, player_map: dict, teams: dict) -> dict:
    """Compare recommendations against actuals. Returns comparison dict."""
    comparison: dict = {"classic": {}, "draft": {}}

    team_points = collected_data.get("team_points", [])
    classic_transfers = collected_data.get("classic_transfers", [])
    draft_transactions = collected_data.get("draft_transactions", [])

    # --- Classic Captain ---
    rec_captain = recs["classic"].get("captain")
    if rec_captain:
        actual_captain_entry = next(
            (p for p in team_points if p.get("is_captain")),
            None,
        )
        actual_captain = actual_captain_entry["name"] if actual_captain_entry else None
        actual_captain_pts = actual_captain_entry.get("display_points", 0) if actual_captain_entry else 0

        followed = bool(actual_captain and _names_match(rec_captain, actual_captain))
        rec_captain_pts = _find_player_gw_points(rec_captain, team_points, "display_points")
        # If rec captain not in team (was sold), try player_map
        if rec_captain_pts is None:
            for p in player_map.values():
                if _names_match(p.web_name, rec_captain):
                    rec_captain_pts = 0  # can't easily get GW points for non-squad player here
                    break

        comparison["classic"]["captain_followed"] = followed
        comparison["classic"]["rec_captain"] = rec_captain
        comparison["classic"]["actual_captain"] = actual_captain
        comparison["classic"]["actual_captain_pts"] = actual_captain_pts
        comparison["classic"]["rec_captain_pts"] = rec_captain_pts if rec_captain_pts is not None else 0
        # Delta: difference in captain points (doubled effect)
        if followed:
            comparison["classic"]["captain_pts_delta"] = 0
        else:
            comparison["classic"]["captain_pts_delta"] = (
                actual_captain_pts - (comparison["classic"]["rec_captain_pts"])
            )

    # --- Classic Transfers ---
    rec_roll = recs["classic"].get("roll_transfer", False)
    actual_roll = len(classic_transfers) == 0
    comparison["classic"]["rec_roll"] = rec_roll
    comparison["classic"]["actual_roll"] = actual_roll

    rec_transfers = recs["classic"].get("transfers", [])
    transfer_comparisons = []
    matched_actual_indices: set[int] = set()

    for rec_t in rec_transfers:
        rec_in = rec_t["in"]
        rec_out = rec_t["out"]
        # Find matching actual transfer by OUT player
        matched = False
        for i, act_t in enumerate(classic_transfers):
            if i in matched_actual_indices:
                continue
            if _names_match(act_t.get("player_out", ""), rec_out):
                matched_actual_indices.add(i)
                same_in = _names_match(act_t.get("player_in", ""), rec_in)
                transfer_comparisons.append({
                    "rec_in": rec_in,
                    "rec_out": rec_out,
                    "actual_in": act_t.get("player_in"),
                    "actual_out": act_t.get("player_out"),
                    "followed": same_in,
                    "actual_in_pts": act_t.get("player_in_points", 0),
                    "actual_out_pts": act_t.get("player_out_points", 0),
                    "actual_net": act_t.get("net", 0),
                    "actual_verdict": act_t.get("verdict", ""),
                })
                matched = True
                break
        if not matched:
            transfer_comparisons.append({
                "rec_in": rec_in,
                "rec_out": rec_out,
                "actual_in": None,
                "actual_out": None,
                "followed": False,
                "not_made": True,
            })

    # Flag actual transfers not in recommendations
    unadvised = []
    for i, act_t in enumerate(classic_transfers):
        if i not in matched_actual_indices:
            unadvised.append({
                "actual_in": act_t.get("player_in"),
                "actual_out": act_t.get("player_out"),
                "actual_in_pts": act_t.get("player_in_points", 0),
                "actual_out_pts": act_t.get("player_out_points", 0),
                "actual_net": act_t.get("net", 0),
                "actual_verdict": act_t.get("verdict", ""),
            })

    comparison["classic"]["transfers"] = transfer_comparisons
    comparison["classic"]["unadvised_transfers"] = unadvised

    # --- Draft Waivers ---
    rec_waivers = recs["draft"].get("waivers", [])
    waiver_comparisons = []
    matched_txn_indices: set[int] = set()

    for rec_w in rec_waivers:
        rec_in = rec_w["in"]
        rec_out = rec_w["out"]
        priority = rec_w["priority"]
        matched = False
        for i, act_t in enumerate(draft_transactions):
            if i in matched_txn_indices:
                continue
            act_out = act_t.get("player_out", "") or ""
            if _names_match(act_out, rec_out):
                matched_txn_indices.add(i)
                same_in = _names_match(act_t.get("player_in", ""), rec_in)
                waiver_comparisons.append({
                    "priority": priority,
                    "rec_in": rec_in,
                    "rec_out": rec_out,
                    "actual_in": act_t.get("player_in"),
                    "actual_out": act_t.get("player_out"),
                    "followed": same_in,
                    "different_replacement": not same_in,
                    "actual_in_pts": act_t.get("player_in_points", 0),
                    "actual_out_pts": act_t.get("player_out_points", 0),
                    "actual_net": act_t.get("net", 0),
                    "actual_verdict": act_t.get("verdict", ""),
                })
                matched = True
                break
        if not matched:
            waiver_comparisons.append({
                "priority": priority,
                "rec_in": rec_in,
                "rec_out": rec_out,
                "actual_in": None,
                "actual_out": None,
                "followed": False,
                "not_executed": True,
            })

    unadvised_waivers = []
    for i, act_t in enumerate(draft_transactions):
        if i not in matched_txn_indices:
            unadvised_waivers.append({
                "actual_in": act_t.get("player_in"),
                "actual_out": act_t.get("player_out"),
                "actual_in_pts": act_t.get("player_in_points", 0),
                "actual_out_pts": act_t.get("player_out_points", 0),
                "actual_net": act_t.get("net", 0),
                "actual_verdict": act_t.get("verdict", ""),
            })

    comparison["draft"]["waivers"] = waiver_comparisons
    comparison["draft"]["unadvised_waivers"] = unadvised_waivers

    return comparison
