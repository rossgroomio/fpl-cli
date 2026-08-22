"""Prompts for league-recap LLM summaries."""

from __future__ import annotations

from fpl_cli.cli._league_recap_types import LeagueRecapData

# =============================================================================
# SYNTHESIS PROMPT (Stage 2: League-wide editorial)
# =============================================================================

RECAP_SYNTHESIS_SYSTEM_PROMPT = """You are writing a gameweek recap newsletter for a Fantasy Premier League mini-league.

<context>
Your audience is every member of this league. They want entertainment first, information second. Write with personality - name names, call out embarrassing decisions, celebrate great picks.
</context>

<tone>
- Newsletter columnist voice: opinionated, fun, a bit cheeky
- Name specific managers when praising or roasting
- Reference specific decisions (captain picks, bench choices, and transfers where transfer data is provided)
- Use the data to tell a story, not just list stats
- Brief - 300-400 words max. Punchy paragraphs, not walls of text
</tone>

<rules>
- NEVER give advice or recommendations. This is a recap, not a preview
- NEVER speculate about future gameweeks
- Stick to what happened this gameweek
- If fines were triggered, make them a highlight
- The biggest bench haul is always funny - lean into it
- If a manager played a chip, that's a big narrative hook. A chip that flopped deserves mockery; a chip that paid off deserves grudging respect. When referencing chip users, treat the "Chips Played" section as the source of truth — it includes an explicit total count; use that number verbatim. Do NOT count tags in the standings table. Do not name a subset as "the X wildcards" — either name all users of that chip or none.
- When referencing captain choices, treat the "## Captains" section as the source of truth. It lists every manager grouped by their intended captain pick, with an explicit total count. Use those counts verbatim. NEVER name a captain "outlier", "dissenter", or "the manager(s) who picked Y" unless they appear under that captain in the section. If you describe N managers as picking the modal captain, it must match the section's group size for that player. Do NOT infer captain choices from the awards or standings — they are compressed and miss managers whose pick was neither the best nor the worst.
- Only reference transfers that appear explicitly in the Awards section or in a transfers note. If no transfer information is given, do not mention transfers, hits, or moves in and out at all - absence of transfer data means there is nothing to report, not licence to invent one.
- NEVER claim a manager's bench outscored their team unless bench points are strictly greater than their GW points. Use the exact numbers provided.
- NEVER alter player or manager names. Use the exact spelling provided in the data.
</rules>"""


def get_recap_synthesis_prompt(
    gw: int,
    league_name: str,
    fpl_format: str,
    awards_text: str,
    standings_text: str,
    fines_text: str,
    research_summary: str | None = None,
    *,
    captains_text: str = "",
    chips_text: str = "",
    is_bgw: bool = False,
    is_dgw: bool = False,
    season_length: int = 38,
) -> tuple[str, str]:
    """Build the synthesis prompt for league recap. Returns (system, user)."""
    sections = [
        f"# Gameweek {gw} Recap: {league_name}",
        f"Format: {fpl_format}",
        f"Season progress: GW{gw} of {season_length}",
    ]

    if is_bgw:
        sections.append("**This was a BLANK GAMEWEEK** - not all teams had fixtures. Factor this into your analysis of low scores.")
    if is_dgw:
        sections.append("**This was a DOUBLE GAMEWEEK** - some teams had two fixtures. Factor this into your analysis of high scores.")

    if fpl_format == "draft":
        sections.append("Note: Draft format has NO captaincy. Do not mention captains.")

    if fpl_format == "classic" and gw == 1:
        sections.append(
            "**No transfers were made this gameweek** - GW1 squads are built before the "
            "deadline, so the game records no transfers and no hits for anyone. Do not "
            "mention transfers, hits, or moves in and out."
        )

    sections.extend([
        "",
        "## Awards",
        awards_text,
        "",
        "## GW Standings",
        standings_text,
    ])

    if captains_text:
        sections.extend(["", "## Captains", captains_text])

    if chips_text:
        sections.extend(["", "## Chips Played", chips_text])

    if fines_text:
        sections.extend(["", "## Fines", fines_text])

    if research_summary:
        sections.extend(["", "## GW Context (from research)", research_summary])

    user_prompt = "\n".join(sections)
    user_prompt += "\n\nWrite the recap newsletter for this gameweek."

    return RECAP_SYNTHESIS_SYSTEM_PROMPT, user_prompt


# =============================================================================
# Context formatting
# =============================================================================


