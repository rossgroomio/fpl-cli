# FPL CLI Architecture

```mermaid
flowchart TB
    subgraph CLI["CLI Layer"]
        cli[cli/__init__.py<br/>FormatAwareGroup]
        ctx[_context.py<br/>Format detection]
        helpers[_helpers.py / _json.py<br/>Display & JSON output]
    end

    subgraph Config["Configuration"]
        defaults[config/defaults.yaml]
        settings[~/...fpl-cli/settings.yaml]
        env[".env<br/>API Keys"]
    end

    subgraph Agents["Agent Layer"]
        subgraph DataAgents["Data Agents"]
            fixture[FixtureAgent]
            price[PriceAgent]
            scout[ScoutAgent]
        end

        subgraph AnalysisAgents["Analysis Agents"]
            stats[StatsAgent]
            captain[CaptainAgent]
            squad_analyzer[SquadAnalyzerAgent]
            bench[BenchOrderAgent]
            starting_xi[StartingXIAgent]
            transfer_eval[TransferEvalAgent]
        end

        subgraph ActionAgents["Action Agents"]
            waiver[WaiverAgent]
        end

        subgraph OrchAgents["Orchestration Agents"]
            report[ReportAgent]
        end
    end

    subgraph Services["Services"]
        player_scoring[player_scoring<br/>Scoring engines]
        player_prior[player_prior<br/>Bayesian early-season confidence]
        team_ratings[TeamRatingsService<br/>1-7 strength scale]
        matchup[matchup<br/>3-GW matchup scores]
        fixture_preds[FixturePredictionsService<br/>BGW/DGW predictions]
        team_form[team_form<br/>Form stats]
    end

    subgraph APIClients["API Clients"]
        fpl_client[FPLClient]
        draft_client[FPLDraftClient]
        understat_client[UnderstatClient]
        vaastav_client[VaastavClient]
        football_data[FootballDataClient]
    end

    subgraph LLMProviders["LLM Providers"]
        anthropic_prov[AnthropicProvider]
        openai_prov[OpenAICompatProvider]
        perplexity_prov[PerplexityProvider]
    end

    subgraph Scraper["Scraper"]
        scraper[FPLPriceScraper]
    end

    subgraph Models["Data Models"]
        player_m[Player]
        fixture_m[Fixture]
        team_m[Team]
        chip_plan[ChipPlan]
        result_m[AgentResult]
        types_m[TypedDicts<br/>CaptainCandidate, WaiverTarget,<br/>EnrichedPlayer, etc.]
    end

    subgraph Prompts["Prompts"]
        scout_p[scout.py]
        review_p[review.py]
        recap_p[league_recap.py]
    end

    subgraph Templates["Jinja2 Templates"]
        preview_tmpl[gw_preview.md.j2]
        review_tmpl[gw_review.md.j2]
        recap_tmpl[gw_league_recap.md.j2]
    end

    subgraph External["External Services"]
        fpl_api[("FPL API")]
        draft_api[("Draft API")]
        understat_web[("understat.com")]
        vaastav_gh[("vaastav/FPL<br/>GitHub")]
        football_api[("football-data.org")]
        llm_apis[("Claude / OpenAI /<br/>Perplexity APIs")]
        fpl_web[("FPL Website")]
    end

    subgraph Output["Output"]
        console[Rich Console]
        obsidian[("Obsidian Vault<br/>01_Reports/")]
    end

    %% CLI connections
    cli --> ctx
    cli --> helpers
    cli --> defaults & settings & env
    cli --> DataAgents & AnalysisAgents & ActionAgents & OrchAgents

    %% Agent -> Service connections
    fixture --> team_ratings & fixture_preds & matchup
    captain --> player_scoring
    bench --> player_scoring
    starting_xi --> player_scoring
    transfer_eval --> player_scoring
    stats --> player_scoring
    waiver --> player_scoring
    scout --> Prompts

    %% Agent -> API connections
    fixture --> fpl_client
    price --> fpl_client
    stats --> fpl_client
    scout --> LLMProviders
    captain --> fpl_client
    squad_analyzer --> fpl_client
    bench --> fpl_client
    starting_xi --> fpl_client
    transfer_eval --> fpl_client
    waiver --> draft_client & fpl_client

    %% Service -> API connections
    player_prior --> vaastav_client
    player_scoring --> player_prior
    team_ratings --> understat_client
    team_ratings --> football_data

    %% Orchestration
    report --> Templates --> obsidian
    report --> console

    %% LLM Provider connections
    anthropic_prov & openai_prov & perplexity_prov --> llm_apis

    %% API Client -> External
    fpl_client --> fpl_api
    draft_client --> draft_api
    understat_client --> understat_web
    vaastav_client --> vaastav_gh
    football_data --> football_api
    scraper --> fpl_web

    %% Models
    Agents -.-> Models
    Services -.-> Models

    %% Styling
    classDef cli fill:#e1f5fe,stroke:#01579b
    classDef data fill:#e8f5e9,stroke:#1b5e20
    classDef analysis fill:#fff3e0,stroke:#e65100
    classDef action fill:#ffebee,stroke:#b71c1c
    classDef orch fill:#e3f2fd,stroke:#0d47a1
    classDef service fill:#f1f8e9,stroke:#33691e
    classDef api fill:#fce4ec,stroke:#880e4f
    classDef external fill:#f5f5f5,stroke:#424242
    classDef output fill:#e8eaf6,stroke:#1a237e

    class cli,ctx,helpers cli
    class fixture,price,scout data
    class stats,captain,squad_analyzer,bench,starting_xi,transfer_eval analysis
    class waiver action
    class report orch
    class player_scoring,team_ratings,matchup,fixture_preds,team_form service
    class fpl_client,draft_client,understat_client,vaastav_client,football_data,scraper,anthropic_prov,openai_prov,perplexity_prov api
    class fpl_api,draft_api,understat_web,vaastav_gh,football_api,llm_apis,fpl_web external
    class console,obsidian output
```

