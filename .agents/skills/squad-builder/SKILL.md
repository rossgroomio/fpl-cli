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
<!-- CLI commands composed: status, fdr, stats, price-history, chips, captain, waivers, history, player, squad sell-prices, intel -->
<!-- Showcase skill - adapt output paths and supplementary data sources for your setup -->

# Squad Builder
**Environment:** All `fpl` commands require:
`cd "$FPL_CLI_DIR" && source .venv/bin/activate`

<!-- ADAPT: Phase E below calls gw-prep's `normalise_entities.py` and needs two substitutions. `[YOUR_PYTHON]` is the interpreter with `fpl_cli` importable -- `python3` once the venv above is active, or an absolute path to that venv's binary (e.g. `$FPL_CLI_DIR/.venv/bin/python`) if invoked without activating it; same substitution as gw-prep/SKILL.md's `[YOUR_PYTHON]` note. `[YOUR_SKILLS_DIR]` is the directory containing gw-prep, squad-builder and update-gw-prep as siblings (e.g. `$FPL_CLI_DIR/.agents/skills` in a checkout, or wherever the three skills are installed side by side) -- the parent of this skill's own `${CLAUDE_SKILL_DIR}`, which cannot reach a sibling skill's `scripts/` directly. -->

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
1. Run `fpl status --format json` for current GW (N). Extract `metadata.format` (`"classic"`, `"draft"`, or `"both"`) and `metadata.season` (see below)
2. Run `fpl chips --format json` (classic only - skip if format is `"draft"`). Store the full response:
   - `planned`: all planned chips with their GWs (e.g., `[{chip: "wildcard", gameweek: 35}, {chip: "bboost", gameweek: 34}]`)
   - `available`: chips still available to play
   - Check for planned wildcard/freehit on GW N specifically for mode auto-detection
3. If chip found on current GW -> confirm: "Wildcard planned for GW{N}. Run in wildcard mode? [Y/n]"
4. If pre-season / GW1 -> suggest season-start mode
5. If no mode resolved -> ask user

**Format validation:** Wildcard, Free Hit, and Season Start Classic modes require `metadata.format` to be `"classic"` or `"both"`. Season Start Draft and Re-draft require `"draft"` or `"both"`. If the mode doesn't match the configured format, warn the user: "Mode {mode} requires {classic|draft} format, but your config is {format}-only."

**Season label (all modes):** `{season}` is `metadata.season` from `fpl status --format json` -- the hyphenated label, e.g. `"2026-27"`. Every file this skill writes lives under `[YOUR_OUTPUT_DIR]/{season}/` (Phase E), because those filenames carry no season of their own. An explicit mode skips A1's status call, so read `{season}` from the Phase B run instead. Never hardcode it: a hardcoded label silently rots at the July rollover, which is the failure this partition exists to prevent.

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
- `fpl status --format json` (if not already run in A1 -- this is where an explicit-mode run picks up `metadata.season`)
- `fpl fdr --format json` -- the fixture-difficulty analysis: `data.fdr_by_team` and the positional ATK/DEF split every position agent is told to read, plus `data.ratings_warning`. Store as `pfdr`
- `fpl fdr --blanks --format json` -- the confirmed and predicted blank/double-GW schedule. Store as `blanks_schedule`

**Both, not either.** `--blanks` bypasses the fixture agent for a schedule-only payload: no `fdr_by_team`, no ATK/DEF columns, no `ratings_warning`. A build that issues only that call hands its position agents a "pFDR" block containing no pFDR, and never learns what the ratings under it were worth.

Unlike gw-prep, the analysis call here does **not** take `--my-squad`: this skill is replacing the 15, so exposure of the outgoing squad is noise, and the season-start modes have no squad to resolve at all. The cost is that `data.predictions_stale` and `data.prediction_warnings`, which the command fills in only once a squad resolves, are absent from `pfdr` -- so `blanks_schedule`'s `metadata.warnings` is the prediction-quality channel for this skill. Do not "harmonise" the flag back in.

**Read the rating-quality signals before any of this reaches a position agent.** Fixture difficulty is only as good as the team ratings underneath it, and the failure modes are invisible in the output -- ratings left over from last season, none at all, ratings still naming relegated clubs, ratings that separate no two teams, pre-season estimates standing in for results. Each renders a pFDR table that looks like ordinary analysis, which is exactly the risk when the whole squad is being picked off it.