def format_recap_awards_context(data: LeagueRecapData) -> str:
    """Format awards into text for the LLM prompt."""
    awards = data.get("awards", {})
    lines = []

    for key in (
        "gw_winner", "gw_loser", "biggest_bench_haul",
        "best_captain", "worst_captain",
        "transfer_genius", "transfer_disaster",
        "waiver_genius", "waiver_disaster",
    ):
        award = awards.get(key)
        if award:
            label = key.replace("_", " ").title()
            lines.append(f"- **{label}:** {award['detail']}")

    return "\n".join(lines) if lines else "No notable awards."


def format_recap_standings_context(data: LeagueRecapData) -> str:
    """Format standings with movement for the LLM prompt."""
    managers = data.get("managers", [])
    if not managers:
        return "No standings data."

    is_classic = data.get("fpl_format") == "classic"
    lines = ["| Pos | Prev | Manager | GW Pts | Total |", "|-----|------|---------|--------|-------|"]
    for m in sorted(managers, key=lambda x: x.get("overall_rank", 0)):
        prev = m.get("previous_rank", "?")
        curr = m.get("overall_rank", "?")
        name = m["manager_name"]
        movement = ""
        if isinstance(prev, int) and isinstance(curr, int) and prev != curr:
            diff = prev - curr
            movement = f" (↑{diff})" if diff > 0 else f" (↓{abs(diff)})"
        chip = m.get("active_chip") if is_classic else None
        chip_tag = f" [{chip}]" if chip else ""
        lines.append(
            f"| {curr} | {prev} | {name}{chip_tag}{movement} | {m['gw_points']} | {m['total_points']} |"
        )
    return "\n".join(lines)


_CHIP_LABEL = {
    "WC": "Wildcard",
    "FH": "Free Hit",
    "BB": "Bench Boost",
    "TC": "Triple Captain",
}


def format_recap_chips_context(data: LeagueRecapData) -> str:
    """Format chip usage as an explicit roster so the narrative doesn't have to count tags.

    Empty for draft format (no chips) or when no one played a chip.
    """
    if data.get("fpl_format") != "classic":
        return ""

    managers = data.get("managers", [])
    sorted_managers = sorted(managers, key=lambda m: -m.get("gw_points", 0))
    by_chip: dict[str, list[str]] = {}
    for m in sorted_managers:
        chip = m.get("active_chip")
        if not chip:
            continue
        by_chip.setdefault(chip, []).append(f"{m['manager_name']} ({m['gw_points']} pts)")

    if not by_chip:
        return ""

    lines = []
    total = 0
    for code in ("WC", "FH", "BB", "TC"):
        users = by_chip.get(code)
        if not users:
            continue
        label = _CHIP_LABEL[code]
        lines.append(f"- **{label}** ({len(users)}): {', '.join(users)}")
        total += len(users)
    lines.insert(0, f"Total chips played this GW: {total}")
    return "\n".join(lines)


def format_recap_captains_context(data: LeagueRecapData) -> str:
    """Per-manager captain roster grouped by intended pick.

    Mirrors the chips section: prevents the synthesis LLM from hallucinating
    captain outliers when the modal pick crowds out the per-award detail.
    Suppressed for draft (no captaincy).
    """
    if data.get("fpl_format") != "classic":
        return ""

    managers = data.get("managers", [])
    if not managers:
        return ""

    by_captain: dict[str, list[tuple[str, str]]] = {}
    for m in managers:
        captain = m.get("captain") or ""
        if not captain:
            continue
        if m.get("captain_played"):
            annotation = f"{m['captain_points']} pts"
        else:
            vc_name = m.get("vice_captain") or "?"
            vc_pts = m.get("vice_captain_points", 0)
            annotation = f"dnp; vice {vc_name} scored {vc_pts} pts"
        by_captain.setdefault(captain, []).append((m["manager_name"], annotation))

    if not by_captain:
        return ""

    groups = sorted(by_captain.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines = [f"Total captains: {sum(len(v) for v in by_captain.values())}"]
    for player, entries in groups:
        entries.sort(key=lambda e: e[0])
        joined = ", ".join(f"{name} ({ann})" for name, ann in entries)
        lines.append(f"- **{player}** (×{len(entries)}): {joined}")
    return "\n".join(lines)


def format_recap_fines_context(data: LeagueRecapData) -> str:
    """Format fines for the LLM prompt."""
    fines = data.get("fines", [])
    if not fines:
        return ""
    return "\n".join(f"- {f['manager_name']}: {f['message']}" for f in fines)
