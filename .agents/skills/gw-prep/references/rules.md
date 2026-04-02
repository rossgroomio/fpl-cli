# GW-Prep Analysis Rules

Rules governing how sub-agents should analyse data and produce recommendations.

---

## Transfer Recommendations (Classic -- Transfer Mode)

When `mode = "transfer"`:

1. **Prioritise by horizon** -- rank transfers by expected point gain over the next 3 gameweeks, not just the immediate fixture.
2. **FDR weighting** -- weight upcoming fixture difficulty using the pFDR data. Players facing a run of low-pFDR fixtures (2-3 range) are preferred over those with a single good fixture followed by hard ones.
3. **Form vs fixtures** -- blend recent form (last 4 GW points, minutes) with fixture difficulty. A player in strong form facing medium fixtures can outperform a cold player facing easy ones.
4. **Price trajectory** -- flag players whose price is rising (buy pressure) or falling (sell pressure). Prioritise transfers that protect squad value.
5. **Maximum 3 transfer suggestions** -- rank by priority. For each transfer, state: player out, player in, rationale, net cost, and expected point swing.
6. **Hit threshold** -- only recommend a points hit (-4) if the expected gain across the planning horizon exceeds 8 points.

## Squad-Builder Rules (Classic -- Wildcard/Free Hit Mode)

When `mode = "squad-builder"`:

1. **Full squad optimisation** -- evaluate all 15 slots, not incremental changes.
2. **Budget constraint** -- the total squad must fit within the available budget (selling price of current squad + bank).
3. **Formation flexibility** -- build a squad that supports at least two viable formations (e.g. 3-4-3 and 4-4-2).
4. **Fixture spread** -- avoid more than 3 players from a single team. Distribute across favourable fixture runs.
5. **Captaincy ceiling** -- ensure at least 2 premium captaincy options with high expected output over the chip horizon.
6. **Bench playability** -- bench players should have a realistic chance of returning points (avoid £4.0m non-players).

## Waiver Recommendations (Draft)

1. **Waiver priority order** -- rank targets by expected impact. The top waiver claim should be the highest-confidence improvement.
2. **Positional need** -- prioritise positions where the current squad is weakest (fewest viable starters, worst upcoming fixtures).
3. **Availability likelihood** -- consider whether high-value targets are likely to be claimed by opponents with higher waiver priority.
4. **Fixture run** -- as with classic, weight the next 3 gameweeks of fixtures rather than just the immediate one.
5. **Maximum 5 waiver suggestions** -- for each: player to drop, player to claim, positional context, and fixture rationale.

## Transfer/Waiver Evaluation Script

The `transfer_eval.py` script provides quantitative **Outlook** (multi-GW quality, target score 0-100) and **This GW** (lineup impact, lineup score 0-100) deltas for each IN candidate vs the OUT player. Use these as the baseline for transfer and waiver recommendations.

- **Positive Outlook delta:** the IN candidate is a better long-term hold
- **Positive This GW delta:** the IN candidate is a better starter this week
- **Override only with stated qualitative reasons** (press conference intel, newsletter signals, rotation predictions)
- All existing transfer rules still apply (hit threshold > 8pts, max 3 suggestions, 3-GW horizon priority, affordability check)

## Blended Analysis

For all formats:

1. **Recent form** -- last 4 GW average points, minutes played (flag rotation risks below 60 mins average).
2. **Expected stats** -- where available, prefer xG/xA-based analysis over raw goals/assists. Flag players significantly over- or under-performing their expected stats.
3. **Set-piece involvement** -- note players on corners, free kicks, and penalties as these provide floor-raising opportunities.
4. **Injury/suspension flags** -- check player availability. Never recommend a player flagged as injured or suspended without explicitly noting the risk.

## Momentum Alerts

Flag the following situations prominently:

- **Hot streak**: 3+ consecutive GWs returning 6+ points
- **Cold streak**: 3+ consecutive GWs returning 2 or fewer points
- **Minutes risk**: started fewer than 2 of last 4 matches
- **Price crash**: lost 0.2+ in value over the last 5 days

## pFDR (Positional FDR) Analysis

For each position (GK, DEF, MID, FWD):

1. Identify the 3 best fixture runs over the next 5 GWs (lowest average pFDR).
2. Identify the 3 worst fixture runs (highest average pFDR).
3. Cross-reference with the user's current squad to surface mismatches (e.g. holding defenders with terrible upcoming fixtures).

## Selection Requirements

Every recommendation must include:

- **Player name and team**
- **Position**
- **Upcoming fixtures** (next 3 GWs with opponent and pFDR)
- **Key stat** (the single most compelling number supporting the recommendation)
- **Risk factor** (the primary reason the recommendation could go wrong)

For starting XI selection:
- **11 players exactly:** 1 GK + 10 outfield
- **Valid formations:** 3-4-3, 3-5-2, 4-3-3, 4-4-2, 4-5-1, 5-3-2, 5-4-1
- **Positions are fixed:** Each player's Pos must match their actual position. Never reassign a player to fit a formation.
- **Bench:** 4 players for both Classic and Draft
- **Bench ordering:** Must use the bench-order script output. Do not manually order bench players.

## Team Exposure Management

When making selection decisions, account for team stacking and use pFDR:

**High-risk scenarios (consider benching one):**
- 2+ attackers from a team with ATK pFDR 5+
- 2+ defenders from a team with DEF pFDR 5+
- 2+ players from a team facing a blank GW
- 3 players from any team in an away fixture

**Low-risk/opportunity scenarios:**
- Double-up attackers on team with ATK pFDR 1-2
- Double-up defenders on team with DEF pFDR 1-2
- Triple-up in a DGW (returns spread across both games)

In squad-builder mode, hard-cap at 3 players per team unless there is an exceptional fixture/form case (state it explicitly).

<!-- ADAPT: Adjust exposure thresholds based on your league's scoring system -->

## Lineup Engine Overrides

The starting XI is determined by the lineup engine (`starting_xi.py`). Override only when you have qualitative information the engine cannot access:
- Press conference intel (confirmed fit/injured/rested)
- Newsletter rotation predictions
- Injury updates not yet reflected in FPL data

**Rules:**
- State the reason for every override
- Mark overrides with `⚡ Override: {reason}` in the Selection table
- Do not override based on vague preference - the engine accounts for form, fixtures, availability, and team exposure
- If you override more than 2 players, reconsider whether the engine input (squad grid) was correct

<!-- ADAPT: Adjust override rules based on your data sources and confidence level -->

## Affordability Analysis

If budget data is available (e.g. from `fpl squad sell-prices`), verify affordability for every classic transfer recommendation. Flag any transfer that requires selling another player to fund it.

<!-- ADAPT: Add your own supplementary data sources or rules here -->

## Troubleshooting

Common issues when running analysis:

- **Stale data**: If CLI output looks outdated, re-run `fpl status --format json` to confirm the current GW.
- **Missing players**: If a recommended player does not appear in stats output, use `fpl player "{name}" --format json` for direct lookup.
- **Budget discrepancies**: Cross-check squad sell prices against available budget if transfers seem unaffordable.

<!-- ADAPT: Add project-specific troubleshooting notes here -->
