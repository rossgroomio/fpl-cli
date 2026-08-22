"""Prompts for gameweek review LLM summaries."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from fpl_cli.utils.gameweek import is_opening_gameweek
from fpl_cli.utils.markdown import fence_flags, parse_heading
from fpl_cli.utils.text import strip_diacritics

logger = logging.getLogger(__name__)

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
The GW Narrative section should be written in the style of Jonathan Liew - lyrical, evocative prose that finds the poetry in the gameweek. Wry observations, clever turns of phrase, balancing the mundane with the profound. Stay grounded in the actual events of the gameweek - do not invent metaphorical comparisons that introduce other named players (e.g. "X's doppelgänger Y", "the new Z"). Lyricism comes from describing what happened, not from importing names from outside the data.
</style>

<rules>
ALWAYS:
- Lead with narrative, not data
- Attribute insights to sources where possible
- Capture the emotional tone of the GW
- Note contrarian takes worth considering
- Use the manager names provided in <team_context> - do not substitute from training data. This applies to ALL sections including prose narrative, not just table cells. Before naming any manager in any sentence, verify the team_context mapping: "Manager X manages Team Y" - only write that connection if team_context says so. A manager known from training data for a certain tactical style (e.g. a "system" manager) must NOT be attributed to a team they do not manage according to team_context, even when writing figurative or lyrical prose
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
- Treat 3-letter team codes (LEE, NEW, MAN, BUR, ARS, etc.) as surnames or people's names. LEE is Leeds United, not someone called "Lee"; NEW is Newcastle, not "New"; MAN is Manchester, not "Man". In prose, always expand codes to the full team name (or a natural short form like "Leeds", "Newcastle", "Man Utd"). Reserve 3-letter codes for table cells only
- Fabricate fixture counts, goal totals, or any other numeric summary statistic. If you mention the number of fixtures or total goals, use the values from the "Summary:" line at the top of the GW Results block. If that line is absent, don't cite a count at all
- Split a DGW player's gameweek total across their two fixtures ("14 in the first, 5 in the second"). You only receive the GW total - any per-match breakdown is fabrication. Cite the full GW total only, or describe the haul qualitatively ("a clean sheet and a goal in the DGW") without assigning points to individual fixtures
- Fabricate transfer history, loan arrangements, or contractual details about players in the Disappointments or Standout Performers tables. If you lack sourced information explaining why a player blanked or hauled, describe the statistical outcome ("returned just 1 point") without inventing a backstory. Do not reference a player's club history, loan status, or off-field context unless it appeared in your search results
- Name any player in the GW Narrative paragraph who does not appear in the Dream Team list, the Blankers list, or as a goalscorer/assister in the GW Results match lines. The narrative must reference only players grounded in the provided data - no metaphorical comparisons, no "X reminded us of Y", no "the next Z". If you cannot make a point without naming an unprovided player, drop the comparison and describe what actually happened instead

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
[3-5 players drawn EXCLUSIVELY from the Dream Team list above. Do not include any player not on that list. Use actual points from GW data.]

## Disappointments
| Player | Club | Pts | What Went Wrong | Concern Level |
|--------|------|-----|-----------------|---------------|
[3-5 players drawn EXCLUSIVELY from the Blankers list above. Do not include any player not on that list, even if they had a poor gameweek by other measures. Use actual points and ownership from GW data. The "What Went Wrong" cell must describe only the individual player in that row — do not introduce other players or teams not on the list.]

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
    team_glossary: str = "",
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
        team_glossary: Comma-separated 3-letter-code to full-name mapping (e.g. "ARS = Arsenal, LEE = Leeds United") so the LLM never renders codes as surnames.

    Returns:
        Formatted user prompt string.
    """
    # Build GW results section if data is provided
    gw_results = ""
    if dream_team or blankers or match_results:
        gw_results_parts = ["<gw_results>"]
        if manager_context or team_glossary:
            gw_results_parts.append("<team_context>")
            if team_glossary:
                gw_results_parts.append(
                    "Team code glossary (codes map to full team names — never render codes as surnames in prose):"
                )
                gw_results_parts.append(team_glossary)
            if manager_context:
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
- Your "Standout Performers" section MUST ONLY include players from the Dream Team list above. Do not add any player not on that list.
- Your "Disappointments" section MUST ONLY include players from the Blankers list above. Do not add any player not on that list.
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
- Ignore bench points - if players on the bench outscored starters, call it out
- Infer a scoring breakdown from a player's total points. You only receive totals - you do NOT know how many minutes they played, whether they kept a clean sheet, scored, assisted, got bonus, or were booked. Never write phrases like "presumably a clean sheet appearance", "must have got an assist", "looks like a 60+ minute cameo", or any similar guess. If the total is low, just state the total ("Mac Allister managed 1 point") without speculating on the components
- Attribute DGW or BGW status to any team not listed in the `<gw_fixtures>` block. That block is authoritative - if the community narrative implies a team played twice or blanked, ignore it unless the team is explicitly in the DGW/BGW list. Every team not listed played ONCE. Never write "in a DGW", "from a double gameweek", "blanked in their DGW" etc. for a single-gameweek team
- Use the word "league" to refer to the global FPL game. In this prompt, "league" ALWAYS means the user's mini-league (Classic or Draft) by name. The "Global FPL top score" and "Global FPL average" are community-wide stats across all FPL managers worldwide - refer to them as "the global top score", "the overall average", or "the best manager in the game". NEVER write "the highest in the league", "the top score in the league", or any phrasing that implies these global stats came from the user's mini-league\""""

_HARD_CONSTRAINTS_FINE_NEVER = """\
- Miss a fine trigger - these are socially important to the user's leagues"""

