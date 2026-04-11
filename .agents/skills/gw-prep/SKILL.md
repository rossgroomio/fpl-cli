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

<!-- CLI commands composed: status, chips, chips sync, chips timing, fdr, captain, waivers, squad grid, squad sell-prices, price-history, player, stats -->

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

### A1.5 -- Chip Status

```bash
fpl chips sync
fpl chips --format json
```

`chips sync` ensures the local chip plan reflects any changes made via the FPL website. Run it silently before reading chip status.

Extract active chip status. If **Wildcard** or **Free Hit** is active for GW N:
- Set `mode = "squad-builder"` (full squad selection, not incremental transfers)
- Set `active_chip = "wildcard"` or `active_chip = "freehit"` accordingly
- Otherwise `mode = "transfer"` and `active_chip` is unset

This mode switch affects which rules apply in Phase C sub-agents. `active_chip` is used in Phase B9 to locate a matching squad-builder output file.

### A2 -- Budget Data (classic only - skip if format is "draft")

```bash
fpl squad sell-prices --refresh
```

Scrapes current sell prices from the FPL website. Requires `FPL_EMAIL` and `FPL_PASSWORD` in `.env`. If credentials are not configured, skip this step - affordability analysis in Phase C will be limited to the data available from other commands.

<!-- ADAPT: If you don't use FPL website credentials, remove this step -->

---

## Phase B: Data Gathering

Run all applicable commands below. Every command uses `--format json`. Skip commands marked with a format condition that doesn't match `metadata.format` from A1.

### B1 -- Fixture Difficulty

```bash
fpl fdr --format json
```

Returns fixture difficulty runs (pFDR, positional ATK/DEF ratings, upcoming fixtures) plus BGW/DGW predictions with confidence levels.

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

```bash
fpl stats --format json
```

<!-- ADAPT: Add your own supplementary data sources here. Examples:
  - `fpl preview --save --scout` generates a GW preview with fixture analysis and scout insights.
    Read the saved file and inject its content into Phase C sub-agents as additional context.
  - Newsletter extracts (e.g. community tips, model projections) saved as markdown files
-->

### B9 -- Squad-builder File Discovery (classic/both + squad-builder mode only - skip otherwise)

_Skip unless `mode == "squad-builder"` AND `active_chip ∈ {wildcard, freehit}` AND `metadata.format ∈ {"classic", "both"}`._

Locate a matching squad-builder output file and extract the Classic Squad block for embedding. Sets `squad_builder_source = "embed"` on success, or `"rederive"` with a `squad_builder_reason` code on failure. All steps are synchronous and must complete before Phase C dispatches.

1. Look for `[YOUR_OUTPUT_DIR]/gw{N}-squad-builder.md`.
   - Not found → `squad_builder_source = "rederive"`, `squad_builder_reason = "file-missing"`. Done.
2. Parse the file's YAML frontmatter. Required fields: `mode`, `gameweek`.
   - Missing or malformed → `squad_builder_source = "rederive"`, `squad_builder_reason = "frontmatter-malformed"`. Done.
3. Gameweek check: `file.gameweek == N`?
   - Mismatch → `squad_builder_source = "rederive"`, `squad_builder_reason = "gameweek-mismatch"`. Done.
4. Mode check: normalise `file.mode` (lowercase, strip whitespace and hyphens) and compare to `active_chip`. `Wildcard` matches `wildcard`; `Free Hit` matches `freehit`. `Season Start Classic`, `Season Start Draft`, and `Re-draft` files correctly reject as mode-mismatch by design.
   - Mismatch → `squad_builder_source = "rederive"`, `squad_builder_reason = "mode-mismatch"`. Done.
5. Call the extraction helper:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/extract_classic_squad.py" --file "[YOUR_OUTPUT_DIR]/gw{N}-squad-builder.md"
   ```

   - Non-zero exit → `squad_builder_source = "rederive"`, `squad_builder_reason = "extraction-failed"` (typical: file was produced with `--draft` only, no `## Classic Squad` block). Done.
   - Zero exit → store `data.block` in phase context as `embedded_classic_squad_block`. Set `squad_builder_source = "embed"`.