## Data Flow: Preview Pipeline

```mermaid
flowchart LR
    subgraph Input["User Input"]
        cmd["fpl preview --scout --save"]
    end

    subgraph Pipeline["Agent Pipeline"]
        direction TB
        A[FixtureAgent] -->|fixtures, FDR, form| B[StatsAgent]
        B -->|players, xG, value picks| C[CaptainAgent]
        C -->|captain ranks| D[ScoutAgent]
        D -->|expert analysis| E[ReportAgent]
    end

    subgraph Context["Shared Context"]
        ctx[("context dict<br/>gameweek, fixtures,<br/>players, stats,<br/>team_form")]
    end

    subgraph Output["Output"]
        file["gw{N}-preview.md"]
        vault[("Obsidian")]
    end

    Input --> Pipeline
    Pipeline <--> Context
    E --> file --> vault

    style ctx fill:#fff9c4,stroke:#f57f17
```

## Agent Inheritance

```mermaid
classDiagram
    class Agent {
        <<abstract>>
        +name: str
        +description: str
        +config: dict
        +run(context) AgentResult*
        +close()
        +validate()
        +log() / log_success() / log_warning() / log_error()
        #_create_result() AgentResult
    }

    class AgentResult {
        +agent_name: str
        +status: AgentStatus
        +data: dict
        +message: str
        +errors: list
        +timestamp: datetime
        +requires_approval: bool
        +pending_actions: list
        +success: bool
    }

    class AgentStatus {
        <<enumeration>>
        SUCCESS
        PARTIAL
        FAILED
        PENDING_APPROVAL
    }

    Agent --> AgentResult : returns
    AgentResult --> AgentStatus : has

    Agent <|-- FixtureAgent
    Agent <|-- PriceAgent
    Agent <|-- StatsAgent
    Agent <|-- ScoutAgent
    Agent <|-- CaptainAgent
    Agent <|-- SquadAnalyzerAgent
    Agent <|-- BenchOrderAgent
    Agent <|-- StartingXIAgent
    Agent <|-- TransferEvalAgent
    Agent <|-- WaiverAgent
    Agent <|-- ReportAgent
```

## CLI Command Mapping

```mermaid
flowchart LR
    subgraph Direct["Direct API Commands"]
        status["status"]
        fixtures["fixtures"]
        player["player"]
        stats["stats"]
        history["history"]
        league["league"]
        price_hist["price-history"]
        chips["chips"]
        ratings["ratings"]
        credentials["credentials"]
        init["init"]
    end

    subgraph ViaAgent["Agent-Backed Commands"]
        preview["preview"]
        review["review"]
        recap["league-recap"]
        cap["captain"]
        fdr["fdr"]
        xg["xg"]
        diff["differentials"]
        tgt["targets"]
        pr["price-changes"]
        waivers["waivers"]
        squad["squad"]
        transfer_eval["transfer-eval"]
    end

    subgraph Agents["Agents"]
        FA[FixtureAgent]
        PA[PriceAgent]
        SA[StatsAgent]
        SCA[ScoutAgent]
        CA[CaptainAgent]
        SQA[SquadAnalyzerAgent]
        WA[WaiverAgent]
        RA[ReportAgent]
        TEA[TransferEvalAgent]
    end

    preview --> FA & SA & CA & SCA & RA
    review --> RA
    recap --> RA
    cap --> CA
    fdr --> FA
    xg --> SA
    diff --> SA
    tgt --> SA
    pr --> PA
    waivers --> WA
    squad --> SQA
    transfer_eval --> TEA

    style Direct fill:#e8f5e9,stroke:#1b5e20
    style ViaAgent fill:#fff3e0,stroke:#e65100
```

