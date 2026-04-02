---
name: squad-builder
model: opus
description: >-
  Build an optimal 15-player FPL squad from scratch. Handles mid-season wildcard,
  free hit, and season-start squad selection for both Classic and Draft formats.
  Use when the user says "build squad", "wildcard squad", "free hit squad",
  "plan my wildcard", "play wildcard", "season start squad", "pre-season squad",
  "first gameweek squad", "draft rankings",
  or when called by gw-prep with --wildcard/--freehit.
compatibility:
  claude-code: full (5 sub-agents in 2 waves via Agent tool)
  codex: full (single sequential pass)
  cursor: full (single sequential pass)
  copilot: full (single sequential pass)
---
<!-- CLI commands composed: status, fdr, stats, price-history, chips, captain, waivers, history, player, squad sell-prices -->
<!-- Showcase skill - adapt output paths and supplementary data sources for your setup -->

# Squad Builder
**Environment:** All `fpl` commands require:
`cd "$FPL_CLI_DIR" && source .venv/bin/activate`

Build-from-scratch squad optimisation. Five modes, one pipeline.

## Execution Strategy

**Claude Code:** Orchestrator handles setup (Phase A), data gathering (Phase B), and candidate shortlisting (Phase B2). Phase C launches 4 parallel position-research agents (one per position). Phase D launches 1 assembly agent that merges results into the final squad. Total: 5 sub-agents across 2 waves.

**Codex / Cursor / Copilot / other agents:** Run all phases sequentially in the same context. Phase B commands can be parallelised if the tool supports it. Phase B2 shortlisting and Phase C position research can be done inline per position. Use the Phase D assembly prompt as a direct instruction rather than dispatching a sub-agent.

---

## Phase A: Context Detection
### A1. Determine Mode
Check args / user input:
- `--wildcard` / `wildcard` -> **Wildcard** (mid-season, budget-constrained, horizon from A1b)
- `--freehit` / `freehit` -> **Free Hit** (mid-season, budget-constrained, single GW)
- `--season-start` -> **Season Start Classic** (GBP100m budget, horizon from A1b)
- `--season-start --draft` or `--draft` -> **Season Start Draft** (no budget, pick-order ranking, horizon from A1b)
- `--redraft` -> **Re-draft** (mid-season, no budget, all players available, pick-order ranking, horizon from A1b)

If no explicit mode:
1. Run `fpl status --format json` for current GW (N). Extract `metadata.format` (`"classic"`, `"draft"`, or `"both"`)
2. Run `fpl chips --format json` (classic only - skip if format is `"draft"`). Store the full response:
   - `planned`: all planned chips with their GWs (e.g., `[{chip: "wildcard", gameweek: 35}, {chip: "bboost", gameweek: 34}]`)
   - `available`: chips still available to play
   - Check for planned wildcard/freehit on GW N specifically for mode auto-detection
3. If chip found on current GW -> confirm: "Wildcard planned for GW{N}. Run in wildcard mode? [Y/n]"
4. If pre-season / GW1 -> suggest season-start mode
5. If no mode resolved -> ask user

**Format validation:** Wildcard, Free Hit, and Season Start Classic modes require `metadata.format` to be `"classic"` or `"both"`. Season Start Draft and Re-draft require `"draft"` or `"both"`. If the mode doesn't match the configured format, warn the user: "Mode {mode} requires {classic|draft} format, but your config is {format}-only."

### A1b. Derive Horizon
After mode is determined, derive the planning horizon. The unified principle: **horizon = GWs until your next squad reset opportunity**.

If `fpl chips --format json` was not run in A1 (explicit mode was provided), run it now (Classic only - skip if format is `"draft"`). Store the full response as described in A1 step 2.

**Classic modes (Wildcard, Free Hit, Season Start Classic):**
- If a **wildcard is planned** and `planned_wc_gw > current_gw`: `horizon = planned_wc_gw - current_gw`. Example: GW32, WC planned GW35 -> horizon 3. The old squad plays GW32-34; the new squad starts from GW35.
- If a **wildcard is planned** but `planned_wc_gw <= current_gw`: stale plan. Warn: "WC planned for GW{N} but current GW is {M} - plan is stale. Consider running `fpl chips remove`." Fall back to mode default.
- If **no wildcard is planned** but one is available: note it for the checkpoint ("You have an unplanned wildcard available"). Fall back to mode default (Wildcard=6, Season Start=8).
- If **no wildcard is planned** and none available: fall back to mode default.
- **Free Hit** overrides all of the above: horizon = 1 regardless of other chip state.

