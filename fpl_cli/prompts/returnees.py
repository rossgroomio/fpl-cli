"""Prompts for the returnee radar's optional AI-search enrichment.

One query per shortlisted player, not one per watchlist. The response has to
carry a source citation to be worth acting on (R16), and a per-player query is
what makes the provider's own citation list attributable to a single player --
a batched query would return one flat citation list for everyone in it.

The response is asked for as a bare JSON object because the caller reads three
fields out of it and nothing else. The prompt insists on `null` over a guess:
the deterministic FPL signal already covers "we do not know", so an invented
date is strictly worse than no date at all.
"""

from __future__ import annotations

from datetime import date

RETURNEE_ENRICHMENT_SYSTEM_PROMPT = """You are a Premier League injury and availability researcher. You establish when a specific flagged player is expected back in matchday contention, using the most recent reporting you can find.

<priorities>
Weight sources in this order:
1. The club's own channels - official injury updates, manager press conferences, club website medical bulletins
2. Established football journalists with club-specific beats, and reputable outlets reporting a named source
3. Aggregated injury trackers (Premier Injuries, Fantasy Football Scout, Physioroom)
Ignore social-media speculation, fan forums, and anything that does not attribute its claim.
</priorities>

<rules>
ALWAYS:
- Prefer reporting from the last 7 days; an older report is only worth stating if nothing newer exists, and then say so in the summary
- Convert a stated timeframe into a calendar date (e.g. "out for another two weeks" from a report dated 1 March becomes 2026-03-15), and say in the summary that the date is derived from a timeframe
- Use `null` for the return date whenever reporting is vague, contradictory, or absent - that is the honest answer and the caller handles it
- Keep the summary to one sentence naming what was reported and by whom

NEVER:
- Guess a date, or infer one from the injury type alone
- Report a return date for a player who has already returned - say so in the summary instead
- Confuse a player with a namesake at another club; the club given in the request is authoritative
</rules>

<output_format>
Reply with a single JSON object and nothing else - no prose before or after, no markdown code fence:

{"expected_return": "YYYY-MM-DD" or null, "summary": "one sentence, naming the source", "confidence": "high" or "medium" or "low"}

confidence is `high` when a club source or press conference states it, `medium` when a reputable outlet reports it, `low` when it is derived from a timeframe or a single unattributed report.
</output_format>"""

RETURNEE_ENRICHMENT_USER_PROMPT_TEMPLATE = """When is {web_name} ({team}, {position}) expected to be available for {team} again?

<player_status>
FPL availability status: {status}
FPL news text: {news}
{news_age}
{chance}
</player_status>

<context>
Today is {today}. The next Premier League gameweek is GW{gameweek}.
The FPL news above is the only timing this tool has, and it is either missing a date or old enough to be worth re-checking - that is why this query is being run. Find out what has been reported since.
</context>

Reply with the JSON object described in your instructions, and nothing else."""


def build_returnee_enrichment_prompt(
    *,
    web_name: str,
    team: str,
    position: str,
    status: str,
    news: str,
    gameweek: int,
    today: date,
    news_age_days: int | None = None,
    chance_of_playing: int | None = None,
) -> str:
    """Build the per-player enrichment query.

    Every field comes from the player pool the run already fetched, so the
    model is told what FPL says rather than left to rediscover it and
    contradict it silently.
    """
    news_age = (
        f"FPL last updated that news {news_age_days} day(s) ago."
        if news_age_days is not None
        else "FPL does not say when that news was last updated."
    )
    chance = (
        f"FPL puts their chance of playing the next match at {chance_of_playing}%."
        if chance_of_playing is not None
        else "FPL states no chance of playing for the next match."
    )
    return RETURNEE_ENRICHMENT_USER_PROMPT_TEMPLATE.format(
        web_name=web_name,
        team=team or "their club",
        position=position,
        status=status,
        news=news or "(no news text)",
        news_age=news_age,
        chance=chance,
        gameweek=gameweek,
        today=today.isoformat(),
    )
