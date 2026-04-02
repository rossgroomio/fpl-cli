---
name: update-gw-prep
description: >
  Append a "GW Update" section to existing gameweek recommendations using
  supplementary intelligence (newsletters, model outputs, etc.). Runs as a
  second pass after the initial gw-prep skill has produced a recommendations
  file. Use when the user says "update gw prep", "second pass", "add gw
  update", or wants to revise recommendations with new information.
model: opus
compatibility:
  claude-code: full (single sub-agent via Agent tool)
  codex: full (single sequential pass)
  cursor: full (single sequential pass)
  copilot: full (single sequential pass)
---
<!-- CLI commands used: status, captain, chips timing, player -->

# Update GW Prep

Append a `## GW Update` section to an existing gameweek recommendations file,
incorporating supplementary data that arrived after the initial prep was written.

## Environment

```bash
cd "$FPL_CLI_DIR" && source .venv/bin/activate
```

Set `FPL_CLI_DIR` to the root of your fpl-cli checkout.

## Prerequisites

- An existing `[YOUR_OUTPUT_DIR]/gw{N}-recommendations.md` file produced by the
  gw-prep skill (or equivalent)
- fpl-cli installed and configured (`fpl status` returns current GW)
<!-- ADAPT: Add any supplementary data source prerequisites here -->

## Workflow

### Phase A — Gather context and verify readiness

**A1. Detect current gameweek**

```bash
fpl status --format json
```

Parse the gameweek number. If "Finished", use N + 1. If "In Progress", use N.

Also extract `metadata.format` (`"classic"`, `"draft"`, or `"both"`). This determines which sections of the update to produce and which Phase B commands to run.

**A2. Verify existing recommendations**

Confirm `[YOUR_OUTPUT_DIR]/gw{N}-recommendations.md` exists and contains the
baseline sections (captain picks, transfer targets, chip strategy, bench order).
If the file is missing, abort with: "No existing recommendations found for
GW{N}. Run the gw-prep skill first."

**A3. Read supplementary data**

Read any supplementary reports available (newsletters, model outputs, community
consensus data, etc.).

<!-- ADAPT: Add supplementary data source paths here -->

**A4. Abort check**

If no supplementary data sources are available, report: "No supplementary data
sources found. Nothing to update." and stop.

### Phase B — Collect live data

Run the following CLI commands to get current analytical output:

```bash
fpl captain --format json        # classic only - skip if format is "draft"
fpl chips timing --format json   # classic only - skip if format is "draft"
```

<!-- ADAPT: Add supplementary data source reads here -->

Read the scoring and formatting rules:

```
.agents/skills/gw-prep/references/rules.md
```

<!-- ADAPT: Adjust the rules path to match your setup -->

### Phase C — Draft the update (sub-agent)

Dispatch a single sub-agent with the following brief.

#### Sub-agent brief

You are updating gameweek recommendations with new information. Your job is to
append a `## GW Update` section that highlights what has changed since the
baseline was written.

##### Input data blocks

1. **Baseline recommendations** — the existing `gw{N}-recommendations.md` file
2. **Supplementary data** — any reports, newsletters, or model outputs gathered
   in Phase A3
3. **CLI output** — `fpl captain` and `fpl chips timing` JSON results from
   Phase B

##### Anti-recency-bias rules

- Do not overweight information simply because it is newer. The baseline
  recommendations were produced with full analytical rigour.
- Only flag a change when supplementary data provides a *material* reason to
  revise (injury news, confirmed lineup leaks, significant model movement,
  ownership swings that affect EV calculations).
- State the source and reasoning for every proposed change.
- If supplementary data agrees with the baseline, say so briefly and move on.

##### Format-aware output

Only produce sections matching the active format from `metadata.format`:
- `"classic"` -- Captain, Transfers, Chips, Bench Order sections only
- `"draft"` -- Waivers, Bench Order sections only (no Captain, no Chips)
- `"both"` -- all sections

##### Waiver window logic (draft leagues - skip if format is "classic")

Detect whether the waiver window is frozen or open from the status output.

- **Frozen**: Waiver recommendations are locked. Note any players who *would*
  be targets if the window were open, but mark them as post-deadline.
- **Open**: Include waiver pickup/drop recommendations with priority ordering.

##### Squad builder mode detection

If the baseline recommendations contain a `squad_builder_mode: true` marker (or
equivalent heading), switch to squad-builder output format — the user is
planning transfers across multiple gameweeks rather than optimising for a single
week.

##### Player lookup

When you need to verify a player's stats or ownership, use:

```bash
fpl player "<name>" --format json
```

Always use `--format json` for machine-readable output.

##### Output template (normal mode)

```markdown
## GW Update

**Updated**: {date} | **Sources**: {list of supplementary sources used}

### Captain

{Any change to captain ranking, with reasoning. If no change: "No change from
baseline — {top pick} remains the top captain choice."}

### Transfers

{Revised transfer recommendations if injury/price/model data warrants. If no
change: "Baseline transfer plan holds."}

### Chips

{Any change to chip timing strategy. If no change: "No chip timing revision
needed."}

### Bench Order

{Revised bench order if relevant player news emerged. If no change: "Bench
order unchanged."}

### Notable Intelligence

{Bullet list of noteworthy items from supplementary data that don't trigger a
recommendation change but are worth flagging — e.g. press conference quotes,
ownership movements, set piece taker changes.}
```

##### Output template (squad builder mode)

```markdown
## GW Update

**Updated**: {date} | **Sources**: {list of supplementary sources used}

### Transfer Plan Revisions

{Changes to the multi-week transfer sequence, with reasoning per gameweek
affected. If no change: "Multi-week transfer plan holds."}

### Chip Deployment

{Any shift in optimal chip timing across the planning horizon.}

### Notable Intelligence

{Same as normal mode.}
```

### Phase D — Append to recommendations

Append the sub-agent's output to the existing file:

```
[YOUR_OUTPUT_DIR]/gw{N}-recommendations.md
```

Ensure there is a blank line before the `## GW Update` heading. Do not modify
any existing content above the update section.

If a previous `## GW Update` section exists, replace it (the latest update
supersedes earlier ones).

### Phase E — Verify and summarise

Read back the final file and confirm:

1. The baseline sections are intact and unmodified
2. The `## GW Update` section is correctly appended
3. All CLI commands used `--format json`

Report to the user: "Appended GW Update to gw{N}-recommendations.md with
{n} revisions and {m} notable intelligence items."
