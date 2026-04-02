# GW-Prep Output Template

Template structure for the gameweek recommendations file.

**Output path:** `[YOUR_OUTPUT_DIR]/gw{N}-recommendations.md`

---

## File Structure

```markdown
# Gameweek {N} Recommendations

**Deadline:** {deadline}
**Mode:** {transfer | squad-builder}
**Generated:** {timestamp}

---

## Classic League

### Chip Timing

Summary of chip timing analysis. Note if any chip is recommended for this GW or upcoming GWs, with rationale.

### Captain Pick

| Rank | Player | Team | Opponent (pFDR) | Key Stat | Rationale |
|------|--------|------|-----------------|----------|-----------|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |

**Recommended captain:** {player} -- {one-line rationale}

### Transfer Recommendations

_If mode is `squad-builder`, replace this section with Squad Builder below._

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

### Squad Builder (Wildcard/Free Hit only)

_Replace Transfer Recommendations when mode is `squad-builder`._

**Budget:** {available} | **Formation:** {primary} / {secondary}

#### Recommended XV

| Pos | Player | Team | Price | Next 3 Fixtures | Rationale |
|-----|--------|------|-------|-----------------|-----------|
| GK | | | | | |
| GK | | | | | |
| DEF | | | | | |
| DEF | | | | | |
| DEF | | | | | |
| DEF | | | | | |
| DEF | | | | | |
| MID | | | | | |
| MID | | | | | |
| MID | | | | | |
| MID | | | | | |
| MID | | | | | |
| FWD | | | | | |
| FWD | | | | | |
| FWD | | | | | |

**Total cost:** {total} | **Remaining budget:** {remaining}

### Bench Order

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