_HARD_CONSTRAINTS_ALWAYS = """\
- Analyse Classic and Draft separately with distinct verdicts
- Reference specific players and points where it adds colour (e.g., "Bruno G hauled 11 points" or "Grealish's -1 was painful")
- Highlight selection mistakes: if a "Bench vs Starters" section is provided in the player data, use it directly - these are pre-computed formation-valid comparisons. Also flag wrong captain choices
- Evaluate captain quality using the "Hindsight Best Captain" line. Captain points in the player data are ALREADY multiplied - compare raw-to-raw, not raw-to-multiplied. If the hindsight line names a different player as optimal, the captain was a mistake: state who would have been better and the +N pts swing shown. Only call the captain pick "clever"/"the right call"/"paid off" when the hindsight line confirms they were the optimal captain
- Note team concentration when notable: if 2+ players from the same team collectively hauled or blanked, call it out. Team-grouping and position-grouping are independent: if you don't have a position label for a player in the data, don't state one — refer to them by name only ("Brighton had Welbeck and Van Hecke both blanking"). Only use a position label when it appears explicitly in the data you received (e.g. "Brighton forward Welbeck"). Never infer position from context, club, or guess
- Maintain wry, dry humour especially when delivering bad news
- When suggesting players to move on from, specify which format (Classic or Draft)
- If a chip was played, frame the Classic Verdict around whether the chip paid off - chips raise expectations"""

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
Chips (each changes how you should frame the verdict):
- **Triple Captain (TC)** = captain's points are tripled (not doubled). Shown as "(TC)" in player data. A TC haul or flop is always worth calling out.
- **Bench Boost (BB)** = all 15 players score, not just the starting XI. Bench points are the strategy, not luck. The bar for total points is higher because you're fielding a full squad - a below-average BB week is a waste. Under BB, auto-subs are cosmetic: a DNP starter replaced by a bench player produces zero points delta because both were already scoring. NEVER frame an auto-sub in a BB week as "rescuing" points or "the sub delivered N points" - the N points would have been banked either way. Players tagged with "no points impact: BB active" must be discussed in that light, if at all.
- **Free Hit (FH)** = unlimited transfers for one week, squad reverts next GW. Higher expectations since the manager had a blank slate. Frame as a tactical punt that paid off or didn't. Summarise transfers as squad construction, not individual hit/miss verdicts.
- **Wildcard (WC)** = unlimited free transfers (squad persists). All transfers were free - do not evaluate individual transfer hits/misses. Frame as squad construction quality: did the new squad deliver?

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

_EDGE_CASE_WAIVERS = 'If no waivers processed in Draft, note "No waivers this week" in Draft Verdict'
_EDGE_CASE_MISSING_DATA = "If data for one format is missing, analyse only the format with data"

_EDGE_CASES = f"""\
<edge_cases>
- If no transfers were made in Classic, note "No transfers this week" in Classic Verdict
- {_EDGE_CASE_WAIVERS}
- {_EDGE_CASE_MISSING_DATA}
</edge_cases>"""

