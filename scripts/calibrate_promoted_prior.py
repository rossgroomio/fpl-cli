#!/usr/bin/env python3
"""Fit the Championship-to-PL conversion in `team_ratings_prior` against real data.

Both constants that place a promoted side on the Premier League scale --
the level (CHAMPIONSHIP_GOALS_*_FACTOR) and the spread
(CHAMPIONSHIP_TRANSFER_COEFFICIENT) -- fall out of one regression per axis:

    y = a + k * z

where, for a team promoted from Championship season s into PL season s+1,

    z = its standardised rate within the Championship in season s
    y = its standardised rate within the Premier League in season s+1

Both sides are standardised in their own league's units, so `k` is the spread
term `_rescale_to_pl` applies and `a` is the level in PL standard deviations --
the same units that function works in. Regressing measured-on-measured is
deliberate: the attenuation from Championship sampling noise is part of what we
want the coefficient to carry, since the input at runtime is equally noisy.

Also reports, per season and per axis, the reliability

    rho = 1 - (mu / n) / Var(observed)

-- the share of the observed cross-team spread that is not Poisson sampling
noise. rho needs a single season, so it is available even where the free tier
serves too little history to fit anything.

rho bounds k from above as k <= sqrt(rho), NOT k <= rho. The retention constant
scales a standard deviation where rho is a variance ratio, and the full
decomposition is

    k = tau * sqrt(rho_ELC * rho_PL)

with rho_PL the reliability of the pool the promoted side is ranked in (a true
gap is a smaller share of a noisy pool's observed sd than of its true one) and
tau the transfer of true Championship standing to true PL standing. Only tau
needs the regression; the two reliabilities are measurable from one season each.

NOTE ON COVERAGE: as of 2026-08, football-data.org's free tier serves only the
CURRENT season -- earlier ones return 403 -- so the regression below cannot run
on a free key at all, since it needs Championship season s alongside Premier
League season s+1. The reliability section still works. Run the regression only
against a paid key, or a cache built up over several seasons.

Usage:
    FOOTBALL_DATA_API_KEY=... python3 scripts/calibrate_promoted_prior.py
    ... --from-season 2015 --to-season 2024 --request-interval 7

Never prints the API key. Reads nothing but football-data.org.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from statistics import mean

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fpl_cli.api.football_data import FootballDataClient  # noqa: E402
from fpl_cli.services.team_ratings_prior import (  # noqa: E402
    _league_matches,
    rate_reliability,
    rate_spread,
    z_score,
)

AXES = ("scored_home", "scored_away", "conceded_home", "conceded_away")


@dataclass
class Rates:
    """One team's per-game rates in one season, with the games behind each."""

    values: dict[str, float]
    home_games: int
    away_games: int

    def games_for(self, axis: str) -> int:
        return self.home_games if axis.endswith("_home") else self.away_games


def aggregate(matches: list[dict]) -> dict[int, Rates]:
    """Per-game rates by football-data team id.

    Keyed on the numeric id rather than the tla: the tla is not unique (both
    Sheffield clubs are served as "SHE"), and unlike the production code this
    never has to join to FPL short names, so it can use the unambiguous key.

    Playoffs are dropped through the same filter production uses. They matter
    more here than there: the regression's whole sample is the three promoted
    teams per season, one of which is the playoff winner, so counting its
    Wembley final as an ordinary home match biases the very constants this
    script exists to fit.
    """
    matches = _league_matches(matches)
    buckets: dict[int, dict[str, list[float]]] = {}

    def bucket(team_id: int) -> dict[str, list[float]]:
        return buckets.setdefault(team_id, {axis: [] for axis in AXES})

    for m in matches:
        home_id, away_id = m.get("home_team_id"), m.get("away_team_id")
        if home_id is None or away_id is None:
            continue
        h, a = bucket(home_id), bucket(away_id)
        h["scored_home"].append(m["home_score"])
        h["conceded_home"].append(m["away_score"])
        a["scored_away"].append(m["away_score"])
        a["conceded_away"].append(m["home_score"])

    rates = {}
    for team_id, data in buckets.items():
        # Strict on purpose, unlike performances_from_samples() in the live
        # ratings path (#138): this fits constants from a completed season, and
        # reliability() below divides the observed spread by a Poisson floor
        # sized from games actually played. Estimating a venue a club never
        # played would feed that fit evidence it never had.
        if not data["scored_home"] or not data["scored_away"]:
            continue
        rates[team_id] = Rates(
            values={axis: mean(data[axis]) for axis in AXES},
            home_games=len(data["scored_home"]),
            away_games=len(data["scored_away"]),
        )
    return rates


def reliability(rates: dict[int, Rates], axis: str) -> tuple[float, float, float]:
    """(observed sd, Poisson noise sd, rho) for one axis across a division.

    The measurement itself is production's, so a correction to the noise-floor
    model lands in both at once -- this script is only worth running if it
    measures the constants the same way the code applying them does.

    Simulated against known planted talent spreads, this recovers rho to within
    ~0.02 above rho = 0.4 and carries a small upward bias below it (it reports
    ~0.07 where the truth is 0), because clamping a negative variance estimate
    at zero can only push the result up. Read a low rho as an upper bound.

    Note this is far better powered than the regression below: rho uses every
    team in the division each season, where the regression sees only the three
    promoted ones.
    """
    mean_rate, sd = rate_spread([r.values[axis] for r in rates.values()])
    games = mean(r.games_for(axis) for r in rates.values()) if rates else 0.0
    noise_var = mean_rate / games if games else 0.0
    return sd, noise_var**0.5, rate_reliability(mean_rate, sd, games)


