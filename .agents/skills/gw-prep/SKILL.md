---
name: gw-prep
description: >
  Generate gameweek preparation recommendations for FPL classic and draft leagues.
  Runs data gathering via fpl-cli, then dispatches parallel sub-agent analysis for
  each league format. Use when the user asks to prepare for the next gameweek,
  get transfer/waiver recommendations, or review their squad.
model: opus
compatibility:
  claude-code: full (parallel Classic + Draft sub-agents via Agent tool)
  codex: partial (sequential execution - no sub-agent spawning)
  cursor: partial (sequential execution)
  copilot: fallback (sequential execution)
---

<!-- CLI commands composed: status, chips, chips sync, chips timing, fdr, captain, waivers, squad grid, squad sell-prices, price-history, player, stats, intel, returnees -->

# Gameweek Preparation

Generate transfer/waiver recommendations and squad analysis for the upcoming FPL gameweek across classic and draft formats.

## Environment

```bash
cd "$FPL_CLI_DIR" && source .venv/bin/activate
```

## Execution Strategy

**Claude Code:** Launch Phase C sub-agents in parallel (classic + draft simultaneously) using the `Agent tool parameters:` blocks shown in Phase C.

**Codex / Cursor / Copilot / other agents:** Do not attempt to spawn sub-agents. Run Phase C-classic then Phase C-draft sequentially in the same context. Use the same prompts and output templates - just one after the other.

**In all cases** the final output format is identical: Classic section followed by Draft section (or whichever formats are active).

---

## Phase A: Context Detection

### A1 -- Gameweek and Deadline

```bash
fpl status --format json
```

Extract:
- `gameweek` -- the upcoming GW number (N)
- `deadline` -- the transfer deadline timestamp
- `phase` -- current status (e.g. "Fixture day 1 of 2", "Between gameweeks")
- `metadata.format` -- `"classic"`, `"draft"`, or `"both"`. This determines which sub-agents to dispatch and which Phase B commands to run. If format is not present (no entry IDs configured), ask the user.
- `metadata.season` -- the hyphenated season label (e.g. `"2026-27"`). Referred to below as `{season}`.

**Every file this skill reads or writes lives under `[YOUR_OUTPUT_DIR]/{season}/`.** Report filenames carry the gameweek but no season, so a flat output directory lets 2026-27's GW21 file overwrite 2025-26's. Take `{season}` from this command rather than hardcoding it -- a hardcoded label silently rots at the July rollover, which is the failure this partition exists to prevent. `fpl` writes its own reports to the same season directory.

### A1.5 -- Chip Status

```bash
fpl chips sync
fpl chips --format json
```

`chips sync` ensures the local chip plan reflects any changes made via the FPL website. Run it silently before reading chip status.

Extract active chip status for GW N:
- **Wildcard** active → `mode = "squad-builder"`, `active_chip = "wildcard"`
- **Free Hit** active → `mode = "squad-builder"`, `active_chip = "freehit"`
- **Bench Boost** active → `mode = "benchboost"`, `active_chip = "benchboost"`
- Otherwise → `mode = "transfer"`, `active_chip` is unset

This mode switch affects which rules apply in Phase C sub-agents. `active_chip` is used in Phase A3 to locate a matching squad-builder output file (squad-builder mode only).

### A2 -- Budget Data (classic only - skip if format is "draft")

```bash
fpl squad sell-prices --refresh
```

Scrapes current sell prices from the FPL website. Requires `FPL_EMAIL` and `FPL_PASSWORD` in `.env`. If credentials are not configured, skip this step - affordability analysis in Phase C will be limited to the data available from other commands.

<!-- ADAPT: If you don't use FPL website credentials, remove this step -->

### A3 -- Squad-builder File Discovery (classic/both + squad-builder mode only - skip otherwise)

_Skip unless `mode == "squad-builder"` AND `active_chip ∈ {wildcard, freehit}` AND `metadata.format ∈ {"classic", "both"}`._

Locate a matching squad-builder output file and extract the Classic Squad block for embedding. Sets `squad_builder_result = "embed"` on success, or `"rederive"` with a `squad_builder_reason` code on failure. Resolving mode in Phase A (before Phase B) lets sub-agents know up-front which Phase B outputs will actually feed them, and prints the rederive warning banner before any data-gather commands run. All steps are synchronous and must complete before Phase B begins.

1. Look for `[YOUR_OUTPUT_DIR]/{season}/gw{N}-squad-builder.md`.
   - Not found → `squad_builder_result = "rederive"`, `squad_builder_reason = "file-missing"`. Done.
2. Parse the file's YAML frontmatter. Required fields: `mode`, `gameweek`.
   - Missing or malformed → `squad_builder_result = "rederive"`, `squad_builder_reason = "frontmatter-malformed"`. Done.
3. Gameweek check: `file.gameweek == N`?
   - Mismatch → `squad_builder_result = "rederive"`, `squad_builder_reason = "gameweek-mismatch"`. Done.
