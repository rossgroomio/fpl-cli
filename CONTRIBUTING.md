# Contributing

fpl-cli is a solo project where most PRs are authored by Claude Code
sessions. These conventions exist less for etiquette and more to keep the
automated release pipeline meaningful: commit subjects that reach main are
published verbatim in `CHANGELOG.md` and the GitHub release notes.

## Dev setup & checks

See the [Development section of the README](README.md#development) for
setup. Every PR must pass CI, which runs:

```console
$ ruff check fpl_cli/    # Lint
$ pyright fpl_cli/       # Type check
$ pytest                 # Tests
$ hatch build            # Package build
```

## Commit subjects & PR titles

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

## Releases

Versioning is tag-driven (`hatch-vcs`) — no file carries a version number.
Publishing a GitHub release triggers `.github/workflows/release.yml`, which
builds the package, uploads it to PyPI ([fplkit](https://pypi.org/project/fplkit/))
via trusted publishing, and prepends the git-cliff section to
`CHANGELOG.md`. The full runbook lives in
[`.agents/skills/release/SKILL.md`](.agents/skills/release/SKILL.md);
agent sessions run it as the `release` skill.

## Agent sessions

The operative copy of these conventions for agents is in
[CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) ("Commits & Changelog"),
which load into every session's context. Changing the conventions means
updating this file and both of those — see the sync rules in CLAUDE.md.