### Format Awareness

Commands are classified by format applicability:

| Category | Commands |
|---|---|
| **Classic only** | `captain`, `targets`, `differentials`, `chips`, `credentials` |
| **Draft only** | `waivers` |
| **General** | Everything else (format-gated sections within) |

`FormatAwareGroup` auto-hides inapplicable commands in `--help` based on configured format. Format resolved from settings (`classic_entry_id` / `draft_league_id`) or `FPL_FORMAT` env var.

### Custom Analysis Gating

Commands are independently classified by the `custom_analysis` toggle:

| Category | Commands | When opted out |
|---|---|---|
| **Pure-experimental** | `captain`, `targets`, `differentials`, `waivers`, `allocate`, `transfer-eval`, `ratings` | Unregistered from CLI |
| **Mixed** | `stats`, `xg`, `fdr`, `preview` | Experimental columns/sections stripped |
| **Data-only** | Everything else | No change |

`FormatAwareGroup.list_commands()` and `get_command()` filter out the `EXPERIMENTAL` frozenset when `custom_analysis` is off. Mixed commands check `is_custom_analysis_enabled()` within their `_run()` to gate experimental columns/sections. Both filters (format and experimental) are independent and must both pass.

## Services Layer

```mermaid
flowchart TB
    subgraph Scoring["player_scoring.py"]
        direction TB
        prepare["prepare_scoring_data() → ScoringData"]
        scoring_ctx["ScoringContext + build_scoring_context()"]
        build_matchups["build_fixture_matchups()"]
        compute_agg["compute_aggregate_matchup()"]
        captain_score["calculate_captain_score()"]
        target_score["calculate_target_score()"]
        diff_score["calculate_differential_score()"]
        waiver_score["calculate_waiver_score()"]
        bench_score["calculate_bench_score()"]
        lineup_score["calculate_lineup_score()"]
        select_xi["select_starting_xi()"]
        build_eval["build_player_evaluation()"]
        prepare --> scoring_ctx
        scoring_ctx --> build_matchups & compute_agg
    end

    subgraph Ratings["team_ratings.py"]
        direction TB
        svc["TeamRatingsService"]
        calc["TeamRatingsCalculator"]
        svc --> calc
    end

    subgraph Matchup["matchup.py"]
        direction TB
        matchup_fn["calculate_matchup_score()"]
        gw_maps["build_gw_fixture_maps()"]
        compute_3gw["compute_3gw_matchup()"]
    end

    subgraph FP["fixture_predictions.py"]
        direction TB
        fp_svc["FixturePredictionsService"]
        find_blank["find_blank_gameweeks()"]
        find_double["find_double_gameweeks()"]
    end

    subgraph TF["team_form.py"]
        tf_fn["calculate_team_form()"]
    end

    %% Cross-service dependencies
    Matchup --> TF
    Ratings --> UnderstatClient & FootballDataClient

    style Scoring fill:#f1f8e9,stroke:#33691e
    style Ratings fill:#f1f8e9,stroke:#33691e
    style Matchup fill:#f1f8e9,stroke:#33691e
    style FP fill:#f1f8e9,stroke:#33691e
    style TF fill:#f1f8e9,stroke:#33691e
```

**player_scoring** - Central scoring engine. `prepare_scoring_data()` is the shared entry point for all scoring agents' data preparation - fetches teams, fixtures, next GW, creates TeamRatingsService, and builds a `ScoringContext`, returning everything in a `ScoringData` frozen dataclass. Optional `include_players`/`include_understat`/`include_history` flags control additional data fetching. `include_history` batch-fetches per-GW player history via `get_player_detail()` for all players with minutes > 0, enabling `compute_form_trajectory()` - a median-filtered slope of recent GW points that returns a multiplier (0.8-1.2) applied to the form contribution in all scoring contexts. `ScoringContext` (frozen dataclass) holds pre-fetched data (team map, fixture map, ratings service, optional team form/understat). `build_scoring_context()` constructs it (called internally by `prepare_scoring_data()`). `build_fixture_matchups()` produces per-fixture `FixtureMatchup` objects with opponent FDR (used for captain fixture classification and display; no longer an additive scoring component). `compute_aggregate_matchup()` returns a scalar 3GW average matchup score (used by stats/waiver). All formulas define weights via `StatWeight`-based `QualityWeights` instances for cross-formula comparability. Two scoring families:

- **Ownership family** (target/diff/waiver): All three route through `_calculate_quality_based_score()` / `_calculate_quality_based_raw()` with `TARGET_QUALITY_WEIGHTS`, `DIFFERENTIAL_QUALITY_WEIGHTS`, `WAIVER_QUALITY_WEIGHTS`. Shared flow: quality baseline via `calculate_player_quality_score()`, underperformance regression bonus, 3-GW matchup (scalar average, weight 0.75 via `_matchup_bonus`), availability penalty (-3pt when flagged < 75%). Waiver uses `mins_factor_override` for a stricter combined factor (availability * per-appearance) because draft waivers are a season commitment; target/diff use standard `mins_factor`. Waiver adds position-need and team-stacking adjustments post-quality. All three include `penalty_xG` via `StatWeight`.
- **Single-GW family** (captain/bench/lineup/allocator horizon=1): `calculate_single_gw_core()` with `GW_SELECTION_WEIGHTS`. Per-fixture matchup scores summed (not averaged), weighted by `matchup_weight` (captain 2.0, bench/lineup/allocator 1.5). Captain and bench share this core; bench adds coverage and set-piece bonuses, normalises via `BENCH_CEILING` (raw `priority_score_raw` exposed in output). Lineup uses `calculate_lineup_score()` + `select_starting_xi()` to pick the optimal starting XI from a 15-man squad. Squad allocator uses `score_all_players_sgw()` when `--horizon 1` to feed single-GW scores as solver coefficients (no fixture coefficient step, no shrinkage). Captain's pen bonus is `StatWeight`-derived. FDR is not an additive component in either family.

`BenchOrderAgent` is enriched with Understat data (npxG, xGChain, penalty_xG) where available.

Both families' normalised scores are subject to early-season confidence shrinkage via `shrink_scores()` (GW1-10). Per-player confidence is derived from prior-season pts/90 (vaastav data) via `player_prior.py`. `prepare_scoring_data(include_prior=True)` fetches priors into `ScoringData.player_priors`; each agent calls `shrink_scores()` between scoring and ranking.

**player_prior** - Bayesian early-season confidence. `generate_player_prior()` computes per-player `prior_strength` (percentile rank of pts/90 within position) and `confidence` (shrinkage control). Price-based fallback for players without PL history. YAML cache (`config/player_prior.yaml`) with season/GW invalidation. Constants: `REGRESSION_CONSTANT=6`, `CUTOFF_GW=10`.

**TeamRatingsService** - Persists team strength ratings (1-7 scale, per axis: atk_home/away, def_home/away) to `config/team_ratings.yaml`. Auto-refreshes when stale. Supports fixture-based and xG-based calculation. Blends with prior ratings before GW5.

**matchup** - Computes matchup scores (0-10) using team form, opponent form, venue, and position. `compute_3gw_matchup()` applies recency-weighted window `[0.5, 0.3, 0.2]`.

**FixturePredictionsService** - Reads `config/fixture_predictions.yaml` for predicted BGW/DGW data with confidence levels. Pure functions `find_blank_gameweeks()` / `find_double_gameweeks()` detect from live fixture data.

**team_form** - Calculates rolling form stats (last 6 matches, venue splits, league position).

## LLM Provider Abstraction

```mermaid
classDiagram
    class LLMResponse {
        +content: str
        +model: str
        +usage: TokenUsage
        +citations: list~str~
    }

    class OpenAICompatProvider {
        +query(prompt, system_prompt) LLMResponse
        +post_process(content) str
        +close()
        #_build_payload()
        #_parse_response()
    }

    class PerplexityProvider {
        +DEFAULT_MODEL: sonar-pro
        #_build_payload() adds web_search_options
        #_parse_response() extracts citations
    }

    class AnthropicProvider {
        +DEFAULT_MODEL: claude-sonnet-4-6
        +query(prompt, system_prompt) LLMResponse
        +post_process(content) str
        +close()
    }

    OpenAICompatProvider <|-- PerplexityProvider
    OpenAICompatProvider ..> LLMResponse
    AnthropicProvider ..> LLMResponse
```