_EDGE_CASES_OPENING_GAMEWEEK = f"""\
<edge_cases>
- Classic has no transfers in the opening gameweek and no transfer was rolled or held. Judge the squad as a pre-season build, not as a week of restraint
- {_EDGE_CASE_WAIVERS}
- {_EDGE_CASE_MISSING_DATA}
- If the league standings are reported as unavailable, say the table is not out rather than inventing or inferring a position
</edge_cases>"""

_SEASON_CONTEXT_OPENING_GAMEWEEK = """\
<season_context>
This is Gameweek 1 - the opening gameweek of the season. It differs from every other review:
- No transfers exist. Squads are bought pre-season and the first free transfer arrives in GW2. There was no transfer to make, roll, hold or take a hit on, so never praise or criticise transfer activity, restraint or "keeping the free transfer"
- There is no previous gameweek. Overall Rank equals this week's rank, no rank has moved, and no player has form, momentum or a run of games behind them. Never reference a rank change, a previous score, or last week
- Everything you see is the result of one match per team. Treat single-match evidence as thin: a haul or a blank is a data point, not a verdict on a player
- The squad itself is the decision under review. Assess selection, captaincy and squad construction
- For "Next Week", the live decision is the manager's first transfer of the season (and their first waiver in Draft)
</season_context>
"""

