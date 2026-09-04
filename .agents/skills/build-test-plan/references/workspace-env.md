# The fpl-workspace run environment

What the plan can assume about the session that will execute it, so the
preflight asserts the right things and the tests use the right paths.

**Verify before you rely on it.** The vault owns these facts, not this
file. Read `/home/user/fpl-workspace/docs/directory-setup.md` and its
`CLAUDE.md` when you build a plan, and prefer what they say. What follows
is the shape to look for and the parts that have bitten before.

## Where the runner works

The vault root, `/home/user/fpl-workspace`. Report paths in the plan are
cwd-relative from there (`01_Reports/<season>/…`), which is how the
reference plan reads and what a runner checking output will type.

`fpl` is on PATH as an installed `fplkit` — there is no venv to activate
and no fpl-cli source in the vault. Two consequences for the plan:

- Tests exercise the CLI, never the library.
- The runner **cannot fix fpl-cli from there**. Say so in the wrap-up:
  findings get reported to the user, not patched.

## Directories and environment

| Variable | Expected | Notes |
| --- | --- | --- |
| `FPL_CLI_CONFIG_DIR` | `<vault>/config` | Hand-maintained config; `.env` loads from here |
| `FPL_CLI_DATA_DIR` | `<vault>/data` | Generated state, tracked in git |
| `FPL_CLI_CACHE_DIR` | unset | Falls back to `~/.cache/fpl-cli`; disposable |
| `FPL_EMAIL` / `FPL_PASSWORD` | set | Playwright scraper (sell prices) |
| `PERPLEXITY_API_KEY` | set | Research LLM |
| `FPL_ANTHROPIC_API_KEY` | set | Synthesis LLM — see below |
| `FOOTBALL_DATA_API_KEY` | set | Standings provider |
| `FPL_BROWSER_IGNORE_CERTS` | `1` | Required in cloud sessions; the agent proxy re-signs TLS |

Both directory overrides must be **absolute**; a relative value is
rejected. The preflight should assert presence of keys, never print values.

**The `ANTHROPIC_API_KEY` trap.** Claude Code on the web reserves that
variable name and strips it from the container, so a secret stored under it
never arrives — not even in PID 1's environment. The vault stores it as
`FPL_ANTHROPIC_API_KEY` and a SessionStart hook copies it into
`config/.env`, which the CLI loads itself. A `--summarise` test that aborts
on a missing key is usually this, not an fpl-cli bug: have the preflight
check `config/.env` carries `ANTHROPIC_API_KEY` so the plan distinguishes
the two.

## What lives where

- `config/` — `settings.yaml` (league/entry IDs, fines, report dirs, LLM
  providers), `team_managers.yaml`, `team_ratings_overrides.yaml`,
  `previews/` (season intel), gitignored `.env`.
- `data/` — `team_ratings.yaml`, `team_finances.json`, `chip_plan.json`,
  `player_prior.yaml`, `returnee_snapshot.json`, and `league_history/`.
  `league_history_counters/` and `debug/` are gitignored and rebuildable.
- `01_Reports/<season>/` — generated reports.
- `02_Research/<source>/<season>/` — research output; note the season sits
  *inside* each source directory here, unlike reports.

**The ledger is the one irreplaceable thing.** `data/league_history/` is
append-only and cannot be regenerated: the FPL API keeps per-gameweek
detail only for the current season and collapses it at the July rollover.
Any test that touches it must be non-destructive or reversible, and the
wrap-up should have the runner commit new ledger rows. Its `.ndjson` files
merge by union, so committing from two sessions is safe.

## Season state gates tests

The plan runs at whatever point in the season the runner is at, and that
decides what is testable. Tests that need more than a single completed
gameweek: point-in-time replay, week-over-week snapshot transitions, form
and trend signals, "don't over-weight last season" rating checks, streak
counters. Early-season quality scores are compressed by small samples,
which makes threshold assertions ("elite reads 85+") genuinely ambiguous —
if you write one, say what regime it assumes.

The preflight discovers the state (`fpl status --format json` gives
`metadata.season` and the gameweek), so the plan should reference those
values rather than naming a season or gameweek anywhere.

## Costs the plan should flag

Say up front which flags spend real money or quota, so the runner doesn't
loop them:

- `--summarise` — research + synthesis LLM calls.
- `--enrich` — Perplexity calls, one per candidate; rate limits are
  reachable and a 429 is environmental, not a bug.
- `--backfill-detail` — one FPL request per manager per gameweek.
- `sell-prices --refresh` — Playwright browser session against the FPL
  login.

## Anonymity

fpl-cli is public and the vault is private, which sets the rule in both
directions: the plan document must carry no manager or league names and no
entry/league IDs, because it is written from a public repo and describes
one; the Results log the runner fills in may reference them, because it
stays in the vault. Anything reported back to fpl-cli as an issue gets the
plan's rule, not the log's.