All providers share the `LLMResponse` contract. `OpenAICompatProvider` supports OpenAI, Groq, Together, Ollama via configurable `base_url`. Provider selection configured in settings.

## API Clients

| Client | External Source | Purpose |
|---|---|---|
| `FPLClient` | FPL API | Players, fixtures, managers, teams, bootstrap-static (cached) |
| `FPLDraftClient` | FPL Draft API | Draft leagues, waivers, squad data |
| `UnderstatClient` | understat.com | npxG, xA, xGChain, xGBuildup per-90 stats |
| `VaastavClient` | vaastav/FPL GitHub | Historical CSV data (3-4 seasons), price trends, GW-level profiles |
| `FootballDataClient` | football-data.org | League standings, match results |
| `FPLPriceScraper` | FPL website | Price change scraping (needs credentials) |

## Model Relationships

```mermaid
erDiagram
    Player ||--o{ Fixture : "plays in"
    Team ||--o{ Player : "has"
    Team ||--o{ Fixture : "participates"

    Player {
        int id PK
        int code "stable cross-season ID"
        string web_name
        int team_id FK
        int position "element_type alias"
        int now_cost "in 0.1m units"
        float form
        float expected_goals
        float expected_assists
        float expected_goal_involvements
        float defensive_contribution_per_90
        string status "PlayerStatus enum"
    }

    Fixture {
        int id PK
        int gameweek "event alias"
        int home_team_id FK
        int away_team_id FK
        int home_difficulty "1-5"
        int away_difficulty "1-5"
        datetime kickoff_time
        bool finished
    }

    Team {
        int id PK
        string name
        string short_name
        int strength
        int strength_attack_home
        int strength_attack_away
        int strength_defence_home
        int strength_defence_away
        string form "W/D/L string"
    }

    ChipPlan {
        list chips "PlannedChip list"
        list chips_used "from API"
        int current_gw
    }

    ChipPlan ||--o{ PlannedChip : "contains"

    PlannedChip {
        ChipType chip "WC/FH/BB/TC"
        int gameweek
        string notes
    }

    AgentResult {
        string agent_name
        AgentStatus status
        dict data
        list errors
    }
```

## Module Map

