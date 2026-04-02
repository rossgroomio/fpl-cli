"""Team form calculation service."""

from __future__ import annotations

from typing import Any

from fpl_cli.models.fixture import Fixture
from fpl_cli.models.team import Team


def calculate_team_form(
    fixtures: list[Fixture],
    teams: list[Team],
) -> list[dict[str, Any]]:
    """Calculate form stats for all teams over last 6 matches.

    Returns for each team:
    - team_id: Team ID for lookup
    - league_position: Current league position (1-20)
    - pts_6: Points from last 6 matches
    - gs_6: Goals scored in last 6 matches
    - gc_6: Goals conceded in last 6 matches
    - next_venue: (H) or (A) for next fixture
    - pts_home/gs_home/gc_home: Stats from last 6 HOME matches
    - pts_away/gs_away/gc_away: Stats from last 6 AWAY matches
    - pts_ha/gs_ha/gc_ha: Alias to home/away based on next_venue (backward compat)
    """
    team_form = []

    for team in teams:
        # Get completed fixtures for this team
        completed = [
            f for f in fixtures
            if f.finished and (f.home_team_id == team.id or f.away_team_id == team.id)
        ]
        completed.sort(key=lambda f: f.gameweek or 0, reverse=True)
        recent_6 = completed[:6]

        # Calculate overall stats (last 6 matches)
        pts, gs, gc = 0, 0, 0
        for f in recent_6:
            is_home = f.home_team_id == team.id
            scored = f.home_score if is_home else f.away_score
            conceded = f.away_score if is_home else f.home_score

            if scored is not None and conceded is not None:
                pts += 3 if scored > conceded else 1 if scored == conceded else 0
                gs += scored
                gc += conceded

        # Calculate HOME-specific stats (last 6 home matches)
        home_fixtures = [f for f in completed if f.home_team_id == team.id][:6]
        pts_home, gs_home, gc_home = 0, 0, 0
        for f in home_fixtures:
            if f.home_score is not None and f.away_score is not None:
                pts_home += 3 if f.home_score > f.away_score else 1 if f.home_score == f.away_score else 0
                gs_home += f.home_score
                gc_home += f.away_score

        # Calculate AWAY-specific stats (last 6 away matches)
        away_fixtures = [f for f in completed if f.away_team_id == team.id][:6]
        pts_away, gs_away, gc_away = 0, 0, 0
        for f in away_fixtures:
            if f.home_score is not None and f.away_score is not None:
                pts_away += 3 if f.away_score > f.home_score else 1 if f.away_score == f.home_score else 0
                gs_away += f.away_score
                gc_away += f.home_score

        # Get next fixture to determine H/A
        upcoming = [
            f for f in fixtures
            if not f.finished and (f.home_team_id == team.id or f.away_team_id == team.id)
        ]
        upcoming.sort(key=lambda f: f.gameweek or 999)
        next_fix = upcoming[0] if upcoming else None
        is_home_next = next_fix and next_fix.home_team_id == team.id

        # Backward compatibility aliases (pts_ha points to home or away based on next venue)
        pts_ha = pts_home if is_home_next else pts_away
        gs_ha = gs_home if is_home_next else gs_away
        gc_ha = gc_home if is_home_next else gc_away

        team_form.append({
            "team": team.short_name,
            "team_name": team.name,
            "team_id": team.id,
            "league_position": team.position if team.position else 10,  # Default mid-table
            "pts_6": pts,
            "gs_6": gs,
            "gc_6": gc,
            "next_venue": "(H)" if is_home_next else "(A)",
            # Home-specific (last 6 home matches)
            "pts_home": pts_home,
            "gs_home": gs_home,
            "gc_home": gc_home,
            # Away-specific (last 6 away matches)
            "pts_away": pts_away,
            "gs_away": gs_away,
            "gc_away": gc_away,
            # Backward compat aliases
            "pts_ha": pts_ha,
            "gs_ha": gs_ha,
            "gc_ha": gc_ha,
        })

    # Sort by total points descending
    team_form.sort(key=lambda t: t["pts_6"], reverse=True)
    return team_form
