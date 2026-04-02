"""Prompts for gameweek review LLM summaries."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fpl_cli.utils.text import strip_diacritics

if TYPE_CHECKING:
    from fpl_cli.models.player import Player
    from fpl_cli.models.team import Team

# =============================================================================
# RESEARCH PROMPTS (Stage 1: Social + Journalistic)
# =============================================================================

REVIEW_RESEARCH_SYSTEM_PROMPT = """You are an FPL analyst providing post-gameweek narrative and insight.

<context>
The user has all the statistical data: standings, transfers, results. Your value is narrative, sentiment, and insight that numbers alone don't capture.
</context>

<priorities>
Focus on these signal types, in order of importance:

1. **Standout Performers** - Who hauled and WHY (eye-test observations, tactical reasons, not just "scored twice")
2. **Flops & Disappointments** - Who blanked unexpectedly, rotation victims, injury concerns emerging
3. **Community Sentiment** - FPL Twitter/Reddit reaction, rant thread themes, what managers are feeling
4. **Match Analysis** - Tactical observations from match reports, pundit takes, manager quotes
5. **Community Outlook** - What the FPL community is saying about next moves (captures sentiment, not recommendations)
</priorities>

<trusted_sources>
Prefer these sources when available, but do not treat them as requirements:
- FPL community accounts (e.g. @FPL_Architect, @Lateriser12, @FPLGeneral, @BenCrellin)
- r/FantasyPL post-GW discussion threads
- Quality football coverage (Guardian, Athletic, BBC, Sky Sports)
- Post-match manager press conferences

If these specific sources aren't reachable, use whatever quality football and FPL
coverage the search returns. Any reputable match report or community discussion is
valid sourcing.
</trusted_sources>

<style>
The GW Narrative section should be written in the style of Jonathan Liew - lyrical, evocative prose that finds the poetry in the gameweek. Wry observations, clever turns of phrase, balancing the mundane with the profound.
</style>

<rules>
ALWAYS:
- Lead with narrative, not data
- Attribute insights to sources where possible
- Capture the emotional tone of the GW
- Note contrarian takes worth considering
- Use the manager names provided in <team_context> - do not substitute from training data
- Use the player-team associations from the GW data provided. If web sources contradict the provided data on which team a player belongs to, trust the provided data
- When double-gameweek teams are listed, contextualise their players' hauls accordingly (e.g. points came across two matches, not one exceptional performance)

PREFERRED:
- Include points for standout performers and disappointments where available
- This anchors the narrative but isn't essential if not readily found

NEVER:
- Invent or fabricate sources, quotes, or community sentiment
- List every haul - focus on the narrative-worthy
- List players from blank-gameweek teams as disappointments or fabricate match narratives for matches that didn't happen
- Treat a blank-gameweek zero as a performance failure - most FPL managers plan for these
- Speculate about future double or blank gameweeks for teams NOT listed in the provided actual or predicted DGW data

IF web search returns limited narrative sources:
- Still produce all sections using the match results and player data provided
- Write analysis grounded in the actual results (scorelines, goalscorers, tactical context)
- Mark community sentiment sections with [Limited sourcing] rather than omitting them
- Do NOT refuse to generate output or ask the user for additional sources
</rules>"""


REVIEW_RESEARCH_USER_PROMPT_TEMPLATE = """Provide post-gameweek narrative and insight for Gameweek {gameweek}.

This query runs in the 24-48h after the gameweek finished.

{gw_results}

<research_focus>
1. Scan FPL Twitter for community reaction - who's being celebrated, who's being shipped, what's the mood?
2. Check r/FantasyPL rant thread for sentiment and emerging consensus
3. Read match reports from Guardian/Athletic for tactical observations
4. Note post-match manager quotes that hint at rotation, form, or tactical changes
5. Identify which assets are being talked about as buys vs sells going forward
</research_focus>

<output_format>
## GW{gameweek} Narrative
[2-3 sentences in the style of Jonathan Liew - lyrical, evocative, capturing the character and emotional arc of the gameweek]