4. Mode check: use the following explicit mode map to normalise `file.mode` and compare to `active_chip`.

   | `file.mode` value | Normalised | Match against `active_chip`? |
   |---|---|---|
   | `Wildcard` | `wildcard` | ✓ matches `wildcard` |
   | `Free Hit` | `freehit` | ✓ matches `freehit` |
   | `Season Start Classic` | `seasonstartclassic` | ✗ mode-mismatch by design |
   | `Season Start Draft` | `seasonstartdraft` | ✗ mode-mismatch by design |
   | `Re-draft` | `redraft` | ✗ mode-mismatch by design |

   Any `file.mode` value not in this table is treated as mode-mismatch.
   - Mismatch → `squad_builder_result = "rederive"`, `squad_builder_reason = "mode-mismatch"`. Done.
5. Call the extraction helper:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/extract_classic_squad.py" --file "[YOUR_OUTPUT_DIR]/{season}/gw{N}-squad-builder.md"
   ```

   Parse stdout as JSON regardless of exit code. If JSON parse fails, treat as extraction-failed with a generic error message.
   - Non-zero exit → `squad_builder_result = "rederive"`, `squad_builder_reason = "extraction-failed"`. If stdout parsed with `error: true`, include `messages[0]` verbatim in the in-chat warning banner (see C1 Rederive Variant C). Done.
   - Zero exit → store `data.block` in phase context as `embedded_classic_squad_block`. Set `squad_builder_result = "embed"`.

6. Freshness check (embed path only): if `file.mtime` is older than 72 hours, emit an in-chat info note:
   > ℹ️ `gw{N}-squad-builder.md` was last modified more than 72h ago. The embedded squad may not reflect late-breaking team news. Proceeding — review Phase B data and apply inline swaps if warranted.

   Non-gating. mtime is a weak proxy (confounded by editor saves, iCloud sync, git checkouts).

7. If `squad_builder_result == "rederive"`, print the appropriate in-chat warning banner (variants A/B/C — see C1 Rederive section below for exact wording) before proceeding to Phase B. Proceed immediately; non-interactive.

---

## Phase B: Data Gathering

Run all applicable commands below. Every command uses `--format json`. Skip commands marked with a format condition that doesn't match `metadata.format` from A1.

**Reference preload (every run):** before issuing the CLI commands, read the full contents of `${CLAUDE_SKILL_DIR}/references/rules.md` and `${CLAUDE_SKILL_DIR}/references/output-template.md` into memory. These are inlined verbatim into each Phase C prompt as `{rules_content}` and `{output_template_content}` — sub-agents cannot reach the skill directory on their own, so the orchestrator must pass the text through. If either file is missing, abort with an error naming the expected path.

### B1 -- Fixture Difficulty

```bash
fpl fdr --blanks --format json
```

Returns fixture difficulty runs (pFDR, positional ATK/DEF ratings, upcoming fixtures) plus BGW/DGW predictions with confidence levels. `--blanks` surfaces the confirmed + predicted blank/double-GW schedule inline.

### B2 -- Captain Candidates (classic only - skip if format is "draft")

```bash
fpl captain --format json
```

### B3 -- Waiver Targets (draft only - skip if format is "classic")

```bash
fpl waivers --format json
```

### B4 -- Current Squad

```bash
fpl squad grid --format json
```

If format is `"both"`, also run:

```bash
fpl squad grid --draft --format json
```

Each record carries `player` and `team`. Keep the pair together: the C2.5/C3/C4
scripts below take player names, and a bare surname that two players share
(Dean and Jordan Henderson) is rejected as ambiguous rather than resolved to
whichever the API lists first. Pass `"{player} ({team})"` -- e.g.
`"Henderson (CRY)"` -- for every name sourced from this output.

### B5 -- Price Movements

```bash
fpl price-history --format json
```

### B6 -- Chip Timing Analysis (classic only - skip if format is "draft")

```bash
fpl chips timing --format json
```

### B7 -- Player Detail (on-demand)

For deeper analysis of specific players flagged by other commands:

```bash
fpl player "{player_name}" --format json
```

### B8 -- Statistical Leaders

Run the five differentiated queries below in parallel. Each targets a distinct transfer-signal surface that downstream Phase C analysis depends on; a single generic `fpl stats` call sorts by total points and misses most of them.

```bash
# In-form players across positions (trending, minutes-filtered, available)
fpl stats -s form --min-minutes 315 -n 15 --available-only --format json

# Transfer momentum (who the market is chasing this week)
fpl stats -s transfers_in_event -n 15 --format json

# MID xGI leaders (attacking mids, hauls signal)
fpl stats -p MID -s expected_goal_involvements --min-minutes 450 -n 15 --available-only --format json

# FWD xGI leaders (strikers, hauls signal)
fpl stats -p FWD -s expected_goal_involvements --min-minutes 450 -n 10 --available-only --format json

