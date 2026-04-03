## Project Overview
fpl-cli - CLI tool for Fantasy Premier League analysis (classic + draft).

## Setup & Dev
```bash
source .venv/bin/activate && pip install -e ".[dev]"
ruff check fpl_cli/            # Lint
pyright fpl_cli/               # Type check
pytest tests/                 # Tests
```
Entry point: `fpl_cli/cli/__init__.py:main` (Click). Config: `config/defaults.yaml` (committed) + `~/Library/Application Support/fpl-cli/settings.yaml` (user overrides, deep-merged via `platformdirs`). Override config dir with `FPL_CLI_CONFIG_DIR` env var.

## Architecture
Agents inherit `fpl_cli/agents/base.py:Agent`, implement `async run(context: dict | None) -> AgentResult`. Organised in `agents/{data,analysis,action,orchestration}/`. AgentResult statuses: SUCCESS, PARTIAL, FAILED, PENDING_APPROVAL.

External consumers: `BenchOrderAgent`, `StartingXIAgent`, and `TransferEvalAgent` are imported directly by the gw-prep skill (Obsidian vault) via standalone scripts (`bench_order.py`, `starting_xi.py`, `transfer_eval.py`) that run in fpl-cli's venv. Changes to any agent's interface or import path will break those scripts.

API clients in `fpl_cli/api/`: FPLClient (main API, caches `bootstrap-static/`), fpl_draft, perplexity (needs `PERPLEXITY_API_KEY`), understat (scrapes understat.com for npxG/xGChain/xGBuildup), vaastav (fetches historical CSV data from vaastav/Fantasy-Premier-League GitHub repo - 3 seasons, keyed on `element_code`). Scraper in `fpl_cli/scraper/` (needs `FPL_EMAIL`, `FPL_PASSWORD`). Jinja2 templates in `templates/`.

### Models (non-obvious aliases)
- `Player`: `element_type` = position, `team` = team_id, `code` = stable cross-season ID (element_code). Prices in £0.1m units (100 = £10.0m)
- `Fixture`: `gameweek` (alias "event"), `home_team_id`/`away_team_id`

For a complete inventory of CLI commands, analysis agents, and skills with JSON support and format awareness, see `.agents/TOOLS.md`.

## Conventions
### CLI Patterns
- Read-only data display commands -> direct API client usage (comment `# Pattern: direct-api`)
- Analytical commands -> via agent (comment `# Pattern: via-agent`)
- Help text: describe what the user sees, never reference internal components ("agent", "client")
- Inner async function: always name `_run`
- **Format awareness:** New commands classified in `CLASSIC_ONLY`/`DRAFT_ONLY` frozensets in `_context.py` (omit for General). Shared commands use `@click.pass_context` and `fmt = get_format(ctx)` to gate irrelevant sections (see `league.py` for pattern).

### Agent Patterns
- Primary API client: `self.client` in `__init__` (FPLClient for classic agents, FPLDraftClient for draft agents)
- Secondary clients: qualified name (e.g. `self.perplexity_client`)
- Position map: import `POSITION_MAP` from `fpl_cli/models/player.py`, never redefine locally
- Understat enrichment: import `match_fpl_to_understat` from `fpl_cli/api/understat`

### API Method Naming
- Use `get_` prefix for all data-fetching methods
- Use domain language (gameweek, player, team), never FPL jargon (event, element, bootstrap-static)
- FPL jargon translation happens at the model boundary via Pydantic aliases
- xG, xA, xGI, npxG, FDR, BPS - stats abbreviations acceptable as-is

### Exception Handling
- Never use bare `except Exception`. Use specific types for narrow try blocks; use `# noqa: BLE001 — <justification>` for intentional broad handlers (agent top-level, scraper resilience, graceful degradation)

## FPL Domain Knowledge
- Chips (each available **twice** per season, split at GW19 deadline): Wildcard, Free Hit, Bench Boost, Triple Captain
- Scoring: GK/DEF clean sheet = 4pts, MID = 1pt; goals: DEF=6, MID=5, FWD=4; assist = 3pts; yellow = -1; red = -3
- Transfers: 1 free/GW, max 5 banked; extra transfers cost 4pts each; Wildcard/Free Hit preserve banked transfers
- Draft format has no captains, no budget, no transfers, no chips - uses waivers for player acquisition

## Rules
- Find-and-replace: review each replacement in context - don't blindly replace substrings in unrelated identifiers
- Removing/replacing X: new implementation must have zero dependencies on X
- Repeated convention violations: suggest a ruff lint rule to enforce automatically
- README must stay in sync: any CLI command added/changed/removed requires updating the relevant job section in "What You Can Do" and, if the command has detailed flags or formulas, `docs/command-reference.md`
- Architecture doc must stay in sync: adding a new agent, service, API client, or CLI command requires updating `docs/architecture.md`
- TOOLS.md must stay in sync: adding, removing, or changing a CLI command, analysis agent, or skill requires updating `.agents/TOOLS.md`
- AGENTS.md must stay in sync: any change to project instructions in CLAUDE.md requires the same change in AGENTS.md
- CLI changes require corresponding unit tests
- Changing a function's return format: update existing tests to match and confirm pytest passes
- Tests: `pytest-asyncio` with `asyncio_mode = "auto"`, factories in `tests/conftest.py` (`make_player()`, `make_team()`, `make_fixture()`)
- After any task touching Python files, run `pyright fpl_cli/` as a final check alongside ruff and pytest
