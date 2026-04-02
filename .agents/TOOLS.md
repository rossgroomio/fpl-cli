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

Errors use `emit_json_error()` with `{"command", "error"}` shape.

## CLI Commands

| Command | Description | JSON | Format | Experimental | Pattern |
|---------|-------------|------|--------|-------------|---------|
| `init` | Set up fpl-cli with your FPL IDs and optional features | No | General | No | direct-api |
| `status` | Show FPL gameweek status and upcoming deadlines | Yes | General | No | direct-api |
| `fixtures` | Show fixtures for a gameweek | Yes | General | No | direct-api |
| `player` | Look up a player's stats, xG, ownership and fixture run | Yes | General | No | direct-api |
| `stats` | List players with filtering and sorting | Yes | General | No | direct-api |
| `history` | Show historical player performance across seasons | Yes | General | No | direct-api |
| `league` | Show live league standings for Classic and Draft leagues | No | General | No | direct-api |
| `fdr` | Analyse fixture difficulty - easy runs, blanks, doubles | Yes | General | No | via-agent |
| `xg` | Analyse underlying stats: xG, xA, overperformers | Yes | General | No | via-agent |
| `price-changes` | Show price changes and transfer activity | No | General | No | via-agent |
| `price-history` | Show price trajectory and transfer momentum | Yes | General | No | direct-api |
| `preview` | Run full pre-gameweek analysis and generate report | No | General | No | via-agent |
| `review` | Review a completed gameweek - squad performance and standings | No | General | No | via-agent |
| `league-recap` | Recap a completed gameweek - awards, standings, and banter | No | General | No | via-agent |
| `captain` | Analyse and rank captain options for next gameweek | Yes | Classic | Yes | via-agent |
| `differentials` | Find differential picks - high potential, low ownership | Yes | Classic | Yes | via-agent |
| `targets` | Find transfer targets - high performers across all ownership | Yes | Classic | Yes | via-agent |
| `transfer-eval` | Compare transfer OUT player against IN candidates | Yes | General | Yes | via-agent |
| `allocate` | Select mathematically optimal 15-player squad within budget | Yes | Classic | Yes | direct-api |
| `waivers` | Show waiver recommendations for your draft league | Yes | Draft | Yes | via-agent |
| `squad` | Analyse your FPL squad health and fixtures | Yes | General | No | via-agent |
| `squad grid` | Show squad fixture difficulty grid | Yes | General | No | via-agent |
| `squad sell-prices` | Show squad sell prices and financial breakdown | Yes | Classic | No | direct-api |
| `chips` | View and plan FPL chip usage | Yes | Classic | No | direct-api |
| `chips timing` | Recommend chip timing based on blank/double GW exposure | Yes | Classic | No | via-agent |
| `chips add` | Plan a chip for a gameweek | No | Classic | No | direct-api |
| `chips remove` | Remove a planned chip from a gameweek | No | Classic | No | direct-api |
| `chips sync` | Sync chip usage from FPL API | No | Classic | No | direct-api |
| `ratings` | Display team ratings | No | General | Yes | direct-api |
| `ratings update` | Recalculate ratings from fixture results | No | General | Yes | direct-api |
| `credentials set` | Store FPL email and password in system keyring | No | Classic | No | direct-api |
| `credentials clear` | Remove FPL credentials from system keyring | No | Classic | No | direct-api |

**Column key:**
- **JSON** - supports `--format json` output
- **Format** - Classic (classic league only), Draft (draft league only), General (both)
- **Experimental** - requires `custom_analysis: true` in settings; hidden from `--help` by default
- **Pattern** - `direct-api` (API client only), `via-agent` (uses analysis agent), `mixed` (both patterns in subcommands)

## Skills

Agent playbooks in `.agents/skills/`. Each has a `SKILL.md` entry point. Claude Code discovers them via the `.claude/skills/` symlink.

| Skill | Path | Purpose | Compatibility |
|-------|------|---------|--------------|
| gw-prep | `skills/gw-prep/` | Gameweek preparation recommendations for classic and draft | Full: Claude Code. Partial: Codex, Cursor, Copilot |
| update-gw-prep | `skills/update-gw-prep/` | Append GW update to existing recommendations | Full: Claude Code, Codex, Cursor, Copilot |
| squad-builder | `skills/squad-builder/` | Build optimal 15-player squad (wildcard, free hit, season start) | Full: Claude Code, Codex, Cursor, Copilot |

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
- TransferEvalAgent is used by both `transfer-eval` CLI command and gw-prep skill