## Standout Performers
| Player | Club | Pts | Why They Hauled | Source |
|--------|------|-----|-----------------|--------|
[3-5 players from the Dream Team above with narrative insight. Use actual points from GW data.]

## Disappointments
| Player | Club | Pts | What Went Wrong | Concern Level |
|--------|------|-----|-----------------|---------------|
[3-5 players from the Blankers list above. Use actual points and ownership from GW data.]

## Community Pulse
- **Mood:** [One-word + elaboration]
- **Most discussed:** [Key talking points from Twitter/Reddit]
- **Hot takes:** [Contrarian or spicy opinions gaining traction]

## Match Analysis
[2-3 tactical or analytical observations from quality football coverage - formation changes, player role shifts, manager decisions that shaped the GW]

## Community Outlook
[What the FPL community is saying about next moves - bandwagons forming, players being shipped, emerging consensus. This captures sentiment, not recommendations.]
</output_format>

<quality_requirements>
- Every insight should have attribution or clear sourcing
- Prioritise signal over noise - focus on the narrative-worthy performers
- Capture what makes this GW memorable or notable
</quality_requirements>"""


def get_review_research_prompt(
    gameweek: int,
    dream_team: str = "",
    blankers: str = "",
    match_results: str = "",
    manager_context: str = "",
    bgw_teams: str = "",
    dgw_teams: str = "",
    predicted_dgw_teams: str = "",
) -> str:
    """Generate the research user prompt for a specific gameweek review.

    Args:
        gameweek: The gameweek number to review.
        dream_team: Formatted string of Dream Team players (11 players with highest GW points).
        blankers: Formatted string of high-ownership players who blanked (≤2 pts).
        match_results: Compact scoreline string (e.g. "BHA 1-1 EVE | LEE 0-4 ARS | ...").
        manager_context: Formatted string of team-code-to-manager mappings.
        bgw_teams: Comma-separated short names of teams with a blank gameweek (e.g. "MCI, ARS").
        dgw_teams: Comma-separated short names of teams with a double gameweek (e.g. "EVE, BHA").
        predicted_dgw_teams: Formatted string of predicted future DGWs (e.g. "GW32: EVE, BHA (high confidence)").

    Returns:
        Formatted user prompt string.
    """
    # Build GW results section if data is provided
    gw_results = ""
    if dream_team or blankers or match_results:
        gw_results_parts = ["<gw_results>"]
        if manager_context:
            gw_results_parts.append("<team_context>")
            gw_results_parts.append(manager_context)
            gw_results_parts.append("</team_context>")
        if bgw_teams:
            gw_results_parts.append(f"\n## Blank Gameweek Teams (did NOT play in GW{gameweek})")
            gw_results_parts.append(bgw_teams)
        if dgw_teams:
            gw_results_parts.append(f"\n## Double Gameweek Teams (played TWICE in GW{gameweek})")
            gw_results_parts.append(dgw_teams)
        if predicted_dgw_teams:
            gw_results_parts.append("\n## Predicted Double Gameweeks (upcoming)")
            gw_results_parts.append(predicted_dgw_teams)
        if match_results:
            gw_results_parts.append(f"\n## GW{gameweek} Results")
            gw_results_parts.append(match_results)
        if dream_team:
            gw_results_parts.append(f"\n## GW{gameweek} Dream Team (Official Top Performers)")
            gw_results_parts.append(dream_team)
        if blankers:
            gw_results_parts.append(f"\n## GW{gameweek} Disappointments (High-Ownership Blankers)")
            gw_results_parts.append(blankers)
        gw_results_parts.append("""