# DEF clean-sheet leaders (defensive picks)
fpl stats -p DEF -s clean_sheets --min-minutes 450 -n 10 --available-only --format json
```

Store each result under a distinct key (e.g. `stats_form`, `stats_transfer_momentum`, `stats_mid_xgi`, `stats_fwd_xgi`, `stats_def_clean_sheets`) and inline them into Phase C prompts as labelled sections.

### B9 -- Season Preview Intel

```bash
fpl intel --format json
```

Hand-curated pre-season notes on minutes, injuries, role and set-piece duty. Optional: most setups
have none, and every downstream step is unchanged when there is nothing to read.

**This is an early-season source by design.** Each section expires at the point real data
supersedes it -- projected XIs once actual minutes exist, everything eventually. Do not assume the
schedule: the response's `metadata.decay_schedule` carries the live expiry table and
`metadata.sections_live` what still counts at this gameweek. Once `sections_live` is empty, expect
an empty payload -- that is the decay working, not a failure. Its value is concentrated in the
opening gameweeks, when `starting_xi.py` and `bench_order.py` are ranking players who have not
played yet.

Read `metadata.coverage.usable_as` and store it as `intel_gate`:

| `intel_gate` | What sub-agents may do with intel |
|---|---|
| `full` | Support **or** oppose a pick |
| `negative_filter_only` | Only downgrade: injuries, rotation risk, "not nailed on". Never promote. |
| `none` | Ignore entirely; omit the intel block from every Phase C prompt |

With partial coverage the written-up teams carry annotations and the rest carry nothing, so absence
of a flag would read as absence of merit. Store the payload as `intel` for Phase C.

### B10 -- Injury Returnee Radar

```bash
fpl returnees --enrich --format json
```

Flagged players -- injured, suspended, unavailable, doubtful -- whose expected return lands inside
`metadata.window` gameweeks and who clear the radar's quality bar. Each entry in `data.entries`
carries the return estimate and where it came from (`expected_return`, `return_gameweek`,
`return_source`, `escalation_basis`), a quality verdict (`quality.passed`, `quality.meets_stash`)
and what moved since the previous run (`transition`). `data.departures` says who left the
watchlist and why.

`--enrich` searches the web for fresher return timing on the players FPL's own news field is silent
or stale about, which is what supplies a date for the injured majority. It needs a Perplexity API
key; without one the command skips enrichment, records why in `metadata.enrichment_note` and still
returns the FPL-sourced watchlist -- so this step is never a reason to abort the run. If
`metadata.enrichment_rate_limited` is true the provider rate-limited the run even after the command's
own retries; the answers it did get are cached, so re-run the command once before settling for the
partial payload.

Store the payload as `returnee_radar` and inline it into both Phase C prompts as a labelled
section. C1 renders it as an informational watchlist and is barred from naming its players in
transfer recommendations; C2 renders the subset still in the waiver pool and may escalate an entry
to a stash claim. The Returning Soon section quotes this payload, which is inlined into the prompt
like every other cited source -- so its numbers are grounded the same way every other cited number
is.

<!-- ADAPT: Add your own further supplementary data sources here. Examples:
  - `fpl preview --save --scout` generates a GW preview with fixture analysis and scout insights.
    Read the saved file and inject its content into Phase C sub-agents as additional context.
  - Newsletter extracts (e.g. community tips, model projections) saved as markdown files
-->

---

## Phase C: Analysis Sub-agents

Dispatch sub-agents based on `metadata.format` from A1:
- `"classic"` -- dispatch C1 only
- `"draft"` -- dispatch C2 only
- `"both"` -- dispatch C1 and C2 in parallel (or sequentially if parallel is unsupported)

Each sub-agent receives the JSON output from Phase B commands as context.

### C1 -- Classic League Analysis (skip if format is "draft")

- **model**: opus
- **subagent_type**: general-purpose

Branch on `squad_builder_result` (set in Phase A3; unset on transfer weeks):

---

**[Embed] `squad_builder_result == "embed"`** — wildcard/freehit with matched squad-builder file

**Prompt structure:**

> You are an FPL analyst preparing gameweek {N} recommendations for a classic league.
>
> **Mode: squad-builder — EMBED** (wildcard/free hit — Classic Squad block embedded below)
>
> **Analysis rules (apply in full):**
>
> {rules_content}
>
> **Output format (follow exactly):**
>
> {output_template_content}
>
> **Data (JSON):**
> - Status: {A1 output}
> - Chips: {A1.5 output}
> - pFDR: {B1 output}
> - Captain candidates: {B2 output}
> - Squad: {B4 output}
> - Price movements: {B5 output}
> - Chip timing: {B6 output}
> - Stats: {stats_form}, {stats_transfer_momentum}, {stats_mid_xgi}, {stats_fwd_xgi}, {stats_def_clean_sheets} (from B8)
>
> - Preview intel: {intel} (from B9 - omit this line entirely when `intel_gate` is `none`)
>   Gate: {intel_gate}. Governs **minutes and role only**, never how good a player is. Attribute it
>   ("Transfer Flow projects him starting"), never assert it. Under `negative_filter_only` use it
>   only to downgrade. A player with no intel is not a worse player.
>
> - Returning soon: {returnee_radar} (from B10)
>   **Informational only.** Render it as the Returning Soon section of the Classic output template,
>   one row per entry in `data.entries`, quoting only fields present in that payload. The
>   injury/suspension rule in the analysis rules above governs what may be recommended while a
>   player is flagged, and its classic branch is what bars transfer recommendations -- inline
>   late-change swaps included -- from naming a tracked returnee. Do not restate or relax it here.
>
> <!-- ADAPT: Add your own further supplementary data sources here (newsletters, external reports) -->
>
> **Embedded Classic Squad (from squad-builder — insert this as the `### Classic Squad` section):**
>
> {embedded_classic_squad_block}
>
> **Instructions:**
>
> Default: insert the embedded `### Classic Squad` block above **verbatim** as the Classic Squad section of the recommendations file. This is the expected path for nearly every embed-mode run.
>
> Late-changes path (use only when clearly warranted): if Phase B data reveals a material change squad-builder could not have seen (a starter ruled out after the file was written, an injury confirmed, a price move materially shifting affordability), you may apply a swap inline. To apply a swap: replace the relevant Starting XI or Bench row, update Captain/Vice if affected, adjust the Budget table (subtract OUT price, add IN price, update Total and Remaining) and Team Exposure table accordingly. Immediately after the block's `#### Alternatives` section, append a blockquote note: `> Late change: {OUT name} → {IN name} — {one-line reason, named source e.g. "ruled out per Thursday presser"}`. One note per swap. **Default is verbatim pass-through. Only deviate for a specific named reason. Never rewrite tables to "improve" formatting or fix perceived typos in squad-builder's output, and never fold an annotation (formation, notes) into a heading line — if squad-builder wrote `#### Starting XI`, keep the heading exactly as `#### Starting XI`; the formation belongs only on the `**Formation:**` line beneath it.**
>
> For Phase C2.5 (`transfer_eval.py`): on embed mode, the OUT-candidate pool is the 15 players in the Player column of the embedded Starting XI and Bench tables (not the current `fpl squad grid`). Use this to evaluate whether any late-breaking swaps are justified.
>
> **Sections to produce:** Chip Timing (from B6), Momentum Alerts (from B5/B8), Returning Soon (from B10), pFDR Overview (from B1), and the `### Classic Squad` block (embedded verbatim or swap-edited with trailing note). **Suppress:** Captain Pick top-3 table (captain is inside the embedded block), standalone Bench Order section (bench order is inside the embedded block), and Transfer Recommendations section (any swaps are applied inline in the block).
>
> **User workflow note:** The `### Classic Squad` block is the final 15-player squad the user should enter into FPL. If a trailing `> Late change:` note is present, it explains the swap — the user can revert it in the FPL site before saving if they disagree.
>
> Write the recommendations file with frontmatter:
> ```yaml
> squad_builder_mode: true
> mode: {wildcard|freehit}
> # phase_e_ok is written by Phase E after post-write validation — do not include it here
> ```

