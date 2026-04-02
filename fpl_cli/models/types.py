"""TypedDict definitions for high-traffic agent output shapes."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class FixtureDetail(TypedDict):
    opponent: str
    is_home: bool
    fdr: float


class CaptainCandidate(TypedDict):
    id: int
    player_name: str
    team_short: str
    position: str
    price: float
    ownership: float
    form: float
    ppg: float
    xG: float
    xA: float
    xGI: float
    xG_per_90: float
    xA_per_90: float
    xGI_per_90: float
    fixtures: list[FixtureDetail]
    fixture_count: int
    avg_fdr: float
    matchup_score: float
    attack_matchup: float
    defence_matchup: float
    form_differential: float
    position_differential: float
    pen_bonus: float
    captain_score: int
    captain_score_raw: float
    reasons: list[str]


class PlayerStats(TypedDict):
    id: int
    player_name: str
    team_short: str
    position: str
    price: int
    ownership: float
    minutes: int
    goals: int
    assists: int
    GI: int
    xG: float
    xA: float
    xGI: float
    xG_per_90: float
    xA_per_90: float
    xGI_per_90: float
    goals_minus_xG: float
    assists_minus_xA: float
    GI_minus_xGI: float
    form: float
    total_points: int
    ppg: float
    dc_per_90: float
    # Added by _merge_understat_data (always present after enrichment)
    npxG_per_90: float | None
    xGChain_per_90: float | None
    xGBuildup_per_90: float | None
    penalty_xG: float | None
    penalty_xG_per_90: float | None
    # Added by matchup enrichment loop
    matchup_score: float
    next_opponent: str | None
    attack_matchup: NotRequired[float]
    defence_matchup: NotRequired[float]
    form_differential: NotRequired[float]
    position_differential: NotRequired[float]
    positional_fdr: NotRequired[float]
    appearances: NotRequired[int]
    matchup_avg_3gw: NotRequired[float]
    form_trajectory: NotRequired[float]


class WaiverTarget(TypedDict):
    player_name: str
    id: int
    price: int
    position: str
    team_id: int
    team_name: str
    team_short: str
    form: float
    ppg: float
    minutes: int
    status: str
    xGI_per_90: float
    waiver_score: float
    reasons: list[str]
    chance_of_playing: NotRequired[int]
    chance_of_playing_next_round: NotRequired[int]
    npxG_per_90: NotRequired[float]
    xGChain_per_90: NotRequired[float]


class EnrichedPlayer(TypedDict):
    id: int
    player_name: str
    first_name: str
    second_name: str
    team_id: int
    position: str
    total_points: int
    ppg: float
    form: float
    status: str
    news: str
    chance_of_playing: NotRequired[int]
    goals_scored: int
    assists: int
    clean_sheets: int
    minutes: int
    expected_goals: float
    expected_assists: float
    team_name: str
    team_short: str
    xGI_per_90: float
    dc_per_90: float
    # Added by callers after understat lookup
    npxG_per_90: NotRequired[float | None]
    xGChain_per_90: NotRequired[float | None]
    penalty_xG_per_90: NotRequired[float | None]
    availability: NotRequired[str]
    injury_news: NotRequired[str]
    matchup_avg_3gw: NotRequired[float]
    positional_fdr: NotRequired[float]
    appearances: NotRequired[int]
    form_trajectory: NotRequired[float]