IMPORTANT:
- Your "Standout Performers" section MUST feature players from the Dream Team above.
- Your "Disappointments" section MUST feature players from the Blankers list above.
- Do not highlight players based on general form or transfer trends - use the actual GW data provided.
</gw_results>""")
        gw_results = "\n".join(gw_results_parts)

    return REVIEW_RESEARCH_USER_PROMPT_TEMPLATE.format(
        gameweek=gameweek,
        gw_results=gw_results,
    )


# =============================================================================
# SYNTHESIS PROMPTS (Stage 2: Personal Analysis)
#
# System prompt is assembled from fragments to support conditional fine sections.
# Fragment assembly order:
#   1. _SYSTEM_INTRO
#   2. _HARD_CONSTRAINTS (+ _HARD_CONSTRAINTS_FINE_LINES when fines enabled)
#   3. _CONTEXT_BASE (+ _CONTEXT_FINE_PARAGRAPH when fines enabled)
#   4. _TONE_BASE or _TONE_WITH_FINES
#   5. _OUTPUT_FORMAT_WITH_FINES or _OUTPUT_FORMAT_NO_FINES
#   6. _EDGE_CASES
# =============================================================================

_SYSTEM_INTRO = """You are an FPL analyst providing personalised gameweek analysis with a wry, dry sense of humour."""

_HARD_CONSTRAINTS_BASE_NEVER = """\
- Lump Classic and Draft analysis together - they are separate competitions with different rules
- Be vague ("decent week") without specific player/decision references
- Ignore bench points - if players on the bench outscored starters, call it out"""

_HARD_CONSTRAINTS_FINE_NEVER = """\
- Miss a fine trigger - these are socially important to the user's leagues"""

_HARD_CONSTRAINTS_ALWAYS = """\
- Analyse Classic and Draft separately with distinct verdicts
- Reference specific players and points where it adds colour (e.g., "Bruno G hauled 11 points" or "Grealish's -1 was painful")
- Highlight selection mistakes: if a "Bench vs Starters" section is provided in the player data, use it directly - these are pre-computed formation-valid comparisons. Also flag wrong captain choices
- Note team concentration when notable: if 2+ players from the same team collectively hauled or blanked, call it out
- Maintain wry, dry humour especially when delivering bad news
- When suggesting players to move on from, specify which format (Classic or Draft)"""

_HARD_CONSTRAINTS_FINE_ALWAYS = """\
- Check fine triggers for EACH format against its specific rules"""

_CONTEXT_BASE = """\
<context>
You receive data for TWO separate FPL competitions:

1. **Classic FPL** - Traditional format with transfers, captain choice, and your classic league
2. **Draft FPL** - Different format with waivers (not transfers), no captain, separate league
"""

_CONTEXT_FINE_PARAGRAPH = """\
These have DIFFERENT fine rules and should be analysed independently. A good Classic week doesn't offset a bad Draft week (and vice versa).
"""

_CONTEXT_TAIL = """\
Chips:
- **Triple Captain (TC)** = captain's points are tripled (not doubled). Shown as "(TC)" in player data. A TC haul or flop is always worth calling out.

In the player data:
- Players with points shown normally contributed to your score
- `[AUTO-SUB IN]` = bench player who came on when a starter didn't play
- `[DIDN'T PLAY - auto-subbed out]` = starter who was replaced (0 pts)
- `[BENCH - X pts unused!]` = bench player with good points (6+) who wasn't needed
- `[BENCH]` = bench player who stayed on bench
- In the Verdict sections, ONLY discuss players who actually contributed to the score (starters who played + auto-sub-ins). NEVER cite a [BENCH] player as a contributor - bench players belong in the Selection analysis only
- Analyse auto-sub outcomes: did they help or hurt? Were bench order decisions good?
- `Bench vs Starters (formation-valid swaps):` = pre-computed analysis of bench players who outscored starters where the swap maintains a valid formation. Swaps tagged [formation change] require a different formation. If present, always reference these in your Selection assessment
</context>"""

_TONE_BASE = """\
<tone>
- Direct and honest - don't sugarcoat bad decisions
- Wry, dry humour when delivering bad news
- Celebrate wins genuinely, acknowledge misses honestly
- No excessive positivity or toxic negativity
</tone>"""

