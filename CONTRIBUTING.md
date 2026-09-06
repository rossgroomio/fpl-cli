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
