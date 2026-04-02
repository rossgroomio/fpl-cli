"""Scout agent for FPL expert analysis via deep research."""

from __future__ import annotations

from typing import Any

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.api.fpl import FPLClient
from fpl_cli.api.providers import get_llm_provider
from fpl_cli.cli._context import load_settings
from fpl_cli.models.player import Player, PlayerStatus
from fpl_cli.prompts.scout import SCOUT_SYSTEM_PROMPT, build_scout_user_prompt


class ScoutAgent(Agent):
    """Agent for fetching FPL expert analysis via deep research.

    Uses the configured research provider to get BUY/SELL recommendations
    from web and social sources, mimicking expert FPL analysts.
    """

    name = "ScoutAgent"
    description = "Fetches FPL expert analysis via deep research"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ):
        super().__init__(config)
        self.client = FPLClient()
        self.research_provider = get_llm_provider(
            "research", settings or load_settings()
        )

    async def close(self) -> None:
        await self.client.close()
        await self.research_provider.close()

    def build_position_reference(self, players: list[Player], teams: dict[int, str]) -> str:
        """Build a compact position reference for the prompt.

        Includes players likely to be mentioned: high ownership, good form, or notable.

        Args:
            players: List of all players.
            teams: Mapping of team_id to team short name.

        Returns:
            Formatted position reference string.
        """
        # Filter to relevant players: ownership > 1% OR form > 3 OR minutes > 500
        relevant = [
            p for p in players
            if p.selected_by_percent > 1.0 or p.form > 3.0 or p.minutes > 500
        ]

        # Group by position
        by_position: dict[str, list[str]] = {
            "DEF": [],
            "MID": [],
            "FWD": [],
        }

        for p in relevant:
            pos = p.position_name
            if pos in by_position:
                team_name = teams.get(p.team_id, "???")
                by_position[pos].append(f"{p.web_name} ({team_name})")

        # Format as compact reference
        lines = []
        for pos, names in by_position.items():
            if names:
                # Sort alphabetically for easier lookup
                names.sort()
                lines.append(f"{pos}: {', '.join(names)}")

        return "\n".join(lines)

    def build_unavailable_list(
        self, players: list[Player], teams: dict[int, str]
    ) -> str:
        """Build a list of unavailable players to exclude from recommendations.

        Includes players who are injured, suspended, or have low chance of playing.

        Args:
            players: List of all players.
            teams: Mapping of team_id to team short name.

        Returns:
            Formatted unavailable players string.
        """
        unavailable = []

        for p in players:
            # Skip players with very low ownership/form - unlikely to be recommended anyway
            if p.selected_by_percent < 0.5 and p.form < 2.0 and p.minutes < 300:
                continue

            # Check if unavailable
            is_unavailable = (
                p.status
                in (
                    PlayerStatus.INJURED,
                    PlayerStatus.SUSPENDED,
                    PlayerStatus.NOT_AVAILABLE,
                    PlayerStatus.UNAVAILABLE,
                )
                or (p.chance_of_playing_next_round is not None and p.chance_of_playing_next_round <= 25)
            )

            if is_unavailable:
                team_name = teams.get(p.team_id, "???")
                reason = p.news if p.news else f"Status: {p.status.value}"
                # Truncate long injury news
                if len(reason) > 60:
                    reason = reason[:57] + "..."
                unavailable.append(f"- {p.web_name} ({team_name}): {reason}")

        return "\n".join(unavailable) if unavailable else ""

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Fetch FPL expert analysis for the upcoming gameweek.

        Args:
            context: Should contain:
                - 'gameweek': The gameweek number to analyze

        Returns:
            AgentResult with:
            - content_referenced: Full response with citations
            - content_clean: Response with citations removed (for LLM use)
            - citations: List of source URLs
        """
        if not context or "gameweek" not in context:
            return self._create_result(
                AgentStatus.FAILED,
                message="No gameweek specified",
                errors=["Provide gameweek number in context"],
            )

        gameweek = context["gameweek"]

        # Check if API is configured
        if not self.research_provider.is_configured:
            cls = type(self.research_provider)
            self.log_warning(f"{cls.API_KEY_ENV_VAR} not set - skipping scout analysis")
            return self._create_result(
                AgentStatus.FAILED,
                message="Research provider API key not configured",
                errors=[
                    f"Set {cls.API_KEY_ENV_VAR} environment variable. "
                    f"Get your key from {cls.KEY_SETUP_URL}"
                ],
            )

        self.log(f"Fetching FPL expert analysis for GW{gameweek}...")

        # Fetch player data for position reference and unavailable list
        position_reference = ""
        unavailable_players = ""
        try:
            players = await self.client.get_players()
            teams = await self.client.get_teams()
            team_map = {t.id: t.short_name for t in teams}
            position_reference = self.build_position_reference(players, team_map)
            unavailable_players = self.build_unavailable_list(players, team_map)
            self.log(f"Built position reference ({len(position_reference)} chars)")
            if unavailable_players:
                unavailable_count = unavailable_players.count("\n") + 1
                self.log(f"Found {unavailable_count} unavailable players to exclude")
        except Exception as e:  # noqa: BLE001 — best-effort enrichment
            self.log_warning(f"Could not fetch player data: {e}")

        # Get prompts from prompts module
        system_prompt = SCOUT_SYSTEM_PROMPT
        user_prompt = build_scout_user_prompt(
            gameweek, position_reference, unavailable_players
        )

        try:
            result = await self.research_provider.query(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            if not result.content:
                return self._create_result(
                    AgentStatus.FAILED,
                    message="Empty response from research provider",
                    errors=["No content returned from API"],
                )

            # Create clean version via provider post-processing
            content_clean = self.research_provider.post_process(result.content)

            self.log_success(f"Retrieved expert analysis ({len(result.content)} chars)")

            return self._create_result(
                AgentStatus.SUCCESS,
                data={
                    "gameweek": gameweek,
                    "content_referenced": result.content,
                    "content_clean": content_clean,
                    "citations": result.citations,
                    "model": result.model,
                    "usage": {"input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens},
                },
                message=f"Expert analysis retrieved for GW{gameweek}",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to fetch expert analysis: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to fetch expert analysis",
                errors=[str(e)],
            )