_TONE_WITH_FINES = """\
<tone>
- Direct and honest - don't sugarcoat bad decisions
- Wry, dry humour - especially for fines ("Grealish saw red. Time to dust off the pint glass.")
- Celebrate wins genuinely, acknowledge misses honestly
- No excessive positivity or toxic negativity
</tone>"""

_OUTPUT_FORMAT_HEADER = """\
<output_format>
Structure your response EXACTLY as follows:

## Summary
[2-3 sentences: High-level verdict across both formats. Were you a winner, loser, or somewhere in between this week? Use the GW Position annotation (e.g. "4th= worst") for any league framing - do not re-derive positions from raw data.]
"""

_FINE_CHECK_POINTS_INSTRUCTION = {
    True: "When quoting scores or the gap to the next-worst manager, ALWAYS use net points - never gross.",
    False: "Use points as shown - NEVER reference transfer costs, hits, or net points (these are not tracked).",
}

_OUTPUT_FORMAT_FINE_CHECK_TEMPLATE = """\

## Fine Check
### Classic
[Narrate the pre-computed fine results from <fine_results>. {points_instruction}

When stating your GW position:
- Use the "GW Position" field from the League Standing data - this is your rank THIS GAMEWEEK within the league (NOT your overall league position)
- The GW Position includes a pre-computed annotation in brackets: [TOP HALF], [BOTTOM HALF], or [EXACT MIDDLE]. Trust this annotation - do not re-derive it.
- TOP HALF: frame from top ("You finished 4th this week")
- BOTTOM HALF: frame from bottom ("You finished 4th worst this week")
- EXACT MIDDLE: frame neutrally ("You finished 6th of 11 - dead centre")
- Do NOT confuse this with "Overall League Position" which is the season-long standings]

### Draft
[Narrate the pre-computed fine results from <fine_results>.

Same framing rule as Classic: use "GW Position" (not overall) and trust the [TOP HALF] / [BOTTOM HALF] / [EXACT MIDDLE] annotation. Never misrepresent which half a position falls in.]

[If NO fines in either format, a brief acknowledgment of relief.]
"""

_OUTPUT_FORMAT_TAIL = """\

## Classic Verdict
[2-3 sentences: How did your Classic team perform? Only reference players who actually scored points for you (starters + auto-subs, NOT bench players). Disappointments, captain choice assessment. Reference the community narrative where your players featured.]

**Selection:** [Note any selection mistakes - did benched players outscore starters? Was the captain the right call? If 2+ players from the same team collectively hauled or blanked, note the exposure outcome. If selections were good, acknowledge briefly.]

## Draft Verdict
[2-3 sentences: How did your Draft team perform? Only reference players who actually scored points for you (starters + auto-subs, NOT bench players). Poor performers.]

**Selection:** [Note any selection mistakes - did benched players outscore starters? If 2+ players from the same team collectively hauled or blanked, note the exposure outcome. If selections were good, acknowledge briefly.]

## Next Week
[1-2 sentences: What does this GW suggest for upcoming decisions? If suggesting players to move on from, specify whether this applies to Classic, Draft, or both.]
</output_format>"""

_EDGE_CASES = """\
<edge_cases>
- If no transfers were made in Classic, note "No transfers this week" in Classic Verdict
- If no waivers processed in Draft, note "No waivers this week" in Draft Verdict
- If data for one format is missing, analyse only the format with data
</edge_cases>"""

# User prompt template (data sections - no fine_results, that's added conditionally)
_USER_PROMPT_TEMPLATE = """\
Analyse my Gameweek {gameweek} performance across both Classic and Draft formats.

<community_context>
{research_summary}
</community_context>

<classic_data>
## Team Performance
Points: {classic_points} (Average: {classic_average}, Highest: {classic_highest})
GW Rank: {classic_gw_rank}
Overall Rank: {classic_overall_rank}
Captain: {classic_captain}
{active_chip_line}

## Players
{classic_players}

## Transfers Made
{classic_transfers}

## League Standing
League: {classic_league_name}
GW Position: {classic_gw_position} of {classic_total} ({classic_points_qualifier}this gameweek)
Overall League Position: {classic_position} of {classic_total}
{classic_rivals}

## Worst GW Performers{classic_performers_header_suffix}
{classic_worst_performers}
{classic_transfer_impact}
</classic_data>

<draft_data>
## Team Performance
Points: {draft_points}

## Players
{draft_players}

## Waivers Processed
{draft_transactions}

## League Standing
League: {draft_league_name}
GW Position: {draft_gw_position} of {draft_total} (by points this gameweek)
Overall League Position: {draft_position} of {draft_total}

## Worst GW Performers
{draft_worst_performers}
</draft_data>"""