---

**[Rederive] `squad_builder_result == "rederive"` AND `mode == "squad-builder"`** — wildcard/freehit but file not usable

Before dispatching, print the in-chat warning (variant by `squad_builder_reason`):

**Variant A** (`squad_builder_reason == "file-missing"`):
> ⚠️ **Wildcard/Free Hit detected for GW{N}, but no squad-builder file was found.** Expected `gw{N}-squad-builder.md` in `[YOUR_OUTPUT_DIR]/{season}`. Re-derivation will run (weaker squad selection). To use squad-builder output, run `/squad-builder --{wildcard|freehit}` first, then re-run `/gw-prep`.

**Variant B** (`squad_builder_reason ∈ {"gameweek-mismatch", "mode-mismatch"}`):
> ⚠️ **Squad-builder file found but does not match this run.** Found `gw{N}-squad-builder.md` with `mode: {file.mode}` / `gameweek: {file.gameweek}`. Expected mode `{active_chip}` / gameweek `{N}`. Re-derivation will run. To use squad-builder output, run `/squad-builder --{wildcard|freehit}` for GW{N}, then re-run `/gw-prep`.

**Variant C** (`squad_builder_reason ∈ {"frontmatter-malformed", "extraction-failed"}`):
> ⚠️ **{Wildcard|Free Hit} detected for GW{N}. Found `gw{N}-squad-builder.md` but its frontmatter is malformed or its Classic Squad block could not be extracted.** Typical cause: file was produced with `--draft` only. Run `/squad-builder --{wildcard|freehit}` to regenerate, then re-run `/gw-prep`.

Proceed immediately (non-interactive).

**Prompt structure:**

