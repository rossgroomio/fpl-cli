# Tools & Capabilities

Complete inventory of fpl-cli's composable surface. For architectural diagrams and data flow, see [docs/architecture.md](../docs/architecture.md). For FPL-context usage guidance, see [.claude/output-styles/fpl-mate.md](../.claude/output-styles/fpl-mate.md).

## JSON Envelope

All `--format json` commands emit a standard envelope via `emit_json()` in `fpl_cli/cli/_json.py`:

```json
{
  "command": "<command-name>",
  "metadata": { ... },
  "data": [ ... ]
}
```

- `command` - the CLI command name (e.g. `"captain"`, `"chips-timing"`)
- `metadata` - command-specific context (typically includes `gameweek`)
- `data` - the payload (list or dict, varies per command)

Errors use `emit_json_error()` with `{"command", "error"}` shape. **Both envelopes go to
stdout** and exit 1 on failure, so parse stdout either way and read the exit code to know
which envelope you got. Warnings and progress go to stderr; the failure message itself is
in `error`, not on stderr. Full contract:
[Command Reference](../docs/command-reference.md#json-output).

## CLI Commands

| Command | Description | JSON | Format | Experimental | Pattern |
|---------|-------------|------|--------|-------------|---------|
| `init` | Set up fpl-cli with your FPL IDs and optional features | No | General | No | direct-api |
| `status` | Show FPL gameweek status and upcoming deadlines. JSON `metadata` carries `gameweek`, `format` and `season` (hyphenated label, e.g. `2026-27`) - `season` is the directory segment skills must use when writing alongside fpl-cli's own reports | Yes | General | No | direct-api |
| `doctor` | Check the setup for dead IDs, stale data, and config problems: resolves each configured ID against the live API (reporting the team/league name back so a wrong-but-valid ID is visible), verifies per-team data files (team ratings, managers, season preview intel, finances, player prior) describe the current season's clubs, and reports which config/data/cache directories are in use. With `--providers`, probes the external data sources instead: live shape-and-volume checks against the FPL and Draft APIs, the vaastav and Core-Insights datasets, Understat, and football-data.org, including that every club resolves across sources (the drift that otherwise surfaces as plausible but wrong output). Distinguishes broken (needs a fix today) from stale (self-corrects). Exits non-zero when something is broken | Yes | General | No | direct-api |
| `fixtures` | Show fixtures for a gameweek | Yes | General | No | direct-api |
| `player` | Look up a player's stats, xG, ownership and fixture run. JSON includes `ep_next` (predicted pts next GW) and `ep_this` (current GW), emitting `null` when FPL has no projection; panel shows `xPts` (ep_next) when available, omits the segment when FPL has no projection. `--detail` (`-d`) shows GW-by-GW performance and, for FWD/MID with sufficient history, xGI sustainability (per-match GI-xGI divergence -> form modifier). With `custom_analysis`: JSON adds `info.adjusted_npxg_per_90` (fixture-adjusted) and `info.raw_npxg_per_90` (Understat season avg); panel shows `adj. npxG/90: X.XXX (raw: Y.YYY)` | Yes | General | No | direct-api |
| `stats` | List players with filtering and sorting. `--value` adds quality/value per £m columns; `--window N` sets rolling lookback (3-10) for `rolling_pts_per_m`. Sortable by `ep_next`/`ep_this` (FPL predicted points; `ep_this` is only meaningful before the gameweek's first kickoff — FPL rolls it forward to `ep_next` as each player's match finishes); both in JSON output (emit `null` when FPL has no projection; table renders `—`). Players with no projection sort to the bottom in either direction | Yes | General | No | direct-api |
| `history` | Show historical player performance across seasons | Yes | General | No | direct-api |
| `league` | Show live league standings for Classic and Draft leagues | No | General | No | direct-api |
| `fdr` | Analyse fixture difficulty - easy runs, blanks, doubles | Yes | General | No | via-agent |
| `xg` | Analyse underlying stats: xG, xA, overperformers | Yes | General | No | via-agent |
| `price-changes` | Show price changes and transfer activity | No | General | No | via-agent |
| `price-history` | Show price trajectory and transfer momentum | Yes | General | No | direct-api |
| `returnees` | Track injured and suspended players due back soon, so a returnee can be claimed in draft before they are fit or planned around in classic. Reads the FPL availability news, filters to returns inside a window (`--window N`, 1-38) and through a source-aware quality bar, and diffs against the stored watchlist for week-over-week changes. Most flagged players carry no parseable return date, so date-unknown entries are the norm rather than the exception; `--enrich` searches the web for fresher timing on those and on stale-news entries (needs a Perplexity key; skips with a note without one) and shows it beside the FPL news, never over it. `--all` bypasses the quality bar and deliberately does not persist the snapshot. JSON `metadata` carries `window`, `escalation_window`, `stash_upgrade_margin`, `transitions_available`, `quality_bar_available`, `quality_bar_applied` and `enrichment_requested`/`_available`/`_note`/`_count`; `data` is `{entries, departures}`, each entry carrying `quality.basis`/`quality.meets_stash`, `expected_return`/`return_gameweek`/`return_source`, `escalation_eligible`/`escalation_basis`, `transition`, and `enrichment` (null unless enriched) | Yes | General | No | direct-api |
| `preview` | Run full pre-gameweek analysis and generate report. `--save` writes to `<output dir>/<season>/gw{N}-preview.md`; `--scout` writes to `<research dir>/ai-scout-reports/<season>/gw{N}-scout-preview[-referenced].md` | No | General | No | via-agent |
| `review` | Review a completed gameweek - squad performance and standings. `--save` writes to `<output dir>/<season>/gw{N}-review.md`; `--compare-recs` reads `gw{N}-recommendations.md` from the same season directory | No | General | No | via-agent |
| `league-recap` | Recap a completed gameweek - awards, standings, and banter. Saves to `<output dir>/<season>/gw{N}-league-recap[-draft].md`. Records every run into the league history ledger (`<data dir>/league_history/`) as a side effect, backfilling classic gaps at a coarse tier automatically; `--backfill-detail` rebuilds earlier gameweeks in full (one request per manager per gameweek). Coverage gaps and capture warnings go to stderr; the command exits 0 even when the store is unreadable. `--format json` emits one row per manager (the captured ledger row shape) plus `metadata.coverage`, `season_phase`, `notes_pack`, `synthesis_summary`, `warnings` (always present, each entry a stable `code` plus rewritable `message` - codes listed in [Command Reference](../docs/command-reference.md#league-history)) and `first_capture_store_path` (always present; the partition directory on a first capture, `null` after) | Yes | General | No | via-agent |
| `captain` | Analyse and rank captain options for next gameweek. JSON candidates include `adjusted_npxg_per_90` and `raw_npxg_per_90` when fixture adjustment is active. Consistency tiebreaker (CV-xGI percentile) phased in GW6-10. Players ruled out of the gameweek (0% chance, or no minutes from GW6) are held out of the GW1-9 shrinkage rather than pulled toward the position mean | Yes | Classic | Yes | via-agent |
| `differentials` | Find differential picks - high potential, low ownership. Inverted consistency bonus (volatile players score higher, phased in GW6-10). Players ruled out of the gameweek (0% chance, or no minutes from GW6) are held out of the GW1-9 shrinkage rather than pulled toward the position mean | Yes | Classic | Yes | via-agent |
| `targets` | Find transfer targets - high performers across all ownership. Consistency bonus (CV-xGI percentile, phased in GW6-10). Players ruled out of the gameweek (0% chance, or no minutes from GW6) are held out of the GW1-9 shrinkage rather than pulled toward the position mean | Yes | Classic | Yes | via-agent |
| `transfer-eval` | Compare transfer OUT player against IN candidates. JSON includes `adjusted_npxg_per_90` and `raw_npxg_per_90` per player when fixture adjustment is active. Players ruled out of the gameweek (0% chance, or no minutes from GW6) are held out of the GW1-9 shrinkage rather than pulled toward the position mean | Yes | General | Yes | via-agent |
| `allocate` | Select mathematically optimal 15-player squad within budget. Players ruled out of the gameweek (0% chance, or no minutes from GW6) are held out of the GW1-9 shrinkage rather than pulled toward the position mean; they stay in the solver pool | Yes | Classic | Yes | direct-api |
| `waivers` | Show waiver recommendations for your draft league. JSON `data` carries `top_targets` and `targets_by_position` (ranked claims) plus `pool` — the full unowned roster, one `{id, player_name, position, team_short}` row each, unranked and untruncated, so a consumer can answer "is this player claimable at all" without inferring it from the ranked lists. Players ruled out of the gameweek (0% chance, or no minutes from GW6) are held out of the GW1-9 shrinkage rather than pulled toward the position mean | Yes | Draft | Yes | via-agent |
| `squad` | Analyse your FPL squad health and fixtures | Yes | General | No | via-agent |
| `squad grid` | Show squad fixture difficulty grid | Yes | General | No | via-agent |
| `squad sell-prices` | Show squad sell prices and financial breakdown | Yes | Classic | No | direct-api |
| `chips` | View and plan FPL chip usage | Yes | Classic | No | direct-api |
| `chips timing` | Recommend chip timing based on blank/double GW exposure | Yes | Classic | No | via-agent |
| `chips add` | Plan a chip for a gameweek | No | Classic | No | direct-api |
| `chips remove` | Remove a planned chip from a gameweek | No | Classic | No | direct-api |
| `chips sync` | Sync chip usage from FPL API | No | Classic | No | direct-api |
| `ratings` | Display team ratings | No | General | Yes | direct-api |
| `ratings update` | Recalculate ratings from fixture results (shrunk toward the previous-season prior before GW12; seeds from the prior when no results exist yet) | No | General | Yes | direct-api |
| `intel` | Show season preview intel collected per team, with coverage and per-gameweek decay. JSON `metadata` carries `coverage.usable_as` (`full` / `negative_filter_only` / `none`), `section_confidence`, `sections_live`/`sections_expired`, `decay_schedule`, `team_set_warning` and `warnings`. `-g/--gameweek` ages the payload to any gameweek; `--show-decay` prints the expiry schedule | Yes | General | No | direct-api |
| `intel schema` | Print the preview file format with every field explained | No | General | No | direct-api |
| `intel init` | Scaffold an empty preview file per Premier League team (`--force` overwrites). Stubs never count toward coverage | No | General | No | direct-api |
| `intel show` | Show one team's preview, aged to the current gameweek | Yes | General | No | direct-api |
| `intel resolve` | Match preview player names to FPL `element_code`s; `--write` saves them back with comments preserved (never touching an existing code), `--all` re-resolves coded players and with `--write` saves corrections over them. Ambiguity is reported, never guessed | Yes | General | No | direct-api |
| `credentials set` | Store FPL email and password in system keyring | No | Classic | No | direct-api |
| `credentials clear` | Remove FPL credentials from system keyring | No | Classic | No | direct-api |

**Column key:**
- **JSON** - supports `--format json` output
- **Format** - Classic (classic league only), Draft (draft league only), General (both)
- **Experimental** - requires `custom_analysis: true` in settings; hidden from `--help` by default, and invoking one while it is off reports the toggle and the settings.yaml being read
- **Pattern** - `direct-api` (API client only), `via-agent` (uses analysis agent), `mixed` (both patterns in subcommands)

**Report paths are season-partitioned.** Every command that saves a report writes to `<dir>/<season>/gw{N}-*.md`, where `<season>` is the hyphenated label (`2026-27`). Report filenames carry a gameweek but no season, so a flat directory lets one season's GW21 file overwrite the previous season's. This applies to an explicit `--output` too. Any skill or script writing alongside these reports must use the same season directory, and must take the label from `fpl status --format json` (`metadata.season`) rather than hardcoding it — a hardcoded label silently rots at the July rollover.

## Skills

Agent playbooks in `.agents/skills/`. Each has a `SKILL.md` entry point. Claude Code discovers them via the `.claude/skills/` symlink.

Skills write their own outputs (`gw{N}-recommendations.md`, `gw{N}-squad-builder.md`, `season-start-squad.md`) into the same `<output dir>/<season>/` directory, for the same reason.

| Skill | Path | Purpose | Compatibility |
|-------|------|---------|--------------|
| gw-prep | `skills/gw-prep/` | Gameweek preparation recommendations for classic and draft (embed / rederive / transfer branches; Phase B9 preview intel; Phase B10 injury returnee radar; Phase E post-write validation) | Full: Claude Code. Partial: Codex, Cursor, Copilot |
| update-gw-prep | `skills/update-gw-prep/` | Append GW update to existing recommendations | Full: Claude Code, Codex, Cursor, Copilot |
| squad-builder | `skills/squad-builder/` | Build optimal 15-player squad (wildcard, free hit, season start; Phase B3 preview intel gate) | Full: Claude Code, Codex, Cursor, Copilot |
| preview-ingest | `skills/preview-ingest/` | Convert season preview prose into structured per-team intel files, resolve player codes, verify coverage | Full: Claude Code (parallel per team), Codex, Cursor, Copilot |
| release-notes | `skills/release-notes/` | Draft release notes and suggest the next semver (read-only preview; never tags or publishes) | Full: Claude Code, Codex, Cursor, Copilot |
| release | `skills/release/` | Cut a release end-to-end: preflight, notes via release-notes, approval gate, publish GitHub release (tag drives PyPI publish + changelog automation) | Full: Claude Code (local), Codex, Cursor, Copilot. Partial: Claude Code web (publish step handed to user) |
| create-pr | `skills/create-pr/` | Open a PR for the current branch: conventional-commit title (the changelog line) + why-first prose body in the fpl-cli PR description style | Full: Claude Code, Codex, Cursor, Copilot |

## Analysis Agents

Python classes in `fpl_cli/agents/` that implement `async run(context) -> AgentResult`.

| Agent | Module | Category | CLI Commands | External Consumers |
|-------|--------|----------|-------------|-------------------|
| FixtureAgent | `agents/data/fixture.py` | Data | `fdr`, `player -f`, `chips timing`, `preview` | - |
| PriceAgent | `agents/data/price.py` | Data | `price-changes`, `preview` | - |
| ScoutAgent | `agents/data/scout.py` | Data | `preview` | - |
| StatsAgent | `agents/analysis/stats.py` | Analysis | `xg`, `targets`, `differentials`, `preview` | - |
| CaptainAgent | `agents/analysis/captain.py` | Analysis | `captain`, `differentials` | - |
| SquadAnalyzerAgent | `agents/analysis/squad_analyzer.py` | Analysis | `squad` | - |
| BenchOrderAgent | `agents/analysis/bench_order.py` | Analysis | - | gw-prep skill |
| StartingXIAgent | `agents/analysis/starting_xi.py` | Analysis | - | gw-prep skill |
| TransferEvalAgent | `agents/analysis/transfer_eval.py` | Analysis | `transfer-eval` | gw-prep skill |
| WaiverAgent | `agents/action/waiver.py` | Action | `waivers` | - |
| ReportAgent | `agents/orchestration/report.py` | Orchestration | `preview`, `review`, `league-recap` | - |

**Notes:**
- BenchOrderAgent and StartingXIAgent have no CLI command - they are invoked by gw-prep skill wrapper scripts in `.agents/skills/gw-prep/scripts/`
- Every scoring family holds players known not to be playing out of the GW1-9 shrinkage; bench and lineup order on raw scores, so for those two it changes the displayed score only
- TransferEvalAgent is used by both `transfer-eval` CLI command and gw-prep skill
- `extract_classic_squad.py` (`.agents/skills/gw-prep/scripts/`) — deterministic Classic Squad block extractor used by gw-prep Phase A3 + Phase E. JSON stdout; read-only; emits TypedDict-annotated payloads.
- gw-prep Phase B10 runs `fpl returnees --enrich --format json` and inlines the payload into both Phase C prompts, rendering a Returning Soon section in the classic and draft output templates. In draft a row can escalate into a stash claim; in classic the watchlist is informational and transfer recommendations may not name a tracked returnee (`references/rules.md`)
- `validate_draft_waivers.py` (`.agents/skills/gw-prep/scripts/`) — cross-checks the Draft waiver table against the live waiver pool and squad grid (gw-prep Phase D1). JSON stdout; read-only; always exits 0.
- Both scripts locate markdown sections via `fpl_cli.utils.markdown` (`HeadingMatcher`, `find_section`, `section_body`, `leaf_body`, `fence_flags`), tolerating LLM heading drift (qualifiers, case, leading annotations, opt-in aliases) without matching a different heading that shares a prefix. Also used by `fpl_cli/prompts/review.py` for the GW Narrative section boundary.