def standardise(rates: dict[int, Rates], axis: str) -> dict[int, float]:
    """Z-scores within a division, on production's own zero-spread guard."""
    mean_rate, sd = rate_spread([r.values[axis] for r in rates.values()])
    return {tid: z_score(r.values[axis], mean_rate, sd) for tid, r in rates.items()}


def ols(xs: list[float], ys: list[float]) -> tuple[float, float, float, float]:
    """(intercept, slope, slope standard error, r_squared)."""
    n = len(xs)
    if n < 3:
        return 0.0, 0.0, float("nan"), 0.0
    x_bar, y_bar = mean(xs), mean(ys)
    sxx = sum((x - x_bar) ** 2 for x in xs)
    if not sxx:
        return y_bar, 0.0, float("nan"), 0.0
    slope = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)) / sxx
    intercept = y_bar - slope * x_bar
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    sse = sum(r**2 for r in residuals)
    syy = sum((y - y_bar) ** 2 for y in ys)
    se = (sse / (n - 2) / sxx) ** 0.5
    return intercept, slope, se, (1 - sse / syy) if syy else 0.0


async def fetch(fd: FootballDataClient, competition: str, season: int) -> list[dict]:
    return await fd.get_matches(competition=competition, season=season)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-season", type=int, default=2015)
    parser.add_argument("--to-season", type=int, default=2024)
    parser.add_argument(
        "--request-interval",
        type=float,
        default=7.0,
        help="Seconds between requests; the free tier allows 10/min.",
    )
    args = parser.parse_args()

    if not os.environ.get("FOOTBALL_DATA_API_KEY"):
        print("FOOTBALL_DATA_API_KEY is not set.", file=sys.stderr)
        return 2

    seasons = range(args.from_season, args.to_season + 1)
    pl: dict[int, dict[int, Rates]] = {}
    elc: dict[int, dict[int, Rates]] = {}

    async with FootballDataClient() as fd:
        if not fd.is_configured:
            print("FOOTBALL_DATA_API_KEY is not set.", file=sys.stderr)
            return 2
        requests = [(s, c, store) for s in seasons for c, store in (("PL", pl), ("ELC", elc))]
        for index, (season, competition, store) in enumerate(requests):
            matches = await fetch(fd, competition, season)
            if matches:
                store[season] = aggregate(matches)
            print(
                f"  {competition} {season}: {len(matches):>4} matches, "
                f"{len(store.get(season, {})):>2} teams",
                file=sys.stderr,
            )
            # Pace against the free tier's 10/min, but never after the last
            # request -- there is nothing following it to pace against.
            if index < len(requests) - 1:
                await asyncio.sleep(args.request_interval)

    print("\n## Season coverage\n")
    print(f"PL  seasons served: {sorted(pl) or 'none'}")
    print(f"ELC seasons served: {sorted(elc) or 'none'}")
    if not elc:
        print("\nNo Championship data -- the free tier may not cover ELC on this key.")
        return 1

    print("\n## Championship reliability (single-season, no history needed)\n")
    print("rho = share of observed cross-team spread that is not sampling noise.")
    print("k <= rho, since at most all of the signal survives promotion.\n")
    print(f"{'season':>7} " + " ".join(f"{axis:>15}" for axis in AXES))
    print("-" * 71)
    rho_totals: dict[str, list[float]] = {axis: [] for axis in AXES}
    for season in sorted(elc):
        cells = []
        for axis in AXES:
            _, _, rho = reliability(elc[season], axis)
            rho_totals[axis].append(rho)
            cells.append(f"{rho:>15.2f}")
        print(f"{season:>7} " + " ".join(cells))
    print("-" * 71)
    print(
        f"{'mean':>7} "
        + " ".join(f"{mean(rho_totals[axis]) if rho_totals[axis] else 0:>15.2f}" for axis in AXES)
    )

    pairs = [s for s in sorted(elc) if s in pl and (s + 1) in pl]
    print(f"\n## Promotion regression ({len(pairs)} usable season pairs)\n")
    if not pairs:
        print("Need Championship season s plus Premier League seasons s and s+1.")
        print("Reliability above still stands; it needs no history.")
        return 0

    observations: dict[str, list[tuple[float, float]]] = {axis: [] for axis in AXES}
    promoted_count = 0
    for season in pairs:
        promoted = set(pl[season + 1]) - set(pl[season])
        promoted &= set(elc[season])
        promoted_count += len(promoted)
        for axis in AXES:
            z_elc = standardise(elc[season], axis)
            z_pl = standardise(pl[season + 1], axis)
            for team_id in promoted:
                observations[axis].append((z_elc[team_id], z_pl[team_id]))

    print(f"{promoted_count} promoted-team observations across {len(pairs)} seasons.\n")
    print(f"{'axis':<16} {'n':>4} {'k (slope)':>11} {'se':>7} {'level a':>9} {'r2':>6} {'rho':>6}")
    print("-" * 64)
    for axis in AXES:
        xs = [x for x, _ in observations[axis]]
        ys = [y for _, y in observations[axis]]
        intercept, slope, se, r2 = ols(xs, ys)
        rho = mean(rho_totals[axis]) if rho_totals[axis] else 0.0
        print(
            f"{axis:<16} {len(xs):>4} {slope:>11.3f} {se:>7.3f} "
            f"{intercept:>9.3f} {r2:>6.2f} {rho:>6.2f}"
        )

    print("\nk is the whole spread term: divide by sqrt(rho_ELC * PL_POOL_RELIABILITY)")
    print("to recover CHAMPIONSHIP_TRANSFER_COEFFICIENT, the only part of it that")
    print("this regression is needed for. a is the level in PL standard deviations")
    print("-- the calibrated stand-in for CHAMPIONSHIP_GOALS_SCORED/_CONCEDED_FACTOR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
