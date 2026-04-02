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
- Otherwise `mode = "transfer"` (normal incremental recommendations)

This mode switch affects which rules apply in Phase C sub-agents.

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

**Prompt structure:**

> You are an FPL analyst preparing gameweek {N} recommendations for a classic league.
>
> **Mode: {mode}** (transfer | squad-builder)
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
> If mode is `squad-builder`, apply squad-builder rules from `references/rules.md` instead of transfer rules.
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

### C3 -- Starting XI Selection

Run the lineup engine for each active format's squad **before** bench ordering:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/starting_xi.py" --squad "{comma-separated 15 squad player names from squad grid}"
```

<!-- ADAPT: Replace with your squad player names from the squad grid output -->

Use the script's recommended XI as the default lineup. Sub-agents may override specific picks with stated qualitative reasons (press conference intel, newsletter signals, rotation predictions). Mark any overrides with `⚡ Override: {reason}` in the output. If the script fails (exit 1), fall back to manual selection and note the failure.

### C4 -- Bench Ordering

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
