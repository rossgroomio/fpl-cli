---
name: release
description: >
  Cut a new release: verify main is releasable, pick the next semver from
  conventional commits since the last tag, draft release notes in house
  style, and publish a GitHub release whose tag drives the automated PyPI
  publish and changelog update. Use when the user says "cut a release",
  "new release", "prepare a release", "release to PyPI", "tag a version",
  or "publish a release".
compatibility:
  claude-code: full locally (gh publishes); web sessions prepare everything, user runs the publish step
  codex: full (same shell commands)
  cursor: full (same shell commands)
  copilot: full (same shell commands)
---

# Release

Cut a new fplkit release. The GitHub release publish is the **only** manual
action — everything downstream is automated.

## How releases work in this repo

- **Version = git tag.** `hatch-vcs` derives the package version from the
  latest `v`-prefixed tag (`pyproject.toml` → `[tool.hatch.version] source =
  "vcs"`). No file carries a version number; there is nothing to bump or
  commit before tagging.
- **Publishing a GitHub release triggers `.github/workflows/release.yml`**,
  which runs three jobs in sequence:
  1. **build** — `hatch build`, then a version guard that fails on `0.0.0`
     or `.dev` in the artifact names (catches a malformed tag or missing
     history).
  2. **publish** — uploads to PyPI via trusted publishing (OIDC, GitHub
     environment `pypi`). Package name on PyPI is **fplkit**.
  3. **changelog** — `git-cliff --latest --prepend CHANGELOG.md`, committed
     straight to main as `docs: update CHANGELOG.md [skip ci]`.
- Tags are lightweight and created by GitHub at publish time (`gh release
  create` with a new tag name). Do not pre-push an annotated tag.

## Phase 1 — Preflight

```bash
git fetch origin main --tags
git log -1 origin/main --oneline
git describe --tags --abbrev=0 origin/main      # last release tag
git log $(git describe --tags --abbrev=0 origin/main)..origin/main --oneline
```

- No commits since the last tag → nothing to release; stop.
- Confirm CI is green on the exact tip of main (GitHub Actions `CI` workflow
  for that head SHA). Do not release on a red or pending head.
- Work from the tip of main with a clean tree.

## Phase 2 — Verify locally

Mirror the CI check job (install `-e ".[dev]"` first if needed):

```bash
ruff check fpl_cli/
pyright fpl_cli/
pytest
python -m build          # or: hatch build — confirms sdist+wheel build
```

Check each command's exit status individually — a `| tail` pipe hides
failures. All four must pass before proceeding.

## Phase 3 — Pick the version

Read the commit subjects since the last tag and apply semver over
conventional commits:

- any `feat!:` / `fix!:` / `BREAKING CHANGE` footer → **major**
  (precedent: v2.0.0 came from a single `feat!:`)
- else any `feat:` → **minor**
- else → **patch**

If the bump is debatable (e.g. a `fix:` that changes accepted behaviour),
propose one and let the user decide.

## Phase 4 — Draft the release notes

Preview the grouped commits exactly as the changelog job will render them
(`cliff.toml` groups feat/fix/refactor/perf, skips chore/ci/docs/test,
catches the rest as Other):

```bash
git-cliff --unreleased --tag vX.Y.Z --strip header
```

Write the release body in the house style used by every release since
v1.1.0:

```markdown
## What's new

<1–3 sentences of user-facing prose summarising the headline changes —
what a user gains, not a commit list.>

### Features

- <commit subjects, lightly cleaned: drop scope prefixes like `fix(init):` → `init:`>

### Bug Fixes

- ...

**Full changelog**: https://github.com/rossgroomio/fpl-cli/compare/vPREV...vX.Y.Z
```

Only include groups that have content. Keep the git-cliff wording; don't
rewrite commit subjects beyond trimming prefixes. Show the draft to the
user before publishing.

## Phase 5 — Publish (requires explicit user go-ahead)

Publishing is irreversible: the PyPI version number is consumed permanently
even if the release is later deleted. Never publish without the user
confirming the version and notes.

**Local session (gh available):**

```bash
gh release create vX.Y.Z --target main --title vX.Y.Z --notes-file notes.md
```

This creates the tag at the tip of main and publishes in one step, which
fires the release workflow.

**Web/remote session (no gh; the GitHub MCP server has no create-release
tool):** prepare `notes.md` content and hand the user either the `gh
release create` command above or the UI steps — GitHub → Releases → Draft a
new release → "Choose a tag" → type `vX.Y.Z` (create on publish) → target
`main` → paste notes → Publish.

## Phase 6 — Post-release verification

1. The `Release` workflow run for the tag succeeds — all three jobs
   (build, publish, changelog).
2. https://pypi.org/project/fplkit/ shows the new version
   (`pip index versions fplkit` also works).
3. The `docs: update CHANGELOG.md [skip ci]` commit landed on main.

## Known failure modes

- **Version guard fails** (`0.0.0`/`.dev` in dist names): tag missing the
  `v` prefix, or the workflow checked out a commit the tag isn't on.
- **Changelog push rejected** (non-fast-forward): main advanced between
  publish and the changelog commit — documented race in `release.yml`;
  re-run the changelog job manually.
- **PyPI publish fails with OIDC errors**: check the `pypi` environment on
  the repo and the trusted-publisher config on the fplkit PyPI project.