> You are an FPL analyst preparing gameweek {N} recommendations for a classic league.
>
> **Mode: squad-builder — REDERIVE** (wildcard/free hit, squad-builder output unavailable)
>
> **Analysis rules (apply in full):**
>
> {rules_content}
>
> **Output format (follow exactly):**
>
> {output_template_content}
>
> **Data (JSON):**
> - Status: {A1 output}
> - Chips: {A1.5 output}
> - pFDR: {B1 output}
> - Captain candidates: {B2 output}
> - Squad: {B4 output}
> - Price movements: {B5 output}
> - Chip timing: {B6 output}
> - Stats: {stats_form}, {stats_transfer_momentum}, {stats_mid_xgi}, {stats_fwd_xgi}, {stats_def_clean_sheets} (from B8)
>
> - Preview intel: {intel} (from B9 - omit this line entirely when `intel_gate` is `none`)
>   Gate: {intel_gate}. Governs **minutes and role only**, never how good a player is. Attribute it
>   ("Transfer Flow projects him starting"), never assert it. Under `negative_filter_only` use it
>   only to downgrade. A player with no intel is not a worse player.
>
> - Returning soon: {returnee_radar} (from B10)
>   **Informational only.** Render it as the Returning Soon section of the Classic output template,
>   one row per entry in `data.entries`, quoting only fields present in that payload. The
>   injury/suspension rule in the analysis rules above governs what may be recommended while a
>   player is flagged, and its classic branch is what bars transfer recommendations -- inline
>   late-change swaps included -- from naming a tracked returnee. Do not restate or relax it here.
>
> <!-- ADAPT: Add your own further supplementary data sources here (newsletters, external reports) -->
>
> Apply squad-builder rules from `references/rules.md` for full squad selection. Prepend the fallback banner (Variant A/B/C — see `references/output-template.md` for exact wording) on the line immediately before the Classic section heading in the output file. Do not include `squad_builder_mode` in the frontmatter — the squad was re-derived, not embedded from squad-builder.
>
> Produce the **Classic** section of the output template.

---

**[Bench Boost] `mode == "benchboost"`** — bench boost active, all 15 players score

**Prompt structure:**

> You are an FPL analyst preparing gameweek {N} recommendations for a classic league.
>
> **Mode: benchboost** (Bench Boost active — all 15 squad players score this GW)
>
> **Analysis rules (apply in full):**
>
> {rules_content}
>
> **Output format (follow exactly):**
>
> {output_template_content}
>
> **Data (JSON):**
> - Status: {A1 output}
> - Chips: {A1.5 output}
> - pFDR: {B1 output}
> - Captain candidates: {B2 output}
> - Squad: {B4 output}
> - Price movements: {B5 output}
> - Chip timing: {B6 output}
> - Stats: {stats_form}, {stats_transfer_momentum}, {stats_mid_xgi}, {stats_fwd_xgi}, {stats_def_clean_sheets} (from B8)>
> - Returning soon: {returnee_radar} (from B10)
>   **Informational only.** Render it as the Returning Soon section of the Classic output template,
>   one row per entry in `data.entries`, quoting only fields present in that payload. The
>   injury/suspension rule in the analysis rules above governs what may be recommended while a
>   player is flagged, and its classic branch is what bars transfer recommendations -- inline
>   late-change swaps included -- from naming a tracked returnee. Do not restate or relax it here.
>
> **Bench Boost instructions:**
>
> All 15 players score this GW. The starting XI/bench boundary does not exist. Present all 15 as the scoring squad in a single table (no separate Starting XI and Bench tables). Suppress the Bench Order section entirely.
>
> When evaluating transfers, assess all 15 slots equally. A bench player who won't play (injured, £4.0m non-player) is a wasted scoring slot - flag them as transfer-out candidates even if they wouldn't normally be considered. The hit threshold still applies (expected gain > 8pts over horizon), but the gain calculation should include the bench player's expected return for this GW.
>
> Captain pick proceeds as normal. Chip Timing section should note that Bench Boost is being used this GW.
>
> Produce the **Classic** section of the output template.

---

**[Transfer] `mode == "transfer"`** — normal incremental week (no wildcard/freehit/benchboost active)

**Prompt structure:**

> You are an FPL analyst preparing gameweek {N} recommendations for a classic league.
>
> **Mode: transfer**
>
> **Analysis rules (apply in full):**
>
> {rules_content}
>
> **Output format (follow exactly):**
>
> {output_template_content}
>
> **Data (JSON):**
> - Status: {A1 output}
> - Chips: {A1.5 output}
> - pFDR: {B1 output}
> - Captain candidates: {B2 output}
> - Squad: {B4 output}
> - Price movements: {B5 output}
> - Chip timing: {B6 output}
> - Stats: {stats_form}, {stats_transfer_momentum}, {stats_mid_xgi}, {stats_fwd_xgi}, {stats_def_clean_sheets} (from B8)
>
> - Preview intel: {intel} (from B9 - omit this line entirely when `intel_gate` is `none`)
>   Gate: {intel_gate}. Governs **minutes and role only**, never how good a player is. Attribute it
>   ("Transfer Flow projects him starting"), never assert it. Under `negative_filter_only` use it
>   only to downgrade. A player with no intel is not a worse player.
>
> - Returning soon: {returnee_radar} (from B10)
>   **Informational only.** Render it as the Returning Soon section of the Classic output template,
>   one row per entry in `data.entries`, quoting only fields present in that payload. The
>   injury/suspension rule in the analysis rules above governs what may be recommended while a
>   player is flagged, and its classic branch is what bars transfer recommendations -- inline
>   late-change swaps included -- from naming a tracked returnee. Do not restate or relax it here.
>
> <!-- ADAPT: Add your own further supplementary data sources here (newsletters, external reports) -->
>
> Produce the **Classic** section of the output template.