def _build_system_prompt(*, has_fines: bool, use_net_points: bool = False) -> str:
    """Assemble the synthesis system prompt with conditional fine sections."""
    never_lines = [_HARD_CONSTRAINTS_BASE_NEVER]
    if has_fines:
        never_lines.append(_HARD_CONSTRAINTS_FINE_NEVER)

    always_lines = [_HARD_CONSTRAINTS_ALWAYS]
    if has_fines:
        always_lines.append(_HARD_CONSTRAINTS_FINE_ALWAYS)

    hard_constraints = (
        "<hard_constraints>\nNEVER:\n"
        + "\n".join(never_lines)
        + "\n\nALWAYS:\n"
        + "\n".join(always_lines)
        + "\n</hard_constraints>"
    )

    context = _CONTEXT_BASE
    if has_fines:
        context += "\n" + _CONTEXT_FINE_PARAGRAPH
    context += "\n" + _CONTEXT_TAIL

    tone = _TONE_WITH_FINES if has_fines else _TONE_BASE

    output_format = _OUTPUT_FORMAT_HEADER
    if has_fines:
        output_format += _OUTPUT_FORMAT_FINE_CHECK_TEMPLATE.format(
            points_instruction=_FINE_CHECK_POINTS_INSTRUCTION[use_net_points],
        )
    output_format += _OUTPUT_FORMAT_TAIL

    parts = [
        _SYSTEM_INTRO,
        hard_constraints,
        context,
        tone,
        output_format,
        _EDGE_CASES,
    ]
    return "\n\n".join(parts)


def get_review_synthesis_prompt(
    gameweek: int,
    research_summary: str,
    classic_points: int,
    classic_average: int,
    classic_highest: int,
    classic_gw_rank: int,
    classic_overall_rank: int,
    classic_captain: str,
    classic_captain_points: int,
    classic_players: str,
    classic_transfers: str,
    classic_league_name: str,
    classic_gw_position: int | str,
    classic_position: int,
    classic_total: int,
    classic_rivals: str,
    classic_worst_performers: str,
    classic_transfer_impact: str | None,
    draft_points: int,
    draft_league_name: str,
    draft_players: str,
    draft_transactions: str,
    draft_gw_position: int | str,
    draft_position: int,
    draft_total: int,
    draft_worst_performers: str = "No data",
    fine_results: str = "",
    escalation_note: str | None = None,
    active_chip: str | None = None,
    use_net_points: bool = False,
) -> tuple[str, str]:
    """Generate the synthesis system and user prompts for personalised gameweek analysis.

    Returns:
        Tuple of (system_prompt, user_prompt).
    """
    chip_names = {"3xc": "Triple Captain", "wildcard": "Wildcard", "freehit": "Free Hit", "bboost": "Bench Boost"}
    if active_chip:
        chip_display = chip_names.get(active_chip, active_chip)
        active_chip_line = f"Active Chip: {chip_display}"
    else:
        active_chip_line = ""

    has_fines = bool(fine_results)
    system_prompt = _build_system_prompt(has_fines=has_fines, use_net_points=use_net_points)

    user_parts = [
        _USER_PROMPT_TEMPLATE.format(
            gameweek=gameweek,
            research_summary=research_summary,
            classic_points=classic_points,
            classic_average=classic_average,
            classic_highest=classic_highest,
            classic_gw_rank=classic_gw_rank,
            classic_overall_rank=classic_overall_rank,
            classic_captain=classic_captain,
            classic_captain_points=classic_captain_points,
            classic_players=classic_players,
            classic_transfers=classic_transfers,
            classic_league_name=classic_league_name,
            classic_gw_position=classic_gw_position,
            classic_position=classic_position,
            classic_total=classic_total,
            classic_rivals=classic_rivals,
            classic_worst_performers=classic_worst_performers,
            classic_transfer_impact=classic_transfer_impact or "",
            classic_points_qualifier="by net points " if use_net_points else "by points ",
            classic_performers_header_suffix=" (by Net Points)" if use_net_points else "",
            active_chip_line=active_chip_line,
            draft_points=draft_points,
            draft_league_name=draft_league_name,
            draft_players=draft_players,
            draft_transactions=draft_transactions,
            draft_gw_position=draft_gw_position,
            draft_position=draft_position,
            draft_total=draft_total,
            draft_worst_performers=draft_worst_performers,
        ),
    ]

    if fine_results:
        fine_section = f"\n<fine_results>\n{fine_results}"
        if escalation_note:
            fine_section += f"\n\nNote: {escalation_note}"
        fine_section += "\n</fine_results>"
        user_parts.append(fine_section)

    return system_prompt, "\n".join(user_parts)


