## Project Overview
fpl-cli - CLI tool for Fantasy Premier League analysis (classic + draft). Distributed on PyPI as `fplkit`; the import package is `fpl_cli` and the command is `fpl`.

## Setup & Dev
```bash
source .venv/bin/activate && pip install -e ".[dev]"            # local only — web sessions install globally via setup script
ruff check fpl_cli/ scripts/   # Lint
pyright fpl_cli/ scripts/      # Type check
python3 -m pytest tests/       # Tests
```
Run tests as `python3 -m pytest`, never bare `pytest`: in web sessions the `pytest` on PATH is a uv tool shim with its own interpreter and none of the project's dependencies, so it fails at `import pydantic`. If pytest then rejects `--disable-socket` / `--allow-unix-socket`, the dev extra is incomplete — `pip install pytest-socket`.

Entry point: `fpl_cli/cli/__init__.py:main` (Click). Config: `fpl_cli/config/defaults.yaml`, shipped inside the package and resolved as `SHIPPED_CONFIG_DIR` (`paths.py`), + `settings.yaml` in the user config dir (overrides, deep-merged via `platformdirs`; `~/Library/Application Support/fpl-cli/` on macOS). Repo-root `config/` holds only examples and `team_ratings_overrides.yaml` — nothing there is loaded as defaults. All three writable dirs have env overrides: `FPL_CLI_CONFIG_DIR` (settings, managers, overrides; an optional `fixture_predictions.yaml` here overrides the shipped copy; an optional `previews/` dir holds season preview intel read by `fpl intel`), `FPL_CLI_DATA_DIR` (generated data: team ratings, priors, chip plan, sell prices, the returnee radar's `returnee_snapshot.json` week-over-week baseline (season-stamped, discarded on mismatch like `player_prior.yaml`), the append-only `league_history/` ledger and its rebuildable `league_history_counters/` projection — the ledger partitions by season instead of being discarded at rollover, the one store that outlives a season), `FPL_CLI_CACHE_DIR` (disposable). Overrides must be absolute — a relative one resolves against the cwd, so it is rejected with `UserDirError` rather than silently giving a different dir per invocation. Resolve them by calling `user_config_dir()` / `user_data_dir()` / `user_cache_dir()` at point of use — binding one to a module-level constant freezes the override at import time (before `.env` loads); `tests/test_paths.py` enforces this. Ephemeral environments (Claude Code on the web) must set config + data to a persistent workspace or generated data dies with the container; cache can stay local.

## Architecture
Agents inherit `fpl_cli/agents/base.py:Agent`, implement `async run(context: dict | None) -> AgentResult`. Organised in `agents/{data,analysis,action,orchestration}/`. AgentResult statuses: SUCCESS, PARTIAL, FAILED, PENDING_APPROVAL.

External consumers: `BenchOrderAgent`, `StartingXIAgent`, and `TransferEvalAgent` are imported directly by the gw-prep skill (Obsidian vault) via standalone scripts (`bench_order.py`, `starting_xi.py`, `transfer_eval.py`) that run in fpl-cli's venv. Changes to any agent's interface or import path will break those scripts.

API clients in `fpl_cli/api/`: FPLClient (main API, caches `bootstrap-static/`), fpl_draft, understat (scrapes understat.com for npxG/xGChain/xGBuildup), historical data via dual-source architecture: VaastavClient (multi-season, window passed in by its caller, keyed on `element_code`) + CoreInsightsClient (binds to `get_season_year()` at construction, always exactly one season), composed by `HistoricalDataProvider`. The split is `make_historical_provider()`'s, not either client's: it hands vaastav `season_label_range(get_season_year() - 1, count=3)`, so never hardcode a season label there — the window rolls itself at the July cutover. Shared types in `historical_types.py` (SeasonHistory, PlayerProfile, GwTrendProfile). Season helper: `season_label()` (alias `vaastav_season()`). Scraper in `fpl_cli/scraper/` (credentials resolve `FPL_EMAIL` / `FPL_PASSWORD` first, then the system keyring written by `fpl credentials set`; set `FPL_BROWSER_IGNORE_CERTS=1` behind TLS-inspecting proxies — Chromium ignores `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`). Jinja2 templates in `templates/`.

LLM access is not an API client: `fpl_cli/api/providers/` is a registry of `perplexity` / `anthropic` / `openai` (an OpenAI-compatible base covering Groq, Together, Ollama via `base_url`), resolved per role by `get_llm_provider(role, settings)` and all returning `LLMResponse`. Roles are `research` and `synthesis`; resolution is env > `settings.yaml` > `defaults.yaml`, with `FPL_{ROLE}_PROVIDER` / `_MODEL` / `_BASE_URL` overriding and keys in `PERPLEXITY_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`.

Services in `fpl_cli/services/` are the computation layer between agents and API clients: scoring engine, player priors, team ratings and form, matchup scoring, fixture predictions, season previews, the ILP squad allocator, and the league-history ledger with its counters projection and notes pack. Also load-bearing and easy to miss: `fpl_cli/paths.py` (every path resolution), `fpl_cli/season.py` (season labels and partitioning), `fpl_cli/prompts/` (LLM prompt builders), `fpl_cli/parsers/` (recommendation parsing), `fpl_cli/constants.py`.

### Models (non-obvious aliases)
- `Player`: attribute is `position` (JSON alias `element_type`), `team_id` (alias `team`), `code` = stable cross-season ID (element_code). Prices in £0.1m units (100 = £10.0m)
- `Fixture`: `gameweek` (alias `event`), `home_team_id`/`away_team_id`

For a complete inventory of CLI commands, analysis agents, and skills with JSON support and format awareness, see `.agents/TOOLS.md`. Skills live in `.agents/skills/`. `docs/architecture.md` is the deep reference — module map, services layer, provider abstraction, config resolution, design decisions — read it before changing structure.

## Conventions
### CLI Patterns
- Read-only data display commands -> direct API client usage (comment `# Pattern: direct-api`)
- Analytical commands -> via agent (comment `# Pattern: via-agent`)
- Help text: describe what the user sees, never reference internal components ("agent", "client")
- Inner async function: always name `_run`
- `--format json` commands emit the shared envelope via `emit_json()` / `emit_json_error()` (`cli/_json.py`), never a bare `json.dumps`. Both envelopes go to stdout and every human-readable line to stderr — success is `{command, metadata, data}` and exit 0, failure is `{command, error}` and exit 1, so a consumer parses one stream either way
- **Format awareness:** New commands classified in `CLASSIC_ONLY`/`DRAFT_ONLY` frozensets in `_context.py` (omit for General). Shared commands use `@click.pass_context` and `fmt = get_format(ctx)` to gate irrelevant sections (see `league.py` for pattern).

### Agent Patterns
- Primary API client: `self.client` in `__init__` (FPLClient for classic agents, FPLDraftClient for draft agents)
- Secondary clients: qualified name (e.g. `self.fpl_client` on a draft agent)
- LLM access is not a client attribute: resolve it per role with `get_llm_provider(role, settings)` from `fpl_cli.api.providers` (see `ScoutAgent.research_provider`)
- Position map: import `POSITION_MAP` from `fpl_cli/models/player.py`, never redefine locally
- Understat enrichment: import `match_fpl_to_understat` from `fpl_cli/api/understat`
- Draft-to-main player matching: import `match_draft_to_main` from `fpl_cli/api/fpl_draft`, never join on `(web_name, team_id)` — `code` is the stable key and a mid-season rename breaks the name join

### API Method Naming
- Use `get_` prefix for all data-fetching methods
- Use domain language (gameweek, player, team), never FPL jargon (event, element, bootstrap-static)
- FPL jargon translation happens at the model boundary via Pydantic aliases
- xG, xA, xGI, npxG, FDR, BPS - stats abbreviations acceptable as-is

### Exception Handling
- Never use bare `except Exception`. Use specific types for narrow try blocks; use `# noqa: BLE001 — <justification>` for intentional broad handlers (agent top-level, scraper resilience, graceful degradation)

### Timestamps
- User-facing timestamps (deadlines, kickoffs, `generated_at` stamps) must route through `fpl_cli/utils/time.py` (`format_deadline`, `format_kickoff`, `format_generated_at`, `now_uk`). Never `strftime` on a naive `datetime.now()` or print raw API ISO strings to users. Tool is UK-locked: display is always `Europe/London` with GMT/BST label. Internal datetime math stays UTC.

### Report Paths
- Generated reports are season-partitioned: `<output dir>/<season>/gw{N}-review.md`, `<research dir>/ai-scout-reports/<season>/gw{N}-scout-preview.md`. Filenames carry a gameweek but no season, so a flat directory lets a new season's GW21 report overwrite the previous season's. Resolve destinations with `resolve_output_dir(settings, output)` (`cli/_context.py`), which partitions the configured dir and an explicit `--output` alike; partition anything else with `season_partition()` (`fpl_cli/season.py`). `ReportAgent` writes to `output_dir` verbatim — never add a second season segment there
- Skills writing alongside these reports take the label from `fpl status --format json` (`metadata.season`), never hardcoded

### Commits & Changelog
- Commit subjects and PR titles follow conventional commits. `feat:`/`fix:`/`refactor:`/`perf:` become release-notes lines via git-cliff (`cliff.toml`); `chore:`/`docs:`/`ci:`/`test:`/`style:` and merge commits are skipped
- A changelog-visible subject must read as a standalone user-facing change — the release pipeline publishes it verbatim in CHANGELOG.md and the GitHub release
- `fix:`/`feat:` describe changes relative to main. Follow-up commits addressing review feedback on your own unmerged PR are internal iteration, not changelog entries — use `chore(review): <what changed>`
- PRs are squash-merged with the PR title as the commit subject on main, so the title IS the changelog entry — write it as the user-facing change and keep each PR single-purpose so one title can describe it. CI enforces the format (`pr-title.yml`)
- PR bodies follow the create-pr skill's description style (`.agents/skills/create-pr/SKILL.md`): why-first first-person prose, no boilerplate sections, `Closes #N` in the body (never the title), caveats and verification gaps in italics
- Skill changes take the type of their audience: product skills (gw-prep, squad-builder, update-gw-prep, preview-ingest) are user-facing surface — `feat:`/`fix:`, changelog-visible; process skills (release, release-notes, create-pr) are maintainer tooling — `docs:`/`chore:`, skipped

## FPL Domain Knowledge
`docs/fpl-rules.md` is the authority on scoring, BPS, chips, transfers, waivers and the classic/draft split — read it rather than reasoning from memory, and correct it there when a rule changes. The shape that drives most code:
- Chips: each available **twice** per season, split at the GW19 deadline (Wildcard, Free Hit, Bench Boost, Triple Captain); one chip per gameweek
- Transfers: 1 free/GW, banked up to 5; extras cost 4pts each; Wildcard/Free Hit preserve banked transfers
- Draft has no captains, no budget, no transfers and no chips — acquisition is via waivers and free agents

## Rules
- This repo is public: never put real manager or league names, entry/league IDs, or FPL account details into commits, PR titles or bodies, issues, or CHANGELOG.md. Use placeholders when quoting generated reports from the configured `reports.output_dir`. Footballer and club names are fine — they are public data. Before pushing, check the branch for a pre-existing leak and flag it rather than pushing over it.
- Verify before asserting: don't state that a command, file, function, or data point exists without checking first (read the file, run the command, grep for the name)
- Reviewing a PR by number: the session's working branch is never the PR's branch (a review session gets its own throwaway branch off main), so local files can silently be at a different commit than the PR — possibly missing the PR's changes entirely, or, worse, containing unrelated already-merged work that looks plausible but isn't in the diff. Ground every finding in the PR's actual diff (fetch via the GitHub API/`gh`, or check out the PR's head commit) before citing a file or line number, and re-verify that grounding before posting comments — don't trust a subagent's file/line citations without checking they match a file the PR's `get_files` list actually contains.
- Find-and-replace: review each replacement in context - don't blindly replace substrings in unrelated identifiers
- Removing/replacing X: new implementation must have zero dependencies on X
- Repeated convention violations: suggest a ruff lint rule to enforce automatically
- README must stay in sync: any CLI command added/changed/removed requires updating the relevant job section under `## Usage` (After the Gameweek, Checking Your Setup, Scouting Players, Before the Deadline, Strategic Planning, Season Preview Intel, Custom Analysis, JSON Output) and, if the command has detailed flags or formulas, `docs/command-reference.md`
- Architecture doc must stay in sync: adding a new agent, service, API client, or CLI command requires updating `docs/architecture.md`
- TOOLS.md must stay in sync: adding, removing, or changing a CLI command, analysis agent, or skill requires updating `.agents/TOOLS.md`
- AGENTS.md must stay in sync: any change to project instructions in CLAUDE.md requires the same change in AGENTS.md
- CONTRIBUTING.md must stay in sync: changes to commit/PR conventions, CI checks, or the release pipeline require the same change in CONTRIBUTING.md (the human-facing copy)
- CLI changes require corresponding unit tests
- Changing a function's return format: update existing tests to match and confirm pytest passes
- Tests: `pytest-asyncio` with `asyncio_mode = "auto"`, factories in `tests/conftest.py` — `make_player()`, `make_team()`, `make_fixture()`, `make_history_row()`, and the draft equivalents (`make_draft_player()`, `make_draft_team()`, `make_draft_league_entry()`, `make_draft_standing()`)
- The autouse `_isolated_user_dirs` fixture repoints all three `FPL_CLI_*` dirs at `tmp_path` and clears the resolver caches, so no test reads or writes real user data. It only works because paths resolve at point of use — a module-level constant binds during collection, before the fixture runs, and would hit the real location
- Tests are hermetic: `pytest-socket` blocks all network via `addopts` (`--disable-socket --allow-unix-socket`), so a test that reaches a live endpoint fails when written. Stub the seam instead (see `stub_scoring_network_seams` in `tests/conftest.py` and the autouse fixtures in `tests/test_cli_player.py`); a test that genuinely needs a socket opts out with `@pytest.mark.enable_socket`. Watch for call sites that degrade gracefully on network failure — they hide the dependency until an upstream change makes a successful response wrong
- After any task touching Python files, run `pyright` as a final check alongside ruff and pytest