### C2 -- Draft League Analysis (skip if format is "classic")

- **model**: sonnet
- **subagent_type**: general-purpose

**Prompt structure:**

> You are an FPL analyst preparing gameweek {N} recommendations for a draft league.
>
> **Analysis rules (apply in full):**
>
> {rules_content}
>
> **Output format (follow exactly):**
>
> {output_template_content}
>
> **Data (JSON):**
> - Status: {A1 output}
> - pFDR: {B1 output}
>
> **WAIVER POOL IS AUTHORITATIVE:** `fpl waivers` output is the only source for available players. All other data (stats, form tables, squad context) is for analysis only. Never recommend a claim not present in the waivers output — Phase D1 will flag pool misses as a warning. Cross-position recommendations (e.g. dropping a MID to claim a DEF) are structurally illegal and will be blocked by Phase D1.
>
> **`data.pool` is an availability roster, not a shortlist.** It lists every unowned player in the league — one `{id, player_name, position, team_short}` row each, unranked and untruncated — so membership answers only "may this player be claimed at all". Ranked claims come from `data.top_targets` and `data.targets_by_position`; presence in `data.pool` is never on its own a reason to recommend someone. The single claim allowed to rest on pool membership alone is a stash claim for a tracked returnee, which the waiver scoring suppresses by design and which therefore cannot reach the ranked targets.
>
> - Waivers: {B3 output}
> - Squad: {B4 output}
> - Stats: {stats_form}, {stats_transfer_momentum}, {stats_mid_xgi}, {stats_fwd_xgi}, {stats_def_clean_sheets} (from B8)
>
> - Preview intel: {intel} (from B9 - omit this line entirely when `intel_gate` is `none`)
>   Gate: {intel_gate}. Governs **minutes and role only**, never how good a player is. Attribute it
>   ("Transfer Flow projects him starting"), never assert it. Under `negative_filter_only` use it
>   only to downgrade. A player with no intel is not a worse player.
>
> - Returning soon: {returnee_radar} (from B10)
>   Render the Returning Soon section of the Draft output template from this payload, restricted to
>   tracked returnees whose `id` appears in the waivers `data.pool` list. A returnee missing from
>   the pool is already owned by a rival and is not actionable — leave it out rather than listing it
>   as unavailable. Quote only fields present in this payload.
>
> **STASH CLAIMS:** a Returning Soon row escalates into the Waiver Recommendations table only when
> **all four** gates below hold. Any one failing means watchlist only — no claim.
>
> 1. **Elite by prior:** `quality.meets_stash` is `true`.
> 2. **Returning soon enough:** `escalation_eligible` is `true`. The command has already applied
>    `metadata.escalation_window` and the citation gate, so do not re-derive the timing yourself:
>    an enrichment date counts here only when it arrived cited, and `escalation_basis` names which
>    source the verdict rests on (`fpl-news` or `ai-search`).
> 3. **Claimable and position-for-position:** the returnee's `id` is in the waivers `data.pool`, and
>    the player dropped for them plays the returnee's own position.
> 4. **Beats the incumbent by the configured margin:** take the drop candidate your squad analysis
>    already ranks lowest at that position, run C2.5's `transfer_eval.py --out "{that player}"
>    --in "{returnee}"`, and require `outlook_delta` to exceed the radar's
>    `metadata.stash_upgrade_margin`, which already carries the effective configured value; both
>    are quality points on the same 0-100 scale. If the script errors, or the margin is missing
>    from the metadata, or the delta does not clear it, do not escalate. A drop-priority ordering
>    always yields a lowest-ranked player and so can never answer "no" — the margin is what makes
>    this gate refusable.
>
> Write every stash claim as a stash, never as a straight upgrade: it spends a roster slot until the
> player is fit again, and it is bought to lock the asset before a rival can claim them. Each stash
> row's Rationale must carry the expected return (`expected_return`, or `return_gameweek` where the
> date is unknown) and name the provenance of that date from `escalation_basis`. The
> injury/suspension rule in the analysis rules above is the single authority on what may be
> recommended while a player is flagged.
>
> <!-- ADAPT: Add your own further supplementary data sources here (newsletters, external reports) -->
>
> Additionally enforce: every waiver swap must be position-for-position (MID out → MID in, DEF out → DEF in, etc.). Cross-position swaps are illegal under FPL Draft rules.
>
> Produce the **Draft** section of the output template.

### C2.5 -- Transfer/Waiver Evaluation

After each sub-agent identifies OUT candidates and an IN shortlist (from squad analysis, `fpl targets`, `fpl waivers`), run the transfer evaluation script for each OUT/shortlist pair:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/transfer_eval.py" --out "{out_player_name} ({club})" --in "{comma-separated IN candidates as 'Name (CLUB)'}"
```