| Payload | Field | Fires when |
|---|---|---|
| `pfdr` | `metadata.custom_analysis` | `false` -- custom analysis is off, so this is raw 1-5 FPL API difficulty (`data.easy_fixture_runs` only): no ATK/DEF split, no team ratings, and so no `ratings_warning` to read. Remedy is `fpl init` |
| `pfdr` | `data.ratings_warning` | non-null |
| `blanks_schedule` | `metadata.warnings` | non-empty; `fixture_predictions_stale` is the code this command raises |

Set `data_caveat` -- one `- ` bullet per signal that fired, empty when none did, each message **quoted verbatim** because it names its own remedy (`fpl ratings update`, `fpl init`) and a paraphrase drops it. It is inlined into every Phase C position prompt and the Phase D assembler prompt, and written to the output's Data Quality section in Phase E. A season-start build is the run most likely to fire one -- there are no current-season results to rate teams on yet -- and the one whose output is hardest to revisit later.

### Mid-season (Wildcard / Free Hit / Re-draft)
<!-- Classic-only commands (captain, chips) - skip if format is "draft" -->
- `fpl chips --format json` (Classic only)
- `fpl captain --global --format json` (Classic only. **`--global` is required** - squad-builder is rebuilding the 15, so squad-only captain analysis is incoherent. Global ranks all eligible players for the GW.)
- `fpl allocate --sell-prices /tmp/sell-prices.json --horizon {derived_horizon} --format json` (Classic only. `--sell-prices` provides accurate sell-price budgeting - budget auto-computed from sell values + bank. Horizon from A1b derivation. Free Hit: add `--bench-discount 0.01` to minimise bench spend. Non-Free-Hit: add `--bench-boost-gw {bb_gw}` when BB is planned and falls within horizon. Add `--free-transfers {N}` when FT count is available from sell-prices. Provides the mathematically optimal starting squad for the sub-agent to review and adjust)
- **Minutes floor** -- an early wildcard or free hit is a mid-season run at a gameweek where no player has 450 minutes yet: after `N-1` completed gameweeks the ceiling is `(N - 1) * 90`, so a fixed floor filters out everybody and every query below comes back with `"data": []`. Compute the floor from the current GW (`N`, Phase A1) and substitute it:

  ```
  mins_pos  = min(450, (N - 1) * 45)     # cap reached at GW11
  mins_form = min(315, (N - 1) * 45)     # cap reached at GW8
  ```

  `(N - 1) * 45` is half the minutes the season has made possible so far; from GW8/GW11 the caps bind and behaviour matches the old fixed thresholds. When a query still returns nothing, say so where you use it -- an empty list is a finding, not an absence of data.
- **Early-season sort (`N <= 5`)** -- the floor decides who is in the list; the sort decides the order, and `expected_goal_involvements`, `form`, `total_points` and `points_per_game` over one or two matches order mostly on sample. Rank on `ep_next` instead -- FPL's own projection for the coming gameweek, the same field Phase C step 7 weighs in this window. It is worth the swap because it scales by chance of playing (`--available-only` keeps doubtful players, and this demotes them rather than leaving them at raw form) and because it reorders on fixtures once FPL's fixture factor moves off 1.0. **It is not a second opinion this early:** `ep_next` tracks `form` almost exactly in the opening gameweeks -- at GW4 every row of all four positional shortlists had `ep_next == form`, in the order a `form` sort gives -- so read the swap as availability scaling applied to a form sort, not as a projection replacing an observation. `fpl stats -v -s quality_score` is the ordering that genuinely blends last season's pedigree that early; step 7 governs how far to trust it. Substitute `{rank_*}` below:

  | Placeholder | `N <= 5` | `N >= 6` |
  |---|---|---|
  | `{rank_mid}` | `ep_next` | `expected_goal_involvements` |
  | `{rank_fwd}` | `ep_next` | `form` |
  | `{rank_def}` | `ep_next` | `total_points` |
  | `{rank_gk}` | `ep_next` | `points_per_game` |
  | `{rank_form}` | `ep_next` | `form` |

  Only the ordering changes: every record carries `form`, `expected_goal_involvements`, `total_points`, `points_per_game` and `ep_next` whatever the sort field is. The `quality_per_m`, cheapest and transfer-momentum queries keep their sorts: cheapest ranks on price and transfer momentum on this week's market, neither of which is a small-sample measure, and step 7 already governs how far to trust a quality score this early. **Carry the sort you used into every label that quotes these outputs** (Phase C step 4, Phase D step 4), so an agent reading `=== fpl stats: MID shortlist ===` knows what ordered it.