```
fpl_cli/
├── cli/                          # Click commands & groups
│   ├── __init__.py               # main() entry point, command registration
│   ├── _context.py               # Format enum, CLIContext, FormatAwareGroup (format + experimental gating), settings loader
│   ├── _helpers.py               # Shared display utilities
│   ├── _json.py                  # JSON output serialisation
│   ├── _banner.py                # Startup banner
│   ├── _plan_grid.py             # Fixture grid rendering
│   ├── _review_*.py              # Review command helpers (analysis, classic, draft, summarisation)
│   ├── _league_recap_*.py        # League recap helpers & types
│   ├── _fines.py / _fines_config.py  # League fines system
│   └── [command files]           # One file per command/group
├── agents/
│   ├── base.py                   # Agent ABC, AgentResult, AgentStatus
│   ├── common.py                 # Shared: enrich_player, fetch_understat_lookup, draft helpers
│   ├── data/                     # FixtureAgent, PriceAgent, ScoutAgent
│   ├── analysis/                 # StatsAgent, CaptainAgent, SquadAnalyzerAgent, BenchOrderAgent, StartingXIAgent, TransferEvalAgent
│   ├── action/                   # WaiverAgent
│   └── orchestration/            # ReportAgent
├── api/
│   ├── fpl.py                    # FPLClient (main API, caches bootstrap-static)
│   ├── fpl_draft.py              # FPLDraftClient
│   ├── understat.py              # UnderstatClient + match_fpl_to_understat()
│   ├── vaastav.py                # VaastavClient (historical seasons, GW trends)
│   ├── football_data.py          # FootballDataClient (standings, match results)
│   └── providers/                # LLM provider abstraction
│       ├── _models.py            # LLMResponse, TokenUsage, ProviderError
│       ├── anthropic.py          # AnthropicProvider
│       ├── openai_compat.py      # OpenAICompatProvider (OpenAI, Groq, Together, Ollama)
│       └── perplexity.py         # PerplexityProvider (extends OpenAICompat)
├── services/
│   ├── player_scoring.py         # Scoring engines + prepare_scoring_data() + shrink_scores()
│   ├── player_prior.py           # Player prior (Bayesian early-season confidence)
│   ├── team_ratings.py           # TeamRatingsService + Calculator (1-7 scale)
│   ├── team_ratings_prior.py     # Pre-GW5 prior ratings for blending
│   ├── matchup.py                # Fixture matchup scoring (0-10)
│   ├── fixture_predictions.py    # BGW/DGW predictions from YAML + live detection
│   ├── squad_allocator.py        # ILP squad allocator (PuLP CBC) - score, fixture coefficients, solver. Horizon-aware: horizon=1 uses single-GW scoring (GW_SELECTION_WEIGHTS), horizon>=2 uses ownership-family quality (VALUE_QUALITY_WEIGHTS). Chip-aware: --bench-discount (Free Hit), --bench-boost-gw (Bench Boost per-GW override to 1.0), --sell-prices (WC/FH sell-price budget correction via price_overrides dict)
│   └── team_form.py              # Rolling team form stats
├── models/
│   ├── player.py                 # Player, PlayerStatus, PlayerPosition, POSITION_MAP
│   ├── team.py                   # Team
│   ├── fixture.py                # Fixture
│   ├── chip_plan.py              # ChipPlan, ChipType, PlannedChip, UsedChip
│   └── types.py                  # TypedDicts: CaptainCandidate, WaiverTarget, EnrichedPlayer, etc.
├── prompts/
│   ├── scout.py                  # ScoutAgent system/user prompts
│   ├── review.py                 # Review research prompts
│   └── league_recap.py           # League recap synthesis prompts
├── parsers/
│   └── recommendations.py        # Parse gw{N}-recommendations.md into structured decisions
├── scraper/
│   └── fpl_prices.py             # FPLPriceScraper (needs FPL_EMAIL/FPL_PASSWORD)
├── paths.py                      # PROJECT_ROOT, CONFIG_DIR, DATA_DIR, TEMPLATE_DIR
├── season.py                     # Season year detection, TOTAL_GAMEWEEKS, CHIP_SPLIT_GW
└── constants.py                  # MIN_MINUTES_FOR_PER90

config/
├── defaults.yaml                 # Committed project defaults (includes custom_analysis: false)
├── team_ratings.yaml             # Cached team strength ratings
├── player_prior.yaml             # Cached player priors (season + GW invalidation)
└── fixture_predictions.yaml      # BGW/DGW predictions

data/
└── chip_plan.json                # User's chip plan (runtime)

templates/
├── gw_preview.md.j2              # Preview report template
├── gw_review.md.j2               # Review report template
└── gw_league_recap.md.j2         # League recap template
```

## Agent Skills

```
.agents/
├── README.md                     # Directory purpose and adaptation guide
└── skills/                       # Showcase agent skills (canonical location)
    ├── gw-prep/                  # Gameweek preparation (parallel sub-agents)
    │   ├── SKILL.md
    │   ├── references/
    │   │   ├── rules.md          # Transfer/waiver/selection rules
    │   │   └── output-template.md
    │   └── scripts/
    │       ├── bench_order.py    # BenchOrderAgent wrapper (name -> ID resolution)
    │       ├── starting_xi.py   # StartingXIAgent wrapper (name -> ID resolution)
    │       └── transfer_eval.py # TransferEvalAgent wrapper (name -> ID resolution)
    ├── update-gw-prep/           # Second-pass addendum with supplementary data
    │   └── SKILL.md
    ├── squad-builder/            # 5-mode squad optimisation (WC/FH/season-start/draft/redraft)
    │   ├── SKILL.md
    │   └── references/
    │       ├── rules.md
    │       └── output-template.md
```

**Discovery:** `.claude/skills/` is a symlink to `../.agents/skills`. Claude Code discovers skills via the symlink; other tools read `.agents/skills/` directly. `AGENTS.md` symlinks to `CLAUDE.md` for multi-agent compatibility.

**Adaptation:** Skills are showcase examples with `<!-- ADAPT: ... -->` comments at customisation points. Output paths use `[YOUR_OUTPUT_DIR]` placeholders. All CLI data gathering uses `--format json`.

## Config Resolution

```mermaid
flowchart LR
    A[config/defaults.yaml] -->|deep merge| C[Effective Config]
    B["~/Library/.../fpl-cli/settings.yaml<br/>(or FPL_CLI_CONFIG_DIR)"] -->|overrides| C
    D["FPL_FORMAT env var"] -->|overrides format| C
    E[".env API keys"] --> C
```

User settings deep-merged over committed defaults via `platformdirs`. Format auto-detected from which entry IDs are configured (classic, draft, or both).
