## Squad Constraints
- **15 players:** 2 GK, 5 DEF, 5 MID, 3 FWD
- **Max 3 from any team**
- **Valid formations:** 3-4-3, 3-5-2, 4-3-3, 4-4-2, 4-5-1, 5-3-2, 5-4-1
- **Full stats required:** Run `fpl player "{name}" -f` for every player in the final squad and every serious alternative considered. Never recommend without data.

## pFDR (Positional FDR)
- **ATK column:** Use for FWD and MID (based on opponent's defensive strength)
- **DEF column:** Use for DEF and GK (based on opponent's offensive strength)
Use the correct column everywhere fixture difficulty is referenced.
Label as **pFDR** (not FDR) in all output - this is position-aware and blends team strength with opponent weakness, so values differ from the general FDR shown by `fpl fixtures`.

**When the prompt carries a Data Quality caveat, pFDR is indicative, not decisive.** The caveat means the team ratings under every number here are stale, absent, describing last season's league, or unable to separate any two clubs - the columns still render, they just are not measuring what they claim. Keep quoting pFDR in fixture runs and rationales, but do not let a fixture run alone win a squad slot over a better player; lead on underlying stats, minutes and preview intel, and say in the Why column that fixtures were discounted. A season-start build will usually carry this caveat - there are no current-season results to rate teams on yet - so treat pre-season fixture difficulty as a tiebreak rather than a driver. With no caveat present, weight pFDR normally.

## Fixture Format
`OPP (H/A)` - UPPERCASE = easy (pFDR 1-2), lowercase = hard (pFDR 4+), Title Case = neutral.
DGW: `OPP1 (H/A), OPP2 (H/A) (DGW)`. BGW: `BGW`.
Condensed fixture run: `GW30: SOU(2.1) GW31: BGW GW32: ars(4.8) GW33: BOU(2.3)+eve(3.1) DGW GW34: TOT(3.4)`.
Numbers in parentheses are pFDR values.

## Starting From Scratch
This is not transfer optimisation. You are building the best possible squad given a budget and player pool. The current squad is irrelevant as a constraint - any player in the game is available.
After building, note how many of the user's current players (if known) appear in the recommendation as a data point, not a goal.

## Team Exposure
- Avoid 3 from one team in starting XI unless fixtures are exceptional (DGW, pFDR <= 2)
- Spread across fixture swings: if multiple teams share a BGW, don't overload all of them
- Double-ups acceptable for teams with strong fixture runs - but a conscious, stated choice

## Selection
- **Captain (Classic only):** Highest expected points. DGW players favoured. Home preferred.
- **Vice-Captain (Classic only):** Second-highest ceiling. Different team from captain.
- **Bench order:** Most likely to come on first. Highest expected points among bench.

## Value-for-Money
Available in `fpl player --format json` output when Understat data exists for the player:
- **quality_score** (0-100): Player output quality normalised against a **position-specific** ceiling. Treat as an **elite-within-position** index: a GK or DEF scoring 90 is "top of their position", not "better than an 85-rated MID/FWD". Raw quality is attenuated by a position multiplier (GK 0.7, DEF 0.85) and DEF/GK use dedicated signal sets (`without_xgi()` / `for_gk()`), so cross-position comparisons of `quality_score` are not meaningful. Form and PPG weighted heavily.
- **quality_per_m** (quality_score / price per GBPm): Within-position budget efficiency. Higher = more output per pound.
- When choosing between similarly-ranked candidates at the same position, prefer higher `quality_per_m` to free budget for other slots.
- `quality_per_m` is not meaningful for cross-position comparison (positional ceilings differ).
- Both fields are `null` for players without Understat data - do not penalise or exclude these players, use other signals instead.
- **Free Hit note:** Free Hit uses `fpl allocate --horizon 1 --bench-discount 0.01` for a fixture-adjusted, single-GW optimal squad. The solver output is the primary starting point.

## Solver Integration
When `fpl allocate` output is available (Classic modes):
- The solver provides the **budget-optimal starting squad** based on fixture-adjusted quality scores over the planning horizon
- Treat it as your starting point, not gospel. The solver optimises on quantitative signals only
- Your role is **qualitative adjustment**: injury return timing, ownership differentials for rank chasing, fixture nuances the model doesn't capture (e.g., newly promoted teams without rating history), and eye-test factors
- When deviating from the solver's picks, **state why** - e.g., "Solver picks Player X but they're returning from a 6-week injury with no match fitness"
- The solver's formation is a suggestion. If a different formation produces a more compelling XI, use it
- Captain schedule from the solver is informational only - evaluate captaincy with the full context available to you
- Use `effective_price` from allocator output for budget tables and price display - this reflects sell prices for owned players on Wildcard/Free Hit

---

## Preview Intel
Hand-curated season previews, read via `fpl intel --format json`. Optional: most
setups have none, and the pipeline is unchanged when the gate is `none`.

- **Scope.** Intel governs *minutes and role* — who starts, who is injured, who
  changed position, who takes set pieces. It never governs *quality*. If intel
  and a stat disagree about how good a player is, the stat wins.
- **Gate.** The Phase B3 gate (`metadata.coverage.usable_as`) decides what is
  permitted. Under `negative_filter_only`, intel may only downgrade a player;
  promoting on partial coverage favours whichever teams were written up.
- **Confidence.** `metadata.section_confidence` weights each kind of claim.
  A projected XI at `0.5` has been half-superseded by real minutes; treat it as
  a tiebreaker, not a reason.
- **Attribution.** Say whose opinion it is. "Transfer Flow projects him
  starting", never "he is starting".
- **Silence is not a verdict.** A player with no intel is not worse than one
  with intel. Most teams may have no entry at all.
- **The solver is blind to it.** `fpl allocate` and pFDR never see intel, by
  design — a newsletter must not silently move the optimiser. Any deviation from
  the solver justified by intel has to be stated.
- **Season start is where it matters.** With no current-season data, intel is
  the only source that sees a projected starter with no Premier League history.
  By GW7 projected XIs have expired; by GW13 everything has. That is intended.

## Mode: Wildcard
- **Budget:** Total squad value + bank (from squad sell-prices data)
- **Horizon:** Derived from chip plan. If a subsequent wildcard is planned (beyond the current squad reset), horizon = GWs until that wildcard. Otherwise defaults to 6 GWs. See A1b for derivation logic.
- Prioritise fixture runs over single-GW form
- Balance premium assets (1-2 big hitters) with mid-price performers and enablers
- Bench quality matters - need 4 genuine rotation options
- Avoid teams with upcoming BGWs unless explicitly planning around them
- Consider price trajectory (players likely to rise/fall)
- Team coverage: spread across fixture swings to avoid correlated blanks

## Mode: Free Hit
- **Budget:** Total squad value + bank
- **Horizon:** This gameweek only (squad reverts next GW)
- Maximise expected points for this single GW
- Bench barely matters - pick cheapest playing options to free budget for starters
- Heavily weight captain pick. Build the squad around the captaincy choice.
- Target DGW players if applicable - double the opportunity
- High-risk differentials are acceptable (one-week punt)
- Ignore price changes (squad reverts)
- Prioritise home fixtures, DGW players, set-piece takers

## Mode: Season Start (Classic)
- **Budget:** GBP100.0m
- **Horizon:** Derived from chip plan. If a subsequent wildcard is planned, horizon = GWs until that wildcard. Otherwise defaults to 8 GWs (weight early fixtures more heavily).
- Template picks: proven premium assets with fixture-proof floor
- Value picks: cheap players with starting spots, likely to rise in price early
- Enablers: GBP4.0-4.5m bench players who play (for emergency cover and bench boost potential)
- Price change potential matters - early risers fund future transfers
- Historic performance across seasons, not just most recent (use previous season points, PPG, xGI if available)
- Nailed-on status critical - new signings and rotation risks should be flagged
- Avoid players with difficult early fixture runs unless they're fixture-proof premiums

## Mode: Season Start (Draft)
- **No budget constraint** - pick order determines availability
- **Horizon:** Derived from re-draft timing. If a re-draft is scheduled, horizon = GWs until that re-draft. Otherwise defaults to full season (37 GWs). See A1b for derivation logic.
- **Output format:** Ranked player list by draft value, NOT a fixed 15-player squad
- Early rounds: premium players with highest ceiling AND floor
- Mid rounds: mid-price players with favourable early fixtures and minutes security
- Late rounds: rotation-proof starters, set-piece takers, fixture-run punts
- Positional scarcity: fewer elite FWDs than MIDs - draft premium FWDs early
- Minutes security paramount - avoid rotation risks, especially in later rounds
- Historic season points and consistency (PPG, total minutes) weigh heavily
- Consider snake draft dynamics: if picking late in round 1, early in round 2 - plan pairs

## Mode: Re-draft
- **No budget constraint** - all players return to the pool; pick order determines availability
- **Output format:** Ranked player list by draft value, NOT a fixed 15-player squad
- **Horizon:** Derived from re-draft timing. If another re-draft is scheduled, horizon = GWs until that re-draft. Otherwise horizon = remaining GWs in season (`38 - current_gw`). See A1b for derivation logic.
- Current-season form, xGI, and points are the primary ranking inputs (unlike season start which relies on historical data)
- Early rounds: in-form premium assets with strong remaining fixture runs
- Mid rounds: consistent performers with minutes security
- Late rounds: rotation-proof starters, set-piece takers
- Positional scarcity: fewer elite FWDs than MIDs - draft premium FWDs early
- Minutes security paramount - avoid rotation risks
- Consider snake draft dynamics (same as Season Start Draft)