6. Freshness check (embed path only): if `file.mtime` is older than 72 hours, emit an in-chat info note:
   > ℹ️ `gw{N}-squad-builder.md` was last modified more than 72h ago. The embedded squad may not reflect late-breaking team news. Proceeding — review Phase B data and apply inline swaps if warranted.

   Non-gating. mtime is a weak proxy (confounded by editor saves, iCloud sync, git checkouts).

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

Branch on `squad_builder_source` (set in Phase B9; unset on transfer weeks):

---

**[Embed] `squad_builder_source == "embed"`** — wildcard/freehit with matched squad-builder file

**Prompt structure:**

> You are an FPL analyst preparing gameweek {N} recommendations for a classic league.
>
> **Mode: squad-builder — EMBED** (wildcard/free hit — Classic Squad block embedded below)
>
> Refer to `references/rules.md` for analysis rules and `references/output-template.md` for the output format.
>
> **Data (JSON):**
> - Status: {A1 output}
> - Chips: {A1.5 output}
> - pFDR: {B1 output}
> - Captain candidates: {B2 output}
> - Squad: {B4 output}
> - Price movements: {B5 output}
> - Chip timing: {B6 output}
> - Stats leaders: {B8 output}
>
> <!-- ADAPT: Add your own supplementary data sources here (newsletters, external reports) -->
>
> **Embedded Classic Squad (from squad-builder — insert this as the `### Classic Squad` section):**
>
> {embedded_classic_squad_block}
>
> **Instructions:**
>
> Default: insert the embedded `### Classic Squad` block above **verbatim** as the Classic Squad section of the recommendations file. This is the expected path for nearly every embed-mode run.
>
> Late-changes path (use only when clearly warranted): if Phase B data reveals a material change squad-builder could not have seen (a starter ruled out after the file was written, an injury confirmed, a price move materially shifting affordability), you may apply a swap inline. To apply a swap: replace the relevant Starting XI or Bench row, update Captain/Vice if affected, adjust the Budget table (subtract OUT price, add IN price, update Total and Remaining) and Team Exposure table accordingly. Immediately after the block's `#### Alternatives` section, append a blockquote note: `> Late change: {OUT name} → {IN name} — {one-line reason, named source e.g. "ruled out per Thursday presser"}`. One note per swap. **Default is verbatim pass-through. Only deviate for a specific named reason. Never rewrite tables to "improve" formatting or fix perceived typos in squad-builder's output.**
>
> For Phase C2.5 (`transfer_eval.py`): on embed mode, the OUT-candidate pool is the 15 players in the Player column of the embedded Starting XI and Bench tables (not the current `fpl squad grid`). Use this to evaluate whether any late-breaking swaps are justified.
>
> **Sections to produce:** Chip Timing (from B6), Momentum Alerts (from B5/B8), pFDR Overview (from B1), and the `### Classic Squad` block (embedded verbatim or swap-edited with trailing note). **Suppress:** Captain Pick top-3 table (captain is inside the embedded block), standalone Bench Order section (bench order is inside the embedded block), and Transfer Recommendations section (any swaps are applied inline in the block).
>
> **User workflow note:** The `### Classic Squad` block is the final 15-player squad the user should enter into FPL. If a trailing `> Late change:` note is present, it explains the swap — the user can revert it in the FPL site before saving if they disagree.
>
> Write the recommendations file with frontmatter:
> ```yaml
> squad_builder_mode: true
> mode: {wildcard|freehit}
> ```

---

**[Rederive] `squad_builder_source == "rederive"` AND `mode == "squad-builder"`** — wildcard/freehit but file not usable

Before dispatching, print the in-chat warning (variant by `squad_builder_reason`):

**Variant A** (`squad_builder_reason == "file-missing"`):
> ⚠️ **Wildcard/Free Hit detected for GW{N}, but no squad-builder file was found.** Expected `gw{N}-squad-builder.md` in `[YOUR_OUTPUT_DIR]`. Re-derivation will run (weaker squad selection). To use squad-builder output, run `/squad-builder --{wildcard|freehit}` first, then re-run `/gw-prep`.

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
> Refer to `references/rules.md` for analysis rules and `references/output-template.md` for the output format.
>
> **Data (JSON):**
> - Status: {A1 output}
> - Chips: {A1.5 output}
> - pFDR: {B1 output}
> - Captain candidates: {B2 output}
> - Squad: {B4 output}
> - Price movements: {B5 output}
> - Chip timing: {B6 output}
> - Stats leaders: {B8 output}
>
> <!-- ADAPT: Add your own supplementary data sources here (newsletters, external reports) -->
>
> Apply squad-builder rules from `references/rules.md` for full squad selection. Prepend the fallback banner (Variant A/B/C — see `references/output-template.md` for exact wording) on the line immediately before the Classic section heading in the output file. Do not include `squad_builder_mode` in the frontmatter — the squad was re-derived, not embedded from squad-builder.
>
> Produce the **Classic** section of the output template.

