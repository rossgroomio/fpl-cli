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
- Reference specific decisions (captain picks, transfers, bench choices)
- Use the data to tell a story, not just list stats
- Brief - 300-400 words max. Punchy paragraphs, not walls of text
</tone>

<rules>
- NEVER give advice or recommendations. This is a recap, not a preview
- NEVER speculate about future gameweeks
- Stick to what happened this gameweek
- If fines were triggered, make them a highlight
- The biggest bench haul is always funny - lean into it
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

    sections.extend([
        "",
        "## Awards",
        awards_text,
        "",
        "## Standings",
        standings_text,
    ])

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

    lines = ["| Pos | Prev | Manager | GW Pts | Total |", "|-----|------|---------|--------|-------|"]
    for m in sorted(managers, key=lambda x: x.get("overall_rank", 0)):
        prev = m.get("previous_rank", "?")
        curr = m.get("overall_rank", "?")
        movement = ""
        if isinstance(prev, int) and isinstance(curr, int) and prev != curr:
            diff = prev - curr
            movement = f" (↑{diff})" if diff > 0 else f" (↓{abs(diff)})"
        lines.append(
            f"| {curr} | {prev} | {m['manager_name']}{movement} | {m['gw_points']} | {m['total_points']} |"
        )
    return "\n".join(lines)


def format_recap_fines_context(data: LeagueRecapData) -> str:
    """Format fines for the LLM prompt."""
    fines = data.get("fines", [])
    if not fines:
        return ""
    return "\n".join(f"- {f['manager_name']}: {f['message']}" for f in fines)