The script outputs JSON with Outlook (multi-GW quality) and This GW (lineup impact) deltas for each IN candidate vs the OUT player. Use these scores as the quantitative baseline for transfer/waiver recommendations. Sub-agents may override with qualitative reasons (press conference intel, newsletter signals, season preview intel from B9) using the same `⚡ Override: {reason}` pattern as starting XI overrides. Name the source in the reason.

If the script fails (exit 1), fall back to LLM-driven transfer reasoning and note the failure.

### C3 -- Starting XI Selection (skip if `squad_builder_result == "embed"` OR `mode == "benchboost"` — embed has XI chosen, bench boost has no XI/bench split)

Run the lineup engine for each active format's squad **before** bench ordering:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/starting_xi.py" --squad "{comma-separated 15 squad players from squad grid, each as 'Name (CLUB)'}"
```

<!-- ADAPT: Replace with your squad players from the squad grid output, each as 'Name (CLUB)' -->

Use the script's recommended XI as the default lineup. Sub-agents may override specific picks with stated qualitative reasons (press conference intel, newsletter signals, rotation predictions, season preview intel from B9). Mark any overrides with `⚡ Override: {reason}` in the output, naming the source. If the script fails (exit 1), fall back to manual selection and note the failure.

**Preview intel is at its most useful here in GW1-3**, when the lineup engine is ranking players with no minutes on the board and a projected XI is the only nailed-on signal available. It feeds this override channel deliberately rather than the scoring inside `starting_xi.py`: an override is visible and attributed in the output, whereas a scoring input would move picks invisibly. Respect `intel_gate` -- under `negative_filter_only`, intel may bench a player but never promote one into the XI.

### C4 -- Bench Ordering (skip if `squad_builder_result == "embed"` OR `mode == "benchboost"` — embed has bench order inside block, bench boost has no bench ordering)

Using the starting XI from C3 (or the sub-agent's overridden version), run the bench order script:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/bench_order.py" --starting "{comma-separated starters as 'Name (CLUB)'}" --bench "{comma-separated bench as 'Name (CLUB)'}"
```

Incorporate the bench ordering output into the relevant sections of each sub-agent's recommendations.

---

## Phase D: Output

Combine the outputs from whichever sub-agents were dispatched into a single recommendations file. If only one format is active, the file contains only that format's section.

<!-- ADAPT: Set your output directory. Keep the `/{season}/` segment -- it is what stops a new season overwriting the last one's reports. -->
**Output path:** `[YOUR_OUTPUT_DIR]/{season}/gw{N}-recommendations.md`

The file should follow the structure defined in `references/output-template.md`, with both Classic and Draft sections populated.

Present a brief summary to the user:
- GW number and deadline
- Mode (transfer or squad-builder)
- Key highlights (top captain pick, priority transfer/waiver, chip timing note)
- Output file path

---

## Phase D1: Draft Validation (draft format only - skip if format is "classic")

_Runs after Phase D file write, before Phase E. Not embed-gated._

1. Write Phase B's waivers JSON (from orchestrator context, B3 output) to `/tmp/gw-prep-waivers-{N}.json`.
2. Write Phase B's draft squad-grid JSON (from orchestrator context, B4 draft output) to `/tmp/gw-prep-squad-grid-{N}.json`.
   - For draft-only runs, this is the `fpl squad grid --format json` output from B4.
   - For `both` format runs, this is the `fpl squad grid --draft --format json` output from B4.
3. Run:

   ```bash
   cd "$FPL_CLI_DIR" && source .venv/bin/activate && python "$FPL_CLI_DIR/.agents/skills/gw-prep/scripts/validate_draft_waivers.py" \
     --recommendations-file "[YOUR_OUTPUT_DIR]/{season}/gw{N}-recommendations.md" \
     --waivers-json /tmp/gw-prep-waivers-{N}.json \
     --squad-grid-json /tmp/gw-prep-squad-grid-{N}.json
   ```

   Parse stdout as JSON: `{"ok": bool, "flags": [...], "warnings": [...]}`.
   If the script cannot be found or exits non-zero unexpectedly, emit a warning and proceed to Phase E (fail-open for infrastructure errors, fail-closed only for confirmed rule violations).

4. **Managed block update:** locate the sentinel block in the recommendations file:
   ```
   <!-- phase-d1:start -->
   ...
   <!-- phase-d1:end -->
   ```
   - If flags or warnings are non-empty: replace (or insert) the block immediately after the `## Draft` (or `## Draft League`) heading line with:
     ```
     <!-- phase-d1:start -->
     > ⚠️ Draft validation:
     > - {one line per flag/warning, e.g. "Row 1: cross-position-claim (Drop: João Pedro FWD → Claim: Pedro Porro DEF)"}
     <!-- phase-d1:end -->
     ```
   - If both are empty: remove the block entirely (including sentinels) if present.

