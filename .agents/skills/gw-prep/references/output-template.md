# GW-Prep Output Template

Template structure for the gameweek recommendations file.

**Output path:** `[YOUR_OUTPUT_DIR]/{season}/gw{N}-recommendations.md`

`{season}` is the hyphenated season label from `fpl status --format json` (`metadata.season`). The filename carries the gameweek but no season, so the directory segment is what stops a new season's GW21 file overwriting the previous season's.

---

## File Structure

**Frontmatter fields** added at the top of the output file. `squad_builder_mode` is load-bearing for `/update-gw-prep`'s Phase C detection rule ("if `squad_builder_mode: true`, switch to squad-builder output format") — omit it on non-embed-mode runs to preserve that detection. `mode` is a forward-compat machine-readable enum for future consumers; always written. `season` and `gameweek` let a reader confirm which season's file it opened without inferring it from the path.

```markdown
---
season: {season}  # Always present. Hyphenated label from `fpl status --format json` (metadata.season).
gameweek: {N}     # Always present. Together with season this identifies the file independently of its path.
squad_builder_mode: true  # Only on embed-mode wildcard/freehit runs. Omit on rederive or transfer runs.
mode: wildcard | freehit | benchboost | transfer  # Always present. Enum value matches the active chip or "transfer".
phase_e_ok: true | false  # Embed-mode only. Written by Phase E after post-write validation. Omit on transfer and rederive runs.
phase_e_issues:  # Embed-mode only. Present when phase_e_ok: false. Short-code list from the vocabulary below.
  - missing-subheading       # any of the six expected #### sub-headings is absent
  - xi-row-count-wrong       # starting_xi_rows != 11
  - bench-row-count-wrong    # bench_rows != 4
  - captain-unnamed          # captain_named == false
  - vice-unnamed             # vice_named == false
  - budget-parse-failed      # budget_total_gbp_m is None (parse failure)
  - budget-over-cap          # budget_total_gbp_m is not None and > 100.0
  - team-cap-violation       # max_per_team_ok == false (any team with count > 3)
  - squad-size-wrong         # player_count != 15
---

# Gameweek {N} Recommendations

**Deadline:** {deadline}
**Mode:** {transfer | squad-builder}
**Generated:** {timestamp}

---

## Classic League

### Chip Timing

Summary of chip timing analysis. Note if any chip is recommended for this GW or upcoming GWs, with rationale.

### Captain Pick

_Suppressed on embed-mode runs — see SKILL.md Phase C1._

| Rank | Player | Team | Opponent (pFDR) | Key Stat | Rationale |
|------|--------|------|-----------------|----------|-----------|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |

**Recommended captain:** {player} -- {one-line rationale}

### Transfer Recommendations

_Suppressed on embed-mode runs — see SKILL.md Phase C1._

| Priority | Out | In | Outlook | This GW | Net Cost | Rationale |
|----------|-----|----|---------|---------|----------|-----------|
| 1 | | | | | | |
| 2 | | | | | | |
| 3 | | | | | | |

Outlook = multi-GW quality delta (target score). This GW = lineup impact delta. Both from `transfer_eval.py`.

**Total hits:** {0 or -4/-8} | **Net expected gain:** {points}

For each transfer, include:
- Upcoming fixtures (next 3 GWs)
- Form summary
- Price trend

### Classic Squad

_Embed-mode only: the orchestrator replaces this placeholder at runtime with the `{embedded_classic_squad_block}` extracted from `gw{N}-squad-builder.md`. Do not populate this section manually — it is produced by the C1 sub-agent._

### Bench Order

_Suppressed on embed-mode runs — see SKILL.md Phase C1._
_Suppressed on bench boost runs — all 15 players score, bench order is irrelevant._

BenchOrderAgent recommended order:

| Bench Slot | Player | Score | Rationale |
|------------|--------|-------|-----------|
| GK | | | |
| 1st sub | | | |
| 2nd sub | | | |
| 3rd sub | | | |

Coverage notes (sole-coverage positions, rotation risks).

### Momentum Alerts

- **Hot streaks:** {players}
- **Cold streaks:** {players}
- **Minutes risks:** {players}
- **Price alerts:** {players}

### pFDR Overview

Best and worst fixture runs by position for the next 5 GWs.
Cross-referenced against current squad holdings.

---

## Draft League

### Waiver Recommendations

| Priority | Drop | Claim | Position | Outlook | This GW | Fixture Run | Rationale |
|----------|------|-------|----------|---------|---------|-------------|-----------|
| 1 | | | | | | | |
| 2 | | | | | | | |
| 3 | | | | | | | |
| 4 | | | | | | | |
| 5 | | | | | | | |

Outlook = multi-GW quality delta (target score). This GW = lineup impact delta. Both from `transfer_eval.py`.

### Starting XI

Recommended lineup with formation (via lineup engine).

| Pos | Player | Score | Opponent (pFDR) | Form | Rationale |
|-----|--------|-------|----------------|------|-----------|
| GK | | | | | |
| DEF | | | | | |
| DEF | | | | | |
| ... | | | | | |

Score = lineup engine score (0-100). If a player was overridden into/out of the XI, append: `⚡ Override: {reason}`

**Formation:** {e.g. 3-4-3}

### Bench Order

BenchOrderAgent recommended order:

| Bench Slot | Player | Score | Rationale |
|------------|--------|-------|-----------|
| GK | | | |
| 1st sub | | | |
| 2nd sub | | | |
| 3rd sub | | | |

### Momentum Alerts

- **Hot streaks:** {players}
- **Cold streaks:** {players}
- **Minutes risks:** {players}

---

## Notes

Any additional context, caveats, or follow-up actions.
```

---

## Fallback Banners (Wildcard/Free Hit re-derivation)

When Phase A3 detects a wildcard/freehit week but cannot use the squad-builder file (`squad_builder_result == "rederive"`), prepend the appropriate banner on the line immediately before the Classic section heading in the output file. Choose the variant matching `squad_builder_reason`.

`Season Start Classic`, `Season Start Draft`, and `Re-draft` mode files correctly land in Variant B's mode-mismatch branch by design — those modes cannot match an active wildcard/freehit chip.

**Variant A** (`squad_builder_reason == "file-missing"`):
> ⚠️ **Wildcard/Free Hit detected for GW{N}, but no squad-builder file was found.** Expected `gw{N}-squad-builder.md` in `[YOUR_OUTPUT_DIR]/{season}`. Re-derivation has run (weaker squad selection). To use squad-builder output next time, run `/squad-builder --{wildcard|freehit}` first, then re-run `/gw-prep`.

**Variant B** (`squad_builder_reason ∈ {"gameweek-mismatch", "mode-mismatch"}`):
> ⚠️ **Squad-builder file found but does not match this run.** Found `gw{N}-squad-builder.md` with `mode: {file.mode}` / `gameweek: {file.gameweek}`. Expected mode `{active_chip}` / gameweek `{N}`. Re-derivation has run. To use squad-builder output, run `/squad-builder --{wildcard|freehit}` for GW{N}, then re-run `/gw-prep`.

**Variant C** (`squad_builder_reason ∈ {"frontmatter-malformed", "extraction-failed"}`):
> ⚠️ **{Wildcard|Free Hit} detected for GW{N}. Found `gw{N}-squad-builder.md` but its frontmatter is malformed or its Classic Squad block could not be extracted.** Typical cause: file was produced with `--draft` only. Run `/squad-builder --{wildcard|freehit}` to regenerate, then re-run `/gw-prep`.
