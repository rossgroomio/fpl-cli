---
name: release-notes
description: >
  Draft release notes for the next GitHub Release. Use when the user says
  "release notes", "draft a release", "what's changed since last release",
  or wants to preview the next version before cutting it. Read-only: drafts
  and suggests, never tags, pushes, or publishes — the release skill
  orchestrates this one when actually shipping.
model: sonnet
compatibility:
  claude-code: full
  codex: full
  cursor: full
  copilot: full
---

# Release Notes

Draft release notes for a GitHub Release, ready to paste into the release
form. **This skill is read-only: do not create files in the repo, do not
tag, push, or publish anything.**

## Step 1: Gather context

```bash
git fetch origin main --tags
git describe --tags --abbrev=0 origin/main       # last release tag
git log $(git describe --tags --abbrev=0 origin/main)..origin/main --oneline --no-decorate
```

If there are no commits since the last tag, tell the user there's nothing
to release and stop.

Check git-cliff is available (`git-cliff --version`); install it if missing
(`pip install git-cliff`).

## Step 2: Suggest the version

Semver over conventional commits since the last tag:

- any `feat!:` / `fix!:` / `BREAKING CHANGE` footer → **major**. In this
  project that means a breaking CLI change: removed/renamed command or
  flag, or changed output format (precedent: v2.0.0 came from a `feat!:`
  that changed JSON output)
- else any `feat:` → **minor** (new command, new capability)
- else → **patch** (fixes and tweaks only)

State the suggestion with reasoning, e.g. "Suggested version: **v2.1.0**
(contains new features, no breaking markers)". If the bump is debatable
(e.g. a `fix:` that changes accepted behaviour), say so and let the user
decide.

## Step 3: Generate the grouped changelog

```bash
git-cliff --unreleased --tag vX.Y.Z --strip header
```

This uses the project's `cliff.toml`: feat/fix/refactor/perf are grouped,
chore/docs/ci/test/style and merge commits are skipped, anything
unconventional lands in Other.

**History caveat:** under squash-merging (through v2.0.0, and again from
August 2026 onward) each PR contributes exactly one line — its title,
format-enforced by the `PR Title` CI check. The window in between merged
cloud PRs as merge commits plus every branch commit, so entries from that
range come from individual branch commits: eyeball them for PR-internal
churn that slipped past the `cliff.toml` skips (review follow-ups
mislabelled `fix:`, test-only fixes), drop those lines from the notes, and
add a skip parser so the automated CHANGELOG.md job drops them too.

## Step 4: Draft the notes

House style, used by every release since v1.1.0:

```markdown
## What's new

[One to three sentences summarising the headline change. Written for
users, not developers — what they can now do, not what was refactored.
If there's a single standout feature, lead with it; if it's a mixed bag
of smaller improvements, say so.]

[git-cliff grouped output — only groups with content; lightly clean
scope prefixes like `fix(init):` → `init:`]

**Full changelog**: https://github.com/rossgroomio/fpl-cli/compare/{last_tag}...{new_tag}
```

Keep the git-cliff wording — don't rewrite commit subjects beyond trimming
prefixes. The headline is the editorial bit; write it direct and hype-free.

## Step 5: Present to the user

Output the draft in a fenced code block so it's easy to copy. Below it,
state the suggested version and tag name, and note that publishing a
GitHub Release with that tag triggers the PyPI publish and changelog
automation (the `release` skill handles that end-to-end).
