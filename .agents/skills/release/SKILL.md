---
name: release
description: >
  Cut a new release end-to-end: verify main is releasable, draft notes via
  the release-notes skill, get explicit approval, and publish the GitHub
  release whose tag drives the automated PyPI publish and changelog update.
  Use when the user says "cut a release", "ship it", "new release",
  "release to PyPI", "publish a new version", or "time to release". Only
  for actually publishing — to preview notes, use release-notes instead.
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

## Step 1 — Preflight

If any check fails, report the failures and stop — don't release a broken
or stale build.

- On the tip of main with a clean tree (`git status --porcelain` empty;
  `git fetch origin main --tags` first).
- Commits exist since the last tag — otherwise there's nothing to release.
- CI is green on the exact head SHA of main (GitHub Actions `CI` workflow).
- Local checks pass, mirroring CI (run in parallel; check each exit status
  individually — a `| tail` pipe hides failures):

```bash
ruff check fpl_cli/
pyright fpl_cli/
pytest
python -m build          # or: hatch build — confirms sdist+wheel build
```

## Step 2 — Draft notes and version

Run the `release-notes` skill workflow
(`.agents/skills/release-notes/SKILL.md`): it suggests the semver bump,
generates the git-cliff preview, curates out changelog noise, and drafts
the notes in house style.

## Step 3 — Approval gate

Present the draft and suggested version. Ask the user to confirm or adjust
both. **Do not proceed until the user explicitly approves.** Publishing is
irreversible: the PyPI version number is consumed permanently even if the
release is later deleted.

## Step 4 — Publish

**Local session (gh available)** — use a heredoc to preserve formatting:

```bash
gh release create vX.Y.Z --title "vX.Y.Z" --target main --notes "$(cat <<'EOF'
[approved release notes here]
EOF
)"
```

This creates the tag at the tip of main and publishes in one step, which
fires the release workflow.

**Web/remote session (no gh; the GitHub MCP server has no create-release
tool):** hand the user the command above with the notes filled in, or the
UI steps — GitHub → Releases → Draft a new release → "Choose a tag" → type
`vX.Y.Z` (create on publish) → target `main` → paste notes → Publish.

## Step 5 — Post-release verification

1. The release exists (`gh release view vX.Y.Z`) and the `Release`
   workflow run for it succeeds — all three jobs (build, publish,
   changelog): `gh run list --workflow=release.yml --limit=1`.
2. https://pypi.org/project/fplkit/ shows the new version —
   `pip index versions fplkit`, or `pip install fplkit==X.Y.Z` on a fresh
   venv for a full check.
3. The `docs: update CHANGELOG.md [skip ci]` commit landed on main.

Tell the user the release is live, with links to the release page and the
Actions run.

## Known failure modes

- **Version guard fails** (`0.0.0`/`.dev` in dist names): tag missing the
  `v` prefix, or the workflow checked out a commit the tag isn't on.
- **Changelog push rejected** (non-fast-forward): main advanced between
  publish and the changelog commit — documented race in `release.yml`;
  re-run the changelog job manually.
- **Changelog push rejected by the main ruleset**: the job pushes with the
  `RELEASE_PUSH_TOKEN` secret (the repo admin's fine-grained PAT, a ruleset
  bypass actor) because the github-actions app can't be on the bypass list.
  A missing or expired secret makes the push fall back to `GITHUB_TOKEN`,
  which the ruleset blocks — recreate the PAT (Contents: read/write on this
  repo), update the secret, re-run the job.
- **PyPI publish fails with OIDC errors**: check the `pypi` environment on
  the repo and the trusted-publisher config on the fplkit PyPI project.
- **Version source drift**: never reintroduce a hardcoded `version =
  "X.Y.Z"` in pyproject.toml — `dynamic = ["version"]` + hatch-vcs is the
  single source of truth. And `fallback-version` belongs under
  `[tool.hatch.version]`, not `[tool.hatch.build.hooks.vcs]` — misplaced,
  it is silently ignored.