# User prompt template (data sections - no fine_results, that's added conditionally)
_USER_PROMPT_TEMPLATE = """\
Analyse my Gameweek {gameweek} performance across both Classic and Draft formats.

<community_context>
{research_summary}
</community_context>

<gw_fixtures>
Authoritative fixture counts for this gameweek (use this, NOT the community narrative, to determine DGW/BGW status):
- Double Gameweek teams (played twice): {dgw_teams_line}
- Blank Gameweek teams (did not play): {bgw_teams_line}
- Every other team played ONCE (single gameweek).
</gw_fixtures>
{season_context}

<classic_data>
## Team Performance
Points: {classic_points} (Global FPL average: {classic_average}, Global FPL top score: {classic_highest})
GW Rank: {classic_gw_rank}
Overall Rank: {classic_overall_rank}
Captain: {classic_captain}
Hindsight Best Captain: {classic_captain_hindsight}
{active_chip_line}

## Players
{classic_players}

## Transfers Made
{classic_transfers}

## League Standing
League: {classic_league_name}
{classic_league_position_block}
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


def _build_system_prompt(
    *, has_fines: bool, use_net_points: bool = False, is_opening_gameweek: bool = False,
) -> str:
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
        _EDGE_CASES_OPENING_GAMEWEEK if is_opening_gameweek else _EDGE_CASES,
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
    classic_captain_hindsight: str,
    classic_players: str,
    classic_transfers: str,
    classic_league_name: str,
    classic_gw_position: int | str,
    classic_position: int | str,
    classic_total: int | str,
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
    dgw_teams: str = "",
    bgw_teams: str = "",
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

    if classic_total in (0, "unknown", None):
        classic_league_position_block = (
            "League standings unavailable for this review - no classic league table was"
            " provided. Do not state, infer or narrate a league position for Classic."
        )
    else:
        classic_league_position_block = (
            f"GW Position: {classic_gw_position} of {classic_total}"
            f" ({'by net points ' if use_net_points else 'by points '}this gameweek)\n"
            f"Overall League Position: {classic_position} of {classic_total}"
        )

    has_fines = bool(fine_results)
    opening_gameweek = is_opening_gameweek(gameweek)
    system_prompt = _build_system_prompt(
        has_fines=has_fines, use_net_points=use_net_points, is_opening_gameweek=opening_gameweek,
    )

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
            classic_captain_hindsight=classic_captain_hindsight,
            classic_players=classic_players,
            classic_transfers=classic_transfers,
            classic_league_name=classic_league_name,
            classic_league_position_block=classic_league_position_block,
            classic_rivals=classic_rivals,
            classic_worst_performers=classic_worst_performers,
            classic_transfer_impact=classic_transfer_impact or "",
            classic_performers_header_suffix=" (by Net Points)" if use_net_points else "",
            active_chip_line=active_chip_line,
            dgw_teams_line=dgw_teams or "none this gameweek",
            bgw_teams_line=bgw_teams or "none this gameweek",
            season_context=_SEASON_CONTEXT_OPENING_GAMEWEEK if opening_gameweek else "",
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
_TEAM_CODE_SHAPE_RE = re.compile(r"^[A-Z]{2,4}$")


def validate_research_teams(
    text: str,
    player_map: dict[int, Player],
    teams: dict[int, Team],
    table_allowlist: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Cross-reference table rows in research provider output against FPL API data.

    Corrects team codes in the Standout Performers and Disappointments tables.
    When ``table_allowlist`` is provided, also validates player name cells: strips
    rows for players absent from the list, and corrects corrupted names (e.g.
    "Beto (Ekitiké)" → "Ekitiké") where a known name appears within the cell.

    Args:
        text: The research provider response text containing markdown tables.
        player_map: Mapping of player ID to Player model (from FPL API).
        teams: Mapping of team ID to Team model (from FPL API).
        table_allowlist: Canonical player names from the blankers/dream-team lists
            passed to the research prompt. When provided, rows for players not
            in this set are stripped, and corrupted names are corrected.

    Returns:
        A tuple of (corrected_text, corrections_log) where corrections_log lists
        each correction or strip as a human-readable string.
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

    # Map of accepted team-cell values (short codes and full names, case-insensitive)
    # to their canonical short_name. Lets the gate accept rows where the LLM rendered
    # the club as either "ARS" or "Arsenal" instead of failing the regex and skipping
    # the row entirely.
    team_alias_to_short: dict[str, str] = {}
    for team in teams.values():
        team_alias_to_short[team.short_name.lower()] = team.short_name
        team_alias_to_short[team.name.lower()] = team.short_name

    # Pre-compute normalised → canonical mapping for known names.
    # Sorted by descending length so longer canonicals (e.g. "Bruno Fernandes")
    # win over shorter prefixes (e.g. "Bruno") when both are present.
    normalised_known: list[tuple[str, str]] = (
        sorted(
            ((strip_diacritics(n).lower(), n) for n in table_allowlist),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )
        if table_allowlist
        else []
    )

    # Scan for table sections and correct team codes / player names
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

                # Skip separator rows (e.g. |--------|------|). Admit cells that
                # are either a recognised PL team alias (short code or full name)
                # or just code-shaped (uppercase 2-4 letters) — the latter is a
                # fallback for unknown short codes so name validation still runs.
                team_cell_short = team_alias_to_short.get(team_cell.lower())
                if team_cell_short is None and _TEAM_CODE_SHAPE_RE.match(team_cell):
                    team_cell_short = team_cell
                if team_cell_short is not None:
                    # Strip markdown bold/italics anywhere in the cell before
                    # name comparison (handles "**Salah**", "**Salah** (note)").
                    plain_player = re.sub(r"\*+", "", player_cell).strip()
                    normalised_player = strip_diacritics(plain_player).lower()

                    # Name validation: strip unknown rows, correct corrupted names
                    if normalised_known:
                        matched_canonical = next(
                            (
                                canonical
                                for norm, canonical in normalised_known
                                if re.search(rf"\b{re.escape(norm)}\b", normalised_player)
                            ),
                            None,
                        )
                        if matched_canonical is None:
                            corrections.append(f"{plain_player}: stripped (not in provided list)")
                            continue
                        canonical_normalised = strip_diacritics(matched_canonical).lower()
                        if canonical_normalised != normalised_player:
                            line = re.sub(r"^\|[^|]+\|", f"| {matched_canonical} |", line, count=1)
                            corrections.append(f"{player_cell}: name corrected -> {matched_canonical}")
                            plain_player = matched_canonical
                            normalised_player = canonical_normalised

                    # Team code correction. Compare resolved short_name to resolved
                    # short_name so a row already correct as "Arsenal" doesn't get
                    # rewritten to "ARS" purely for code-vs-name format.
                    expected_team = name_to_team.get(normalised_player)
                    if expected_team and expected_team != team_cell_short:
                        line = re.sub(
                            r"\|\s*" + re.escape(team_cell) + r"\s*\|",
                            f"| {expected_team} |",
                            line,
                            count=1,
                        )
                        corrections.append(f"{plain_player}: {team_cell} -> {expected_team}")

        corrected_lines.append(line)

    return "\n".join(corrected_lines), corrections


_NARRATIVE_HEADER_RE = re.compile(
    r"^#{2,3}\s+(GW|Gameweek)\s*\d+\s+Narrative\b.*$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(
    r"(?:(?<=[.!?][\"')\]])|(?<=[.!?]))\s+(?=[A-Z\"'\[(])"
)
_MIN_PROSE_ALLOWLIST_SIZE = 5


def validate_research_prose(
    text: str,
    player_map: dict[int, Player],
    allowlist: set[str],
) -> tuple[str, list[str]]:
    """Strip sentences in the GW Narrative paragraph that name PL players outside the allowlist.

    The research provider's GW Narrative paragraph is freeform prose and is not
    constrained by the table validator. Build a set of disallowed surnames as
    {all PL web_names} − allowlist, then drop any sentence containing a
    disallowed surname (whole-word match, diacritic-insensitive). If anything
    is stripped, append a visible "[narrative scrubbed: N reference(s)
    removed]" stub to the paragraph so the report does not silently shrink.

    Args:
        text: Full research provider response.
        player_map: Mapping of player ID to Player model (full PL roster).
        allowlist: Player web_names that are legitimately referenceable in the
            narrative. Typically Dream Team ∪ Blankers ∪ match-scorers/assisters
            ∪ user's squad.

    Returns:
        (corrected_text, corrections_log).
    """
    lines = text.split("\n")
    # Fence-aware so a code example in the response containing a line that
    # merely looks like a heading (a leading '#') doesn't falsely open or
    # close the narrative section.
    fenced = list(fence_flags(lines))
    header_idx: int | None = None
    for i, line in enumerate(lines):
        if not fenced[i] and _NARRATIVE_HEADER_RE.match(line):
            header_idx = i
            break

    if header_idx is None:
        logger.warning("validate_research_prose: no narrative header found in text")
        return text, []

    # Guard against an empty/near-empty allowlist collapsing the whole narrative:
    # any realistic GW supplies far more than this from Dream Team alone.
    if len(allowlist) < _MIN_PROSE_ALLOWLIST_SIZE:
        logger.warning(
            "validate_research_prose: allowlist size %d below threshold %d; skipping prose validation",
            len(allowlist),
            _MIN_PROSE_ALLOWLIST_SIZE,
        )
        return text, []

    end_idx = len(lines)
    for j in range(header_idx + 1, len(lines)):
        if not fenced[j] and parse_heading(lines[j]) is not None:
            end_idx = j
            break

    paragraph = "\n".join(lines[header_idx + 1 : end_idx]).strip()
    if not paragraph:
        return text, []

    normalised_allow = {strip_diacritics(n).lower() for n in allowlist}
    # Compile each pattern against the diacritic-stripped name (so LLM output
    # that drops accents — "Hojlund" for "Højlund" — still matches) but keep
    # original-case so the regex requires a capitalised hit in the source
    # text; this avoids over-scrubbing English homographs like "son", "may".
    disallowed: list[tuple[str, re.Pattern[str]]] = []
    for player in player_map.values():
        name = player.web_name
        norm = strip_diacritics(name).lower()
        if norm and norm not in normalised_allow:
            stripped_name = strip_diacritics(name)
            disallowed.append((name, re.compile(rf"\b{re.escape(stripped_name)}\b")))

    if not disallowed:
        return text, []

    # Split on newlines first so a missing terminal punctuation between lines
    # doesn't merge sentences into a single chunk.
    sentences: list[str] = []
    for chunk in paragraph.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        sentences.extend(_SENTENCE_SPLIT_RE.split(chunk))

    kept: list[str] = []
    corrections: list[str] = []
    for sentence in sentences:
        sentence_stripped = strip_diacritics(sentence)
        hit_name: str | None = None
        for name, pattern in disallowed:
            if pattern.search(sentence_stripped):
                hit_name = name
                break
        if hit_name is None:
            kept.append(sentence)
        else:
            corrections.append(
                f"narrative sentence stripped (disallowed: {hit_name}): {sentence.strip()[:120]}"
            )

    if not corrections:
        return text, []

    rebuilt = " ".join(s.strip() for s in kept if s.strip())
    rebuilt = rebuilt.strip()
    stub = f"[narrative scrubbed: {len(corrections)} fabricated reference(s) removed]"
    new_paragraph = f"{rebuilt} {stub}" if rebuilt else stub

    new_lines = lines[: header_idx + 1] + ["", new_paragraph, ""] + lines[end_idx:]
    return "\n".join(new_lines), corrections