_TABLE_HEADERS = {
    "| Player | Club | Pts | Why They Hauled |",
    "| Player | Club | Pts | What Went Wrong |",
}

_ROW_RE = re.compile(r"^\|([^|]+)\|([^|]+)\|")
_TEAM_CODE_RE = re.compile(r"^[A-Z]{2,4}$")


def validate_research_teams(
    text: str,
    player_map: dict[int, Player],
    teams: dict[int, Team],
) -> tuple[str, list[str]]:
    """Cross-reference team codes in research provider markdown tables against the FPL API.

    Web-sourced narrative can attribute players to wrong teams. This function
    corrects team codes in the Standout Performers and Disappointments tables
    by looking up each player's actual team via ``player_map`` and ``teams``.

    Args:
        text: The research provider response text containing markdown tables.
        player_map: Mapping of player ID to Player model (from FPL API).
        teams: Mapping of team ID to Team model (from FPL API).

    Returns:
        A tuple of (corrected_text, corrections_log) where corrections_log lists
        each correction as "{player}: {old_code} -> {new_code}".
    """
    # Build name -> team short_name lookup, excluding ambiguous web_names
    name_counts: dict[str, list[int]] = {}
    for player in player_map.values():
        key = strip_diacritics(player.web_name).lower()
        name_counts.setdefault(key, []).append(player.team_id)

    name_to_team: dict[str, str] = {}
    for name, team_ids in name_counts.items():
        if len(team_ids) == 1:
            team = teams.get(team_ids[0])
            if team:
                name_to_team[name] = team.short_name

    # Scan for table sections and correct team codes
    lines = text.split("\n")
    corrected_lines: list[str] = []
    corrections: list[str] = []
    in_table = False

    for line in lines:
        # Check for table header
        if any(header in line for header in _TABLE_HEADERS):
            in_table = True
            corrected_lines.append(line)
            continue

        # Exit table on blank line or non-table line
        if in_table and (not line.strip() or not line.strip().startswith("|")):
            in_table = False
            corrected_lines.append(line)
            continue

        if in_table:
            match = _ROW_RE.match(line)
            if match:
                player_cell = match.group(1).strip()
                team_cell = match.group(2).strip()

                # Skip separator rows (e.g. |--------|------|)
                if _TEAM_CODE_RE.match(team_cell):
                    normalised = strip_diacritics(player_cell).lower()
                    expected_team = name_to_team.get(normalised)
                    if expected_team and expected_team != team_cell:
                        line = re.sub(
                            r"\|\s*" + re.escape(team_cell) + r"\s*\|",
                            f"| {expected_team} |",
                            line,
                            count=1,
                        )
                        corrections.append(f"{player_cell}: {team_cell} -> {expected_team}")

        corrected_lines.append(line)

    return "\n".join(corrected_lines), corrections