- `fpl stats -p MID -s {rank_mid} --min-minutes {mins_pos} -n 20 --available-only --format json`
- `fpl stats -p FWD -s {rank_fwd} --min-minutes {mins_pos} -n 15 --available-only --format json`
- `fpl stats -p DEF -s {rank_def} --min-minutes {mins_pos} -n 15 --available-only --format json`
- `fpl stats -p GK -s {rank_gk} --min-minutes {mins_pos} -n 8 --available-only --format json`
- `fpl stats --value -p MID -s quality_per_m --min-minutes {mins_pos} -n 15 --available-only --format json` (underpriced mids by underlying performance per £m)
- `fpl stats --value -p FWD -s quality_per_m --min-minutes {mins_pos} -n 15 --available-only --format json`
- `fpl stats --value -p DEF -s quality_per_m --min-minutes {mins_pos} -n 15 --available-only --format json`
- `fpl stats --value -p GK -s quality_per_m --min-minutes {mins_pos} -n 8 --available-only --format json`
- `fpl stats -s now_cost -r --min-minutes {mins_pos} -n 15 --available-only --format json` (cheapest playing options)
- `fpl stats -s {rank_form} --min-minutes {mins_form} -n 20 --available-only --format json` (leaders across positions)
- `fpl stats -s transfers_in_event -n 15 --format json` (transfer momentum)
- `fpl price-history --sort price_slope -n 30 --format json` (season price trajectory - non-blocking, skip if command fails)
- `fpl intel --format json` (season preview intel, if you keep any. Sections age out by design --
  `metadata.sections_live` lists what still counts - so a near-empty mid-season payload is
  correct, not a failure. Read `metadata.coverage.usable_as` before using any of it: see Phase B3.)
<!-- ADAPT: Add further supplementary data source reads here (reports, newsletters) -->

### Season Start (Classic or Draft)
- `fpl history --format json` (historical career arc data - pts/90 trends, cost trajectory, xGI trends across 3 seasons. Primary ranking input at season start when current-season data is absent.)
- `fpl allocate --budget 100.0 --horizon {derived_horizon} [--bench-boost-gw {bb_gw}] [--free-transfers {N}] --format json` (Classic only. Horizon from A1b derivation. Add `--bench-boost-gw {bb_gw}` when BB is planned and falls within horizon. Add `--free-transfers {N}` when FT count is available. Provides mathematically optimal starting squad for sub-agent review)
- `fpl intel --format json` (season preview intel - **the highest-value source in this mode**.
  At season start `fpl stats` has no current-season data at all, so projected XIs, injuries that
  run into the autumn, and new signings with no Premier League record are things nothing else in
  the pipeline can see. Read `metadata.coverage.usable_as` before using any of it: see Phase B3.)
<!-- ADAPT: Add further pre-season reports or previous season summaries here -->

### Draft (any mode)
- `fpl waivers --format json` (available player pool)
- Note: `fpl waivers` reflects current waiver wire availability. For pre-season drafts before the API has draft league data, fall back to `pfdr` (the `fpl fdr --format json` analysis above, which is where the fixture runs are) and player lookups for rankings.
- For Re-draft: all players return to the pool; `fpl waivers` shows who was previously undrafted (informational only).

Skip missing optional sources gracefully. Store all results for Phase B2.

## Phase B2: Candidate Shortlisting (orchestrator, inline)
No sub-agent needed. The orchestrator already has all Phase B JSON. Extract candidate lists per position:

1. Parse the 4 positional `fpl stats` outputs (MID/FWD/DEF/GK) + 4 `quality_per_m` outputs + form + cheapest
2. Deduplicate players appearing in multiple lists
3. Only if the `fpl intel` response's `metadata.coverage.usable_as` is `full` (the same value
   the Phase B3 gate reads): add any player names from `fpl intel` (or other reports) not
   already in the stats lists. A new signing with no Premier League history is invisible to
   `fpl stats` and `fpl history` but may be a projected starter - this is how such a player
   enters the pool at all. Under `negative_filter_only` or `none`, do **not** add
   intel-sourced names: pool admission is the strongest form of promotion, and the gate
   forbids partial-coverage intel from promoting anyone (see Phase B3).