**Draft modes (Season Start Draft, Re-draft):**
- **Season Start Draft:** ask "When is your league's first re-draft? (Enter GW number, or 'none')" -> if re-draft planned: `horizon = first_redraft_gw - 1`. If no re-drafts: `horizon = 37` (full season minus GW1).
- **Re-draft:** ask "Do you have another re-draft scheduled? If so, which GW? (Enter GW number, or 'none')" -> if another re-draft: `horizon = next_redraft_gw - current_gw`. If none: `horizon = 38 - current_gw` (remaining season).

**Mode defaults** (used as fallback when chip state doesn't provide a better answer): Wildcard=6, Free Hit=1, Season Start Classic=8, Season Start Draft=37, Re-draft=`38 - current_gw`.

Also identify **planned bench boost** from chip state:
- If BB is planned and `bb_gw >= current_gw` and `bb_gw < current_gw + derived_horizon`: flag for passthrough to `--bench-boost-gw`
- If BB is planned but falls outside the derived horizon: note "BB planned GW{N} - outside horizon, not passed to solver"

Also identify **free transfers** from the sell-prices JSON (A4):
- Parse `metadata.free_transfers` from `/tmp/sell-prices.json`. If available, flag for passthrough to `--free-transfers {N}`.
- If sell-prices JSON didn't run or field is missing: omit `--free-transfers` flag (solver defaults to 1 FT).

Store: `derived_horizon`, `horizon_source` (e.g., "WC planned GW35", "mode default", "re-draft at GW30"), `bb_passthrough_gw` (int or null), and `ft_passthrough` (int or null).

### A2. Confirm Constraints
Present constraint summary with chip context and confirm:
> **Squad Builder - {Mode} mode**
> - **Gameweek:** {N}
> - **Budget:** {GBPXm / N/A for draft}
> - **Horizon:** {derived_horizon} GWs ({horizon_source})
> - **Format:** {Classic / Draft}
> - **Chips:** {planned chips with GWs, or "None planned"}
> - **BB passthrough:** {"GW{N} - will pass to solver" / "GW{N} - outside horizon, not passed" / "None planned"}
> - **FT passthrough:** {"Solver will receive --free-transfers {N}" / "Not available - solver defaults to 1"}
> - **Available chips:** {available but unplanned chips, if any}
>
> Proceed? (To override horizon, reply with a number instead of confirming)

If user provides a number, use that as horizon for all subsequent phases. Otherwise proceed with derived_horizon.

For Draft format modes, omit chip-related lines (Chips, BB passthrough, Available chips) from the checkpoint - Draft has no chips.

For Season Start Draft mode, also ask: "How many managers in your draft league?" The answer determines snake draft pick pairing calculations in the output.

### A3. Export Sell Prices JSON (Wildcard / Free Hit only)
Run `fpl squad sell-prices --format json > /tmp/sell-prices.json` (add `--refresh` if data is stale). This single command produces the sell-prices file for the allocator and is the source of truth for budget (`metadata.bank` + `metadata.total_sell_value`) and free transfers (`metadata.free_transfers`). No separate Rich-format call is needed.

## Phase B: Data Gathering
Issue all reads and CLI commands in a **single parallel tool-call block**:

### All Modes
- `fpl status --format json` (if not already run in A1)
- `fpl fdr --blanks --format json`

### Mid-season (Wildcard / Free Hit / Re-draft)
<!-- Classic-only commands (captain, chips) - skip if format is "draft" -->
- `fpl chips --format json` (Classic only)
- `fpl captain --format json`
- `fpl allocate --sell-prices /tmp/sell-prices.json --horizon {derived_horizon} --format json` (Classic only. `--sell-prices` provides accurate sell-price budgeting - budget auto-computed from sell values + bank. Horizon from A1b derivation. Free Hit: add `--bench-discount 0.01` to minimise bench spend. Non-Free-Hit: add `--bench-boost-gw {bb_gw}` when BB is planned and falls within horizon. Add `--free-transfers {N}` when FT count is available from sell-prices. Provides the mathematically optimal starting squad for the sub-agent to review and adjust)
- `fpl stats -p MID -s expected_goal_involvements --min-minutes 450 -n 20 --available-only --format json`
- `fpl stats -p FWD -s form --min-minutes 450 -n 15 --available-only --format json`
- `fpl stats -p DEF -s total_points --min-minutes 450 -n 15 --available-only --format json`
- `fpl stats -p GK -s points_per_game --min-minutes 450 -n 8 --available-only --format json`
- `fpl stats --value -p MID -s value_score --min-minutes 450 -n 15 --available-only --format json` (underpriced mids by underlying performance per £m)
- `fpl stats --value -p FWD -s value_score --min-minutes 450 -n 15 --available-only --format json`
- `fpl stats --value -p DEF -s value_score --min-minutes 450 -n 15 --available-only --format json`
- `fpl stats --value -p GK -s value_score --min-minutes 450 -n 8 --available-only --format json`
- `fpl stats -s now_cost -r --min-minutes 450 -n 15 --available-only --format json` (cheapest playing options)
- `fpl stats -s form --min-minutes 315 -n 20 --available-only --format json` (in-form across positions)
- `fpl stats -s transfers_in_event -n 15 --format json` (transfer momentum)
- `fpl price-history --sort price_slope -n 30 --format json` (season price trajectory - non-blocking, skip if command fails)
<!-- ADAPT: Add supplementary data source reads here (reports, newsletters, scout previews) -->

### Season Start (Classic or Draft)
- `fpl history --format json` (historical career arc data - pts/90 trends, cost trajectory, xGI trends across 3 seasons. Primary ranking input at season start when current-season data is absent.)
- `fpl allocate --budget 100.0 --horizon {derived_horizon} [--bench-boost-gw {bb_gw}] [--free-transfers {N}] --format json` (Classic only. Horizon from A1b derivation. Add `--bench-boost-gw {bb_gw}` when BB is planned and falls within horizon. Add `--free-transfers {N}` when FT count is available. Provides mathematically optimal starting squad for sub-agent review)
<!-- ADAPT: Add pre-season reports or previous season summaries here -->

### Draft (any mode)
- `fpl waivers --format json` (available player pool)
- Note: `fpl waivers` reflects current waiver wire availability. For pre-season drafts before the API has draft league data, fall back to `fpl fdr --blanks --format json` and player lookups for rankings.
- For Re-draft: all players return to the pool; `fpl waivers` shows who was previously undrafted (informational only).

Skip missing optional sources gracefully. Store all results for Phase B2.

## Phase B2: Candidate Shortlisting (orchestrator, inline)
No sub-agent needed. The orchestrator already has all Phase B JSON. Extract candidate lists per position:

1. Parse the 4 positional `fpl stats` outputs (MID/FWD/DEF/GK) + 4 `value_score` outputs + form + cheapest
2. Deduplicate players appearing in multiple lists
3. Add any player names from scout reports / previews not already in stats
4. Note which players appear in the `fpl allocate` solver output (JSON field is `web_name`, not `name`)
5. Produce 4 candidate lists:

| Position | Target candidates | Primary stats sources |
|----------|------------------|-----------------------|
| GK | 4-6 | GK ppg, GK value_score |
| DEF | 8-10 | DEF total_points, DEF value_score, cheapest |
| MID | 8-10 | MID xGI, MID value_score, form, transfers_in |
| FWD | 6-8 | FWD form, FWD value_score, cheapest |

Each candidate entry: `{name, team, position, in_allocator_squad: bool}`.

For season-start modes, use `fpl history` output as the primary source for candidate identification instead of `fpl stats`.

Store the 4 lists for Phase C.

## Phase C: Position Research (4 parallel agents)
Launch 4 position-research agents in a **single parallel Agent tool block**:

```
Agent tool parameters (per agent):
  subagent_type: general-purpose
  model: opus
  description: "Squad builder - {POSITION} research"
```

### Position-agent data routing

| Agent | Stats sources | pFDR column | Allocator picks |
|-------|--------------|------------|-----------------|
| GK | GK ppg, GK value_score | DEF | GK entries from allocator |
| DEF | DEF total_points, DEF value_score | DEF | DEF entries from allocator |
| MID | MID xGI, MID value_score | ATK | MID entries from allocator |
| FWD | FWD form, FWD value_score | ATK | FWD entries from allocator |

Cross-positional data (form, cheapest, transfers_in, price-history, captain) goes to Phase D assembler, not position agents.

### Per-agent prompt template

Include all of this in the prompt field, populated with position-specific data:

1. **Role:** You are researching {POSITION} candidates for a {Mode} squad build. Evaluate every candidate, run `fpl player` lookups, score them, and return a structured ranked list. You do NOT build the full squad - a separate assembly agent handles cross-position optimisation, budget balancing, and formatting.

2. **Context:**
   - Mode: {Wildcard|Free Hit|Season Start Classic|Season Start Draft|Re-draft}
   - Horizon: {derived_horizon} GWs (GW{current}-GW{current + horizon - 1})
   - Format: {Classic | Draft}
   - Position slots: {2 for GK, 5 for DEF, 5 for MID, 3 for FWD}

3. **Candidates:** The Phase B2 candidate list for this position:
   ```
   {name, team, in_allocator_squad: bool} for each candidate
   ```

4. **Position data:** Inline the position-specific Phase B outputs:
   ```
   === fpl stats: {POSITION} shortlist ===
   {this position's primary stat output}

   === fpl stats: {POSITION} value score ===
   {this position's value_score output}
   Players ranked by underlying performance (Understat xG/xA) per GBPm. High value_score = outperforming price tag.

   === fpl allocate: {POSITION} picks ===
   {only this position's entries from allocator output, including effective_price}
   These are the solver's optimal picks. Use effective_price (sell price for owned, market for new).

   === Owned player sell prices ({POSITION}) ===
   {entries from /tmp/sell-prices.json filtered to this position}
   On Wildcard/Free Hit, owned players cost their sell price, not market price.
   Use sell_price for any owned player in your rankings, even if they aren't an allocator pick.

   === fpl fdr --blanks ({ATK|DEF} column) ===
   {pFDR data filtered to the relevant column for this position}

   === fpl history (career arcs) ===
   {output from fpl history - season-start modes only}
   ```
   <!-- ADAPT: Add position-relevant scout report excerpts here -->
   Note: all `--format json` commands return `{command, metadata, data}` envelopes - actual records are in `data`.

5. **Rules excerpt:** Include from `references/rules.md`:
   - Squad Constraints (position slot counts)
   - pFDR (ATK vs DEF column usage)
   - Fixture Format
   - Value-for-Money section
   - Solver Integration section
   - The mode-specific section (Wildcard, Free Hit, Season Start Classic, etc.)
   Do NOT include: Team Exposure, Selection (captain/bench), Starting From Scratch, or output template. Those are for the assembler.

6. **Player lookups:**
   You have Bash access. Always prefix with:
   `cd "$FPL_CLI_DIR" && source .venv/bin/activate`

   Run `fpl player "{name}" -f -H` **in parallel** for all candidates in your list.
   For season-start modes with no current-season data, also use `fpl history` data passed in context.

7. **Scoring:** Score each candidate against the mode-specific criteria from rules. Use quality_score and value_score when available (null for players without Understat data - don't penalise).

8. **Return format:** Return a structured ranked list. Per candidate:
   ```
   {rank}. {name} ({team}) - GBP{effective_price}m
      Form: {form} | PPG: {ppg} | Minutes: {minutes}
      Quality: {quality_score}/100 | Value: {value_score}
      Fixtures (next {horizon}): {condensed fixture run}
      Flags: {injury/suspension/rotation risk, if any}
      Allocator pick: {yes/no}
      Owned: {yes (sell price GBP{X}m) / no}
      Rationale: {1-2 sentences on why this rank}
   ```
   **Pricing:** For owned players, use `sell_price` from the sell-prices data as `effective_price`. For non-owned players, use market price. This applies even when the player is not an allocator pick.
   Rank by recommendation strength for this position. Do NOT write to any file.

### Fallback
If a position agent fails or times out, the orchestrator proceeds to Phase D with the allocator's picks for that position as the fallback candidate list. The assembler treats allocator picks as pre-validated when no position-agent ranking is available.

## Phase D: Squad Assembly (1 agent)
Launch after all Phase C agents return (or fail with fallback):

```
Agent tool parameters:
  subagent_type: general-purpose
  model: opus
  description: "Squad builder - {mode} assembly"
```

**Prompt structure - include all of this in the prompt field:**

1. **Role:** You are assembling the final squad from pre-researched position rankings. Start from the solver's optimal squad and adjust using the position agents' qualitative rankings. Enforce all cross-position constraints, select captain/bench, and produce the formatted output.

2. **Constraints:**
   - Mode: {Wildcard|Free Hit|Season Start Classic|Season Start Draft|Re-draft}
   - Budget: {GBPXm from squad sell-prices data | GBP100m | N/A}
   - Horizon: {derived_horizon} GWs (GW{current}-GW{current + horizon - 1}). Source: {horizon_source}
   - Format: {Classic | Draft}
   - Planned chips: {list of planned chips with GWs, or "None"}
   - BB passthrough: {"Solver received --bench-boost-gw {N}" / "BB planned GW{N} but outside horizon" / "None"}
   - Free transfers: {N banked FTs. Context: the manager can course-correct {N} picks after this squad reset without taking hits. The solver received `--free-transfers {N}` which applies temporal discounting (more FTs = solver weights near-term GWs more heavily). Omit if not available.}

3. **Position research results:** Inline all 4 position-agent outputs:
   ```
   === GK research ===
   {GK agent's ranked candidate list, or "Agent failed - use allocator GK picks below"}

   === DEF research ===
   {DEF agent's ranked candidate list, or "Agent failed - use allocator DEF picks below"}

   === MID research ===
   {MID agent's ranked candidate list, or "Agent failed - use allocator MID picks below"}

   === FWD research ===
   {FWD agent's ranked candidate list, or "Agent failed - use allocator FWD picks below"}
   ```

4. **Cross-cutting data:** Inline Phase B outputs not sent to position agents:
   ```
   === fpl allocate (solver-optimal full squad) ===
   {complete allocator output with all positions and effective_price}
   This is the mathematically optimal squad from the ILP solver. Use it as your starting point -
   review and adjust using position-agent rankings for qualitative factors the solver doesn't
   capture (injury timing, ownership differentials, fixture nuances, eye-test).
   Explain any deviations from the solver's picks.
   For Free Hit, treat captain selection as a first-order decision that may override solver
   composition - ownership differentials and scout intelligence may warrant a different choice.
   Use effective_price (not price) for budget tables and price display.

   === fpl fdr --blanks --format json (full) ===
   {complete pFDR output with both ATK and DEF columns}

   === fpl stats: form (cross-positional) ===
   {form stats output - mid-season only}

   === fpl stats: cheapest playing options ===
   {cheapest stats output - mid-season only}

   === fpl stats: transfer momentum ===
   {transfers_in output - mid-season only}

   === fpl captain --format json ===
   {captain output - Classic only, mid-season only}

   === fpl price-history: season price trajectory ===
   {price-history output, or "Not available" if command failed}
   Field guide (all prices in 0.1m units, so 130 = GBP13.0m):
   - price_change: total rise/fall from season start (positive = risen)
   - price_slope: rate of price change per GW (higher = rising faster, negative = falling)
   - price_acceleration: whether rises/falls are speeding up (positive) or slowing (negative)
   - transfer_momentum: net transfers (in minus out) over last 5 GWs (positive = managers buying)
   Use price_slope + transfer_momentum together for price direction signals.
   When metadata.is_stale is true, only price_change is reliable; slope/accel/momentum are null.

   === fpl history (career arcs) ===
   {output from fpl history - season-start modes only}
   ```
   <!-- ADAPT: Add scout reports for cross-cutting insights here -->
   Note: all `--format json` commands return `{command, metadata, data}` envelopes - actual records are in `data`.
   (Include all available sources, "Not available" for missing ones.)

5. **Rules:** Include the full contents of `references/rules.md` with this override prepended:
   > **ASSEMBLER OVERRIDE - "Full stats required" rule:** The position-research agents (Phase C) have already run `fpl player` for every candidate and distilled the results into the ranked lists above. You do NOT need to re-run any `fpl player` lookups. The position research results ARE the full stats. Use the data provided - do not duplicate their work.

   The assembler needs all other rules including Team Exposure, Selection (captain/bench), and Starting From Scratch.

6. **Output template:** Include the relevant section of `references/output-template.md`:
   - Wildcard/Free Hit/Season Start Classic -> "Classic Squad" section
   - Season Start Draft -> "Draft Rankings" section
   - Re-draft -> "Draft Rankings" section (substitute current-season stats for last-season columns)

7. **Assembly process:** This is a data synthesis and formatting task, not a research task. Do not run CLI commands.
   a. Start from the solver's optimal squad as the baseline
   b. Review each position against the position-agent's ranked candidates
   c. When deviating from the solver, state why (e.g., "Solver picks X but position agent flags returning from injury")
   d. Enforce cross-cutting constraints: budget not exceeded, max 3 per team, valid formation
   e. Select captain and vice-captain (Classic only) - different teams
   f. Order bench by likelihood to come on and expected points
   g. Identify 1-2 alternatives per position slot
   h. Validate final squad: formation legal, budget balanced, team limits respected

8. **Return:** The complete squad recommendation as text, formatted per the output template. Do NOT write to any file.

## Phase E: Output
<!-- ADAPT: Set your output directory -->
Write sub-agent output to:
- Mid-season (Wildcard / Free Hit / Re-draft): `[YOUR_OUTPUT_DIR]/gw{N}-squad-builder.md`
- Season start: `[YOUR_OUTPUT_DIR]/season-start-squad.md`

Add frontmatter:
```yaml
---
mode: {Wildcard|Free Hit|Season Start Classic|Season Start Draft|Re-draft}
gameweek: {N}
generated: {YYYY-MM-DD}
budget: GBP{X}m
---
```

Confirm:
> Squad recommendation saved to `[YOUR_OUTPUT_DIR]/{filename}`