5. **Split posture:**
   - Any flag with `type == "cross-position-claim"` present → emit a **red error** in chat:
     > ❌ Phase D1 blocked: `gw{N}-recommendations.md` contains illegal cross-position waiver swap(s):
     > - Row {N}: Drop `{drop}` ({drop_position}) → Claim `{claim}` ({claim_position})
     > Edit the file to fix the flagged row(s), then re-run `/gw-prep` to continue.
     
     **Stop the pipeline.** Do not proceed to Phase E. Do not mark the run as complete.
   - Else (only warnings and/or `waiver-not-in-pool` flags) → emit an in-chat warning:
     > ⚠️ Phase D1: {N} waiver pool miss(es) in `gw{N}-recommendations.md` — see managed block in the Draft section. Claims may be normaliser false positives; verify before submitting waivers.
     
     Proceed to Phase E.
   - No flags or warnings → silent continue to Phase E.

---

## Phase E: Post-write Validation (embed-mode only)

_Skip unless `squad_builder_result == "embed"`. Transfer and rederive runs do not produce a `### Classic Squad` block._

1. Run:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/extract_classic_squad.py" --from-recommendations --file "[YOUR_OUTPUT_DIR]/{season}/gw{N}-recommendations.md"
   ```

   Parse stdout as JSON regardless of exit code. If exit is non-zero → emit warning and proceed:

   - If stdout parses as JSON with `error: true` → include `messages[0]` verbatim in the warning:
     > ⚠️ Phase E: could not recover `### Classic Squad` block from `gw{N}-recommendations.md` — {messages[0]}. Review manually before entering into FPL.
   - Otherwise (JSON parse failed or no `error` key) → emit the generic warning:
     > ⚠️ Phase E: could not recover `### Classic Squad` block from `gw{N}-recommendations.md` — the sub-agent's output may be malformed. Review manually before entering into FPL.

   **Do not mutate the file.** Proceed.

2. Parse the JSON output. Read `validation.structural` and `validation.arithmetic`.

3. **Structural checks:** collect an issue string for each failure:
   - `sub_headings_present[name] == false` → `"missing sub-heading: #### {name}"`
   - `starting_xi_rows != 11` → `"Starting XI has {N} rows, expected 11"`
   - `bench_rows != 4` → `"Bench has {N} rows, expected 4"`
   - `captain_named == false` → `"Captain not named"`
   - `vice_named == false` → `"Vice not named"`

4. **Arithmetic checks:** collect an issue string for each failure:
   - `budget_within_cap == false` OR `budget_total_gbp_m == null` → `"Budget parse failed or over 100.0m cap (parsed: {value})"`
   - `max_per_team_ok == false` → `"Team exposure violation: {list of teams with count > 3}"`. Build this list from `arithmetic.team_exposure` — iterate its entries and include each team whose integer value is greater than 3.
   - `player_count != 15` → `"Squad size is {N}, expected 15"`

5. **Report:** if the issues list is empty, silent continue (no in-chat output). If non-empty, emit:
   > ⚠️ Phase E validation: the Classic Squad in `gw{N}-recommendations.md` has {N} issue(s):
   > - {issue 1}
   > - {issue 2}
   > ...
   >
   > Phase E did not modify the file — the issues above were detected in the file the sub-agent just wrote. Review manually and either re-run `/gw-prep` or edit the file by hand before entering your squad into FPL.

6. **Frontmatter update (narrow exception):** after validation completes, write the result to the recommendations file's YAML frontmatter. Read the frontmatter block only (lines between the opening `---` and closing `---`), update or append the relevant fields, then write the full file as: new frontmatter block (from opening `---` to closing `---` inclusive) followed immediately by the original file content starting from the character after the closing `---`. **Reconstruction contract:** never write a file containing only the frontmatter — the body must be preserved verbatim.

   On successful validation (empty issues list):
   ```yaml
   phase_e_ok: true
   ```

   On failed validation (non-empty issues list):
   ```yaml
   phase_e_ok: false
   phase_e_issues:
     - missing-subheading
     - xi-row-count-wrong
     # ... one short-code per issue, from the vocabulary in references/output-template.md
   ```

   **Short-code vocabulary** (use the code that matches each issue string from steps 3-4):

   | Issue | Short code |
   |---|---|
   | any `#### {name}` sub-heading absent | `missing-subheading` |
   | `starting_xi_rows != 11` | `xi-row-count-wrong` |
   | `bench_rows != 4` | `bench-row-count-wrong` |
   | `captain_named == false` | `captain-unnamed` |
   | `vice_named == false` | `vice-unnamed` |
   | `budget_total_gbp_m is None` (parse failure) | `budget-parse-failed` |
   | `budget_total_gbp_m > 100.0` | `budget-over-cap` |
   | `max_per_team_ok == false` | `team-cap-violation` |
   | `player_count != 15` | `squad-size-wrong` |

   This frontmatter write is the single narrow exception to "Phase E never mutates the file" — it is idempotent (re-running Phase E produces the same frontmatter) and applies to frontmatter only. Flag in the commit message.

   **Embed-mode runs only.** On transfer and rederive runs, Phase E does not execute, so these fields are omitted.

7. Proceed to end of pipeline regardless of validation outcome. **The file body is never mutated by Phase E.**