4. Note which players appear in the `fpl allocate` solver output (JSON field is `web_name`, not `name`)
5. Produce 4 candidate lists:

| Position | Target candidates | Primary stats sources |
|----------|------------------|-----------------------|
| GK | 4-6 | GK shortlist (`{rank_gk}`), GK quality_per_m |
| DEF | 8-10 | DEF shortlist (`{rank_def}`), DEF quality_per_m, cheapest |
| MID | 8-10 | MID shortlist (`{rank_mid}`), MID quality_per_m, cross-positional leaders, transfers_in |
| FWD | 6-8 | FWD shortlist (`{rank_fwd}`), FWD quality_per_m, cheapest |

Each candidate entry: `{name, team, position, in_allocator_squad: bool}`.

For season-start modes, use `fpl history` output as the primary source for candidate identification instead of `fpl stats`.

Store the 4 lists for Phase C.

## Phase B3: Preview Intel Gate (orchestrator, inline)
Decide once, here, how `fpl intel` output may be used. Every downstream agent
follows this decision rather than re-deriving it.

Read `metadata.coverage.usable_as` from the `fpl intel` response:

| `usable_as` | Meaning | What agents may do with it |
|---|---|---|
| `full` | Enough of the league is covered (threshold lives in the service; `metadata.coverage` has the numbers) | Support **or** oppose a pick |
| `negative_filter_only` | Some teams covered, most not | Only downgrade: injuries, rotation risk, "not nailed on". **Never** promote a player. |
| `none` | Nothing loaded, or all aged out | Ignore intel entirely; omit it from Phase C/D prompts |

**Why the gate exists:** with partial coverage the written-up teams carry
"nailed on, takes corners" annotations and the rest carry nothing, so absence of
a flag reads as absence of merit. Promoting on partial intel systematically
favours whichever teams the user happened to write up.

Also note from the same response:
- `metadata.section_confidence` — how much weight each kind of claim still carries
  (`projected_xi: 0.5` means a projected XI is half-superseded by real minutes)
- `metadata.unresolved_players` — names that will not join to FPL data; treat as
  unverified and do not act on them alone

Record the gate decision, coverage figures, and source attributions. Phase D
reports them in the output so the manager can see what influenced the squad.

If `usable_as` is `none`, skip every intel section in the Phase C and D prompts
below. The pipeline behaves exactly as it did before intel existed.

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
| GK | GK shortlist (`{rank_gk}`), GK quality_per_m | DEF | GK entries from allocator |
| DEF | DEF shortlist (`{rank_def}`), DEF quality_per_m | DEF | DEF entries from allocator |
| MID | MID shortlist (`{rank_mid}`), MID quality_per_m | ATK | MID entries from allocator |
| FWD | FWD shortlist (`{rank_fwd}`), FWD quality_per_m | ATK | FWD entries from allocator |