---

**[Transfer] `mode == "transfer"`** — normal incremental week (no wildcard/freehit active)

**Prompt structure:**

> You are an FPL analyst preparing gameweek {N} recommendations for a classic league.
>
> **Mode: transfer**
>
> Refer to `references/rules.md` for analysis rules and `references/output-template.md` for the output format.
>
> **Data (JSON):**
> - Status: {A1 output}
> - Chips: {A1.5 output}
> - pFDR: {B1 output}
> - Captain candidates: {B2 output}
> - Squad: {B4 output}
> - Price movements: {B5 output}
> - Chip timing: {B6 output}
> - Stats leaders: {B8 output}
>
> <!-- ADAPT: Add your own supplementary data sources here (newsletters, external reports) -->
>
> Produce the **Classic** section of the output template.

### C2 -- Draft League Analysis (skip if format is "classic")

- **model**: sonnet
- **subagent_type**: general-purpose

**Prompt structure:**

> You are an FPL analyst preparing gameweek {N} recommendations for a draft league.
>
> Refer to `references/rules.md` for analysis rules and `references/output-template.md` for the output format.
>
> **Data (JSON):**
> - Status: {A1 output}
> - pFDR: {B1 output}
> - Waivers: {B3 output}
> - Squad: {B4 output}
> - Stats leaders: {B8 output}
>
> <!-- ADAPT: Add your own supplementary data sources here (newsletters, external reports) -->
>
> Produce the **Draft** section of the output template.

### C2.5 -- Transfer/Waiver Evaluation

After each sub-agent identifies OUT candidates and an IN shortlist (from squad analysis, `fpl targets`, `fpl waivers`), run the transfer evaluation script for each OUT/shortlist pair:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/transfer_eval.py" --out "{out_player_name}" --in "{comma-separated IN candidate names}"
```

The script outputs JSON with Outlook (multi-GW quality) and This GW (lineup impact) deltas for each IN candidate vs the OUT player. Use these scores as the quantitative baseline for transfer/waiver recommendations. Sub-agents may override with qualitative reasons (press conference intel, newsletter signals) using the same `⚡ Override: {reason}` pattern as starting XI overrides.

If the script fails (exit 1), fall back to LLM-driven transfer reasoning and note the failure.

### C3 -- Starting XI Selection (skip if `squad_builder_source == "embed"` — squad-builder's assembler already chose the XI)

Run the lineup engine for each active format's squad **before** bench ordering:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/starting_xi.py" --squad "{comma-separated 15 squad player names from squad grid}"
```

<!-- ADAPT: Replace with your squad player names from the squad grid output -->

Use the script's recommended XI as the default lineup. Sub-agents may override specific picks with stated qualitative reasons (press conference intel, newsletter signals, rotation predictions). Mark any overrides with `⚡ Override: {reason}` in the output. If the script fails (exit 1), fall back to manual selection and note the failure.

### C4 -- Bench Ordering (skip if `squad_builder_source == "embed"` — bench order is inside the embedded block)

Using the starting XI from C3 (or the sub-agent's overridden version), run the bench order script:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/bench_order.py" --starting "{comma-separated starter names}" --bench "{comma-separated bench names}"
```

Incorporate the bench ordering output into the relevant sections of each sub-agent's recommendations.

---

## Phase D: Output

Combine the outputs from whichever sub-agents were dispatched into a single recommendations file. If only one format is active, the file contains only that format's section.

<!-- ADAPT: Set your output directory -->
**Output path:** `[YOUR_OUTPUT_DIR]/gw{N}-recommendations.md`

The file should follow the structure defined in `references/output-template.md`, with both Classic and Draft sections populated.

Present a brief summary to the user:
- GW number and deadline
- Mode (transfer or squad-builder)
- Key highlights (top captain pick, priority transfer/waiver, chip timing note)
- Output file path
