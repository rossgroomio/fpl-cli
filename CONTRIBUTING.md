# Contributing

fpl-cli is a solo project where most PRs are authored by Claude Code
sessions. These conventions exist less for etiquette and more to keep the
automated release pipeline meaningful: commit subjects that reach main are
published verbatim in `CHANGELOG.md` and the GitHub release notes.

## Dev setup & checks

See the [Development section of the README](README.md#development) for
setup. Every PR must pass CI, which runs:

```console
$ ruff check fpl_cli/ scripts/    # Lint
$ pyright fpl_cli/ scripts/       # Type check
$ pytest                 # Tests
$ hatch build            # Package build
```

Two of the ruff rules are conventions rather than bug-catchers, so they are
worth knowing before a first PR: every function signature in `fpl_cli/` and
`scripts/` carries full type annotations (`ANN`; `tests/` are exempt, and a
bare `Any` is allowed where it is the honest type), and imports are absolute,
never relative (`TID252`).

The test suite is hermetic: `pytest-socket` blocks all network access
(`--disable-socket --allow-unix-socket` in `addopts`), so a test that
reaches a live endpoint fails immediately rather than passing against
whatever the endpoint returned that day. Stub the network seam instead —
the shared `stub_scoring_network_seams` fixture in `tests/conftest.py`
and the autouse fixtures in `tests/test_cli_player.py` show the pattern. A test that genuinely needs
a socket opts out with `@pytest.mark.enable_socket`.

Because the suite is hermetic, it pins fpl-cli's *assumption* of each
external provider's schema, not the schema itself. A scheduled
`provider-probe` job in `.github/workflows/ci.yml` covers that gap: on
the weekly cron (and on manual dispatch) it runs
`fpl doctor --providers --format json` against the live providers and
fails only on "broken" (shape drift), never on transient
unreachability. It does not run on PRs.

A separate `PR Title` check (`.github/workflows/pr-title.yml`) fails the
PR when the title doesn't follow the conventional-commit format below.

## Dependencies

There is no lockfile. `pip install -e ".[dev]"` — locally and in CI —
resolves fresh from the ranges in `pyproject.toml`, which is the same
resolution `pip install fplkit` gives a user: CI tests what users get. A
lock could not change that for anyone installing from PyPI, and it would
keep CI green on the pinned version while a fresh install broke — the
class of failure #80 hit with PuLP. The `requirements.lock` that used to
sit at the repo root was that in miniature: three runtime dependencies
short, generated for macOS and Python 3.12 so a hash-checked install on
the Linux/3.11 CI runner refused it, and read by nothing (#81). Don't add
another. The weekly CI cron re-resolves from scratch, so an upstream
release that breaks main is caught within a week even with no commit
behind it.

An upper bound is deliberate, not default. A runtime dependency gets one
only when its next major is a real prospect *and* the code leans on
surface that major is expected to change:

| Bound | Why |
|---|---|
| `httpx<1` | every API client, and `respx` in the tests couples to its transport internals; 0.28 already removed deprecated arguments and 1.0 is the announced cleanup |
| `pydantic<3` | every model is a v2 `BaseModel` with `ConfigDict`, aliases and `computed_field`; v3 is where the v2 deprecations are removed |
| `click<9` | the whole CLI, and `CliRunner` in every CLI test; 8.2 already changed how stderr is captured, and 9 removes the 8.x deprecations |
| `platformdirs<5` | decides where user config, data and cache live (`paths.py`); a major that moves a location strands existing users' files |
| `pulp<4` | 4.0 removes both solver calls `squad_allocator.py` makes (#80, #82) |

Everything else stays unbounded. `rich` and `keyring` bump majors
routinely without breaking anything here — a bound there is a PR every
few months for no protection — and the rest have no major on the
horizon. When a release does break main, fix the code or add a bound in
the PR that turns CI green, and add the row above. Dev tools are never
capped: CI runs them at latest, and a break there is caught and fixed on
the next PR without reaching a user.

`tests/test_dependencies.py` holds this table to `pyproject.toml` in
both directions and fails if a lockfile reappears.

## Commit subjects & PR titles

This repo is public. Keep real manager and league names, entry/league IDs, and FPL account details out of commit subjects, PR titles and bodies, issues, and the changelog — use placeholders when quoting a generated report. Footballer and club names are public data and are fine.

Both follow [Conventional Commits](https://www.conventionalcommits.org).
git-cliff (`cliff.toml`) turns them into changelog entries:

| Subject prefix | Changelog group |
|---|---|
| `feat:` | Features |
| `fix:` | Bug Fixes |
| `refactor:` | Refactoring |
| `perf:` | Performance |
| `chore:`, `docs:`, `ci:`, `test:`, `style:` | skipped |
| merge commits, `fix(tests):` | skipped |
| anything else | Other |

The rules that matter:

- A changelog-visible subject (`feat:`/`fix:`/`refactor:`/`perf:`) must read
  as a standalone user-facing change — it ships verbatim to users.
- `fix:` and `feat:` describe changes relative to main. Commits addressing
  review feedback on your own unmerged PR are internal iteration, not
  changelog entries — use `chore(review): <what changed>`.
- A breaking change uses the `!` marker (`feat!:`) or a `BREAKING CHANGE:`
  footer; it drives a major version bump at the next release.
- Skill changes take the type of their audience: a product skill (gw-prep,
  squad-builder, update-gw-prep, preview-ingest) is user-facing surface —
  `feat:`/`fix:`, changelog-visible; a process skill (release,
  release-notes, create-pr) is maintainer tooling — `docs:`/`chore:`,
  skipped.

### Merging

PRs are **squash-merged** (merge commits are disabled). The PR title —
with `(#N)` auto-appended by GitHub — becomes the single commit on main,
and therefore the changelog line. Write it as the user-facing change, and
keep each PR single-purpose so one title can describe it. Branch commits
never reach main, so they can iterate freely; the title is what ships.

PR descriptions are flowing first-person prose — open with why, skip the
boilerplate sections, put `Closes #N` in the body rather than the title,
and set caveats or verification gaps in italics. The full style (and the
workflow agent sessions use to open PRs) lives in
[`.agents/skills/create-pr/SKILL.md`](.agents/skills/create-pr/SKILL.md).

## Releases

Versioning is tag-driven (`hatch-vcs`) — no file carries a version number.
Versions follow [semver](https://semver.org/); tags use the `vX.Y.Z` format:

| Bump | When | Example |
|---|---|---|
| **Patch** (`v1.0.1`) | Bug fix, minor tweak | Fix captain scoring edge case |
| **Minor** (`v1.1.0`) | New command, new agent, new capability | Add `FPL_CLI_DATA_DIR` override |
| **Major** (`v2.0.0`) | Breaking CLI change: removed/renamed command or flag, changed output format | `ep_next` emits JSON null (v2.0.0) |

Publishing a GitHub release triggers `.github/workflows/release.yml`, which
builds the package, uploads it to PyPI ([fplkit](https://pypi.org/project/fplkit/))
via trusted publishing, and prepends the git-cliff section to
`CHANGELOG.md`. The full runbook lives in
[`.agents/skills/release/SKILL.md`](.agents/skills/release/SKILL.md);
agent sessions run it as the `release` skill, and can preview the next
version's notes any time with the read-only `release-notes` skill.

## Agent sessions

The operative copy of these conventions for agents is in
[CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) ("Commits & Changelog"),
which load into every session's context. Changing the conventions means
updating this file and both of those — see the sync rules in CLAUDE.md.