Cross-positional data (leaders, cheapest, transfers_in, price-history, captain) goes to Phase D assembler, not position agents.

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
   === fpl stats: {POSITION} shortlist (sort={this position's rank placeholder}, min-minutes={mins_pos}) ===
   {this position's primary stat output — empty means no available player cleared the floor at GW{N}, not that the query failed}

   === fpl stats: {POSITION} value score ===
   {this position's quality_per_m output}
   Players ranked by underlying performance (Understat xG/xA) per GBPm. High quality_per_m = outperforming price tag.

   === fpl allocate: {POSITION} picks ===
   {only this position's entries from allocator output, including effective_price}
   These are the solver's optimal picks. Use effective_price (sell price for owned, market for new).

   === Owned player sell prices ({POSITION}) ===
   {entries from /tmp/sell-prices.json filtered to this position}
   On Wildcard/Free Hit, owned players cost their sell price, not market price.
   Use sell_price for any owned player in your rankings, even if they aren't an allocator pick.

   === fpl fdr ({ATK|DEF} column) ===
   {`pfdr` data filtered to the relevant column for this position - the ATK/DEF split lives in
   `data.fdr_by_team`, which only the non-`--blanks` call returns}

   === Blank/double GW schedule ===
   {`blanks_schedule` data - confirmed and predicted BGWs/DGWs across the horizon}

   {data_caveat} (omit these lines entirely when `data_caveat` is empty)

   === fpl history (career arcs) ===
   {output from fpl history - season-start modes only}
   ```

   === Season preview intel ({POSITION}) ===
   {entries from `fpl intel` filtered to this position's candidates - omit this block entirely
   when the Phase B3 gate is `none`}
   Usage gate: {full | negative_filter_only} (from Phase B3)
   Section confidence: {metadata.section_confidence}

   Hand-curated pre-season notes on minutes, injuries, role and set-piece duty. This is the only
   input that sees things the stats cannot: a projected starter with no Premier League history, an
   injury that runs into the autumn, a player who lost his place over the summer.

   Rules for using it:
   - It overrides **minutes and role expectations only**. It never overrides a stat. If intel and
     `quality_score` disagree on how good a player is, the stat wins.
   - Under `negative_filter_only`, use it solely to downgrade (injury, rotation risk). Do not
     promote a player on intel under this gate.
   - Attribute, do not assert: "Transfer Flow projects him starting", not "he is starting".
   - Weight by section confidence. `projected_xi: 0.5` means real minutes have half-superseded it.
   - A player with no intel is not a worse player. Most teams may have no entry at all.
   - Flag any pick where intel changed your ranking, with `⚡ Intel: {source} - {reason}`.
<!-- ADAPT: Add further position-relevant report excerpts here -->
   Note: all `--format json` commands return `{command, metadata, data}` envelopes - actual records are in `data`.

5. **Rules excerpt:** Include from `references/rules.md`:
   - Squad Constraints (position slot counts)
   - pFDR (ATK vs DEF column usage, and how a Data Quality caveat changes its weight)
   - Fixture Format
   - Value-for-Money section
   - Solver Integration section
   - Preview Intel section (only when the Phase B3 gate is not `none`)
   - The mode-specific section (Wildcard, Free Hit, Season Start Classic, etc.)
   Do NOT include: Team Exposure, Selection (captain/bench), Starting From Scratch, or output template. Those are for the assembler.

6. **Player lookups:**
   You have Bash access. Always prefix with:
   `cd "$FPL_CLI_DIR" && source .venv/bin/activate`

   Run `fpl player "{name}" -f -H` **in parallel** for all candidates in your list.
   For season-start modes with no current-season data, also use `fpl history` data passed in context.

7. **Scoring:** Score each candidate against the mode-specific criteria from rules. Use `quality_score` and `quality_per_m` when available (null for players without Understat data — don't penalise). **Both fields are elite-within-position: never rank or compare candidates across positions by `quality_score` or `quality_per_m`.** A GK showing 90 is "top of the GK pool", not "better than a MID showing 85" — every position is normalised against its own calibrated ceiling, MID and FWD included. Within a position the numbers are trustworthy **from ~GW6**: elite players in every position read 80+, so a MID at 55 genuinely is mid-tier among MIDs. Only compare within the position you are currently filling. **Before GW10 quality scores are prior-informed estimates**: last season's pedigree is blended in (the JSON carries an `early_season_prior_informed` warning quoting how much of the score is this season's observation), so a quiet-starting elite keeps most of their standing — read them as an estimate, not a measurement, and still weigh `ep_next`, price and role for early wildcards and free hits. If the warning is `early_season_small_sample` instead, last season's history could not be loaded and the scores measure only the opening gameweek(s), where a hot-starting role player can out-read a quiet-starting elite; lean on price and prior-season pedigree then -- and not on `ep_next`, which before ~GW6 tracks `form` and so carries the same small sample the warning is about. The shortlist you were handed is already ordered on `ep_next` in that window -- its label carries the sort -- but that ordering is not a second opinion either: read it as a form ordering with doubtful players scaled down by chance of playing, not as a projection.

8. **Return format:** Return a structured ranked list. Per candidate:
   ```
   {rank}. {name} ({team}) - GBP{effective_price}m
      Form: {form} | PPG: {ppg} | Minutes: {minutes}
      Quality: {quality_score}/100 | Value: {quality_per_m}
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
   composition - ownership differentials and preview intel (Phase B3 gate permitting) may warrant
   a different choice.
   Use effective_price (not price) for budget tables and price display.

   === fpl fdr --format json (full) ===
   {complete `pfdr` output with both ATK and DEF columns}

   === fpl fdr --blanks --format json ===
   {complete `blanks_schedule` output - confirmed and predicted BGWs/DGWs}

   {data_caveat} (omit these lines entirely when `data_caveat` is empty)
   When it is present, the ratings behind every pFDR figure above are unreliable: keep the fixture
   columns in the output, but do not let a fixture run alone decide a slot, and carry the caveat
   into the Data Quality section of the template.

   === fpl stats: cross-positional leaders (sort={rank_form}, min-minutes={mins_form}) ===
   {cross-positional stats output - mid-season only}

   === fpl stats: cheapest playing options ===
   {cheapest stats output - mid-season only}

   === fpl stats: transfer momentum ===
   {transfers_in output - mid-season only}

   === fpl captain --global --format json ===
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

   === Season preview intel (team-level) ===
   {`fpl intel` team-level entries: predicted_finish, team_strength, transfers - omit entirely
   when the Phase B3 gate is `none`}
   Usage gate: {full | negative_filter_only} (from Phase B3)
   Coverage: {N}/20 teams

   Use for team-exposure decisions and captaincy, under the same rules the position agents were
   given: minutes and role only, attribute rather than assert, never override a stat, and never
   promote under `negative_filter_only`.

   Note that `fpl allocate` and pFDR have **not** seen this intel - the solver is deliberately
   blind to it. Any deviation from the solver justified by intel must say so explicitly.
<!-- ADAPT: Add further reports for cross-cutting insights here -->
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
<!-- ADAPT: Set your output directory. Keep the `/{season}/` segment -- it is what stops a new season overwriting the last one's files. -->
Write sub-agent output to:
- Mid-season (Wildcard / Free Hit / Re-draft): `[YOUR_OUTPUT_DIR]/{season}/gw{N}-squad-builder.md`
- Season start: `[YOUR_OUTPUT_DIR]/{season}/season-start-squad.md`

Both names repeat every season -- `season-start-squad.md` carries no gameweek at all -- so the `{season}` directory from A1 is what keeps last season's copy intact. Create it if it does not exist.

Add frontmatter:
```yaml
---
mode: {Wildcard|Free Hit|Season Start Classic|Season Start Draft|Re-draft}
gameweek: {N}
season: {season}
generated: {YYYY-MM-DD}
budget: GBP{X}m
---
```

**Write `data_caveat` (from Phase B) into the output's Data Quality section**, messages verbatim, and repeat the bullets in the confirmation to the user. Omit the section entirely when it is empty. This file outlives the run that made it -- gw-prep Phase A3 embeds a `gw{N}-squad-builder.md` Classic Squad block into its own recommendations days later, and a `season-start-squad.md` is read back for weeks -- so a warning that only reached the terminal is gone by the time anyone acts on the squad.

Then normalise the written file, before confirming to the user:

<!-- ADAPT: `[YOUR_PYTHON]` is the interpreter with `fpl_cli` importable (see gw-prep's `[YOUR_PYTHON]` note under Environment). `[YOUR_SKILLS_DIR]` is the directory containing gw-prep, squad-builder and update-gw-prep as siblings -- the script lives under gw-prep's `scripts/` regardless of which skill calls it. -->
```bash
[YOUR_PYTHON] "[YOUR_SKILLS_DIR]/gw-prep/scripts/normalise_entities.py" --file "[YOUR_OUTPUT_DIR]/{season}/{filename}"
```

Parse stdout as JSON and warn, never block; `.agents/skills/gw-prep/references/entity-normalisation.md` carries the contract, the warning template and the failure handling. It matters most for a `gw{N}-squad-builder.md`, where escaped markdown propagates: gw-prep Phase A3 embeds this file's `## Classic Squad` block into its own recommendations.

Confirm:
> Squad recommendation saved to `[YOUR_OUTPUT_DIR]/{season}/{filename}`

Followed by each `data_caveat` bullet, verbatim, when there are any -- the terminal is where the remedy reaches someone who can run it.
