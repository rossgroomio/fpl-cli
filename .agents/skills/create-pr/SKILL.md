---
name: create-pr
description: >
  Use when the user asks to create a pull request or PR for the current
  branch — "create a PR", "open a PR", "raise a PR", "push and create a
  PR", "ship this branch". Writes the title as the changelog line
  (conventional commits) and the body in the fpl-cli PR description style:
  first-person prose, why-first, no boilerplate sections. Also the style
  reference whenever a session writes a PR title or body outside the skill.
model: sonnet
compatibility:
  claude-code: full (gh locally, GitHub MCP tools on the web)
  codex: full (gh)
  cursor: full (gh)
  copilot: full (gh)
---

# Create Pull Request

Opens a PR for the current branch against main: pushes the branch, writes
a conventional-commit title, and a prose description in the fpl-cli PR
description style. Solo repo — there are no reviewers, squads, or labels
to assign, and the PR is created ready (not draft) unless asked otherwise.

## Title: the changelog line

PRs squash-merge with the title as the commit subject on main, and
git-cliff publishes `feat:`/`fix:`/`refactor:`/`perf:` subjects verbatim in
CHANGELOG.md and the GitHub release notes. The `PR Title` CI check rejects
anything non-conventional. So:

- Format `type(scope)?!?: subject` — types as in `cliff.toml` (`feat`,
  `fix`, `refactor`, `perf` are changelog-visible; `chore`, `docs`, `ci`,
  `test`, `style` are skipped). `!` marks a breaking CLI change and drives
  a major bump.
- A changelog-visible title must read as the standalone user-facing change
  ("fix: pin PuLP below 4.0 so squad allocation keeps working"), never the
  implementation ("fix: address review feedback").
- Keep it ≤ 72 characters — GitHub appends ` (#N)` on squash.
- No issue references in the title; `Closes #N` lives in the body, so the
  changelog line stays clean.
- One purpose per PR, so one title can describe it. If no single
  conventional subject covers the diff, the branch wants splitting, not a
  vaguer title.

## Description: the fpl-cli PR description style

The body is **flowing first-person prose**, as if explaining the change to
a colleague. No `## Summary`/`## Changes`/`## Testing` headings, no
file-by-file changelog, no checklists.

### Tone

First person, conversational, proper sentence casing. Reads like an
explanation, not a filled-in template.

Good: "The changelog job pushes straight to main, which the new ruleset
would block — so it now authenticates as the repo admin, who is a bypass
actor."
Bad: "## Summary\nThis PR updates the changelog job's authentication."

### Content

- **Open with why, not what.** Start with the problem or context that
  motivated the change. The diff already shows what changed.
- **Explain the approach when it is non-obvious.** Natural phrasing ("the
  general strategy is"), not a formal section.
- **`Closes #N`** inline when the PR resolves an issue — GitHub closes it
  on merge. The branch name often carries the number (`claude/issue-46-…`).
- **Use italics for caveats, scope notes, and verification gaps.**
  Deliberate omissions, known limitations, and anything you could not
  verify — and why — go in `_italics_`. In this repo that is usually
  behaviour needing live FPL API data, LLM provider keys, or the scraper's
  FPL credentials, none of which a web session has.
- **Include stats when relevant.** Test counts, performance numbers,
  migration sizes — plainly stated, at the end.

### Structure and length

No fixed sections. Bullets only where you are genuinely listing items
(rules, scenarios, per-command effects) — never as the default shape.

Pick the budget before writing, and stay inside it:

| Change | Body |
|---|---|
| Dependency bump, config/data refresh, docs, cleanup, test-only | 1–2 sentences |
| Bug fix, small feature, new flag, refactor | 2–3 sentences |
| New agent, new data source, scoring change, or a strategy the diff cannot show | up to 3 short paragraphs |

Break the body up at every length: at 2–3 sentences, one sentence per
line; beyond that, paragraphs of two-three sentences with blank lines
between. Then one editing pass — delete every sentence that only tells the
reviewer something the diff or the title already tells them.

### What to leave out

- No "Test Plan"/"How to verify"/"Acceptance Criteria" sections
- No "Changes" lists that duplicate the diff
- No empty validation ("great improvement to the codebase")
- No restating the title or narrating commits
- No extra AI-attribution lines — remote Claude Code sessions append a
  "Generated with Claude Code" footer automatically; that platform footer
  stays, and nothing more gets added

## Workflow

### Step 1: Branch and push

```bash
BRANCH=$(git branch --show-current)   # never main
git push -u origin "$BRANCH"
```

Target is always `main`. If the branch's previous PR was already merged,
restart the branch from `origin/main` first and carry only unmerged
commits.

### Step 2: Gather the why

If the branch or session references an issue, read it (`gh issue view N`,
or the GitHub MCP `issue_read` tool) for motivation. `git log
origin/main..HEAD --oneline` shows scope — use it to understand intent,
never to write a commit list into the body.

### Step 3: Write title and body

Title per the rules above; body inside the length budget. Sanity-check the
title against the lint regex:
`^(feat|fix|refactor|perf|chore|docs|ci|test|style)(\([a-zA-Z0-9._/ -]+\))?!?: .+`

### Step 4: Create the PR

Local session:

```bash
gh pr create --title "<title>" --base main --body "$(cat <<'EOF'
<body prose>
EOF
)"
```

Web/remote session: the GitHub MCP `create_pull_request` tool (load via
ToolSearch) with the same title/body, `base: main`.

Pass `--draft` (or `draft: true`) only if the user asked to open it as a
draft to keep working on the branch.

### Step 5: Report

Print the PR URL. If the user asked for auto-merge, arm it (`gh pr merge
--auto --squash`, or the MCP `enable_pr_auto_merge` tool) — it merges once
the required `check` and `lint` checks pass.

## Stacked PRs

When this branch builds on another open PR, target that PR's branch
instead of main and end the body with `Stacked on #N.` Sanity-check with
`git log origin/<target>..HEAD --oneline` — only this PR's commits should
appear. When the base PR squash-merges and its branch auto-deletes, GitHub
retargets this PR to main and the diff inherits the base PR's commits:
rebase onto main before review continues.

## Common Mistakes

- Issue number in the title (belongs in the body as `Closes #N`)
- A changelog-visible title describing the implementation or the review
  process instead of the user-facing change
- Narrating the diff or padding the body to look thorough
- Describing only what changed and never why — even a two-sentence body
  carries the motivation
- Bundling unrelated changes so no single title can describe the PR
- Forgetting to push before creating the PR
- Adding attribution lines beyond the automatic platform footer

## Validation

- The branch is pushed and the PR targets `main` (or a stacked base per
  the rules above), created ready unless the user asked for a draft.
- The title matches the lint regex, is ≤ 72 characters, carries no issue
  ref, and — if changelog-visible — reads as the standalone user-facing
  change.
- The body is why-first first-person prose within its length budget: no
  boilerplate headings, no diff narration, no empty validation, and no
  sentence restating the title or the diff.
- `Closes #N` appears when the PR resolves an issue; caveats and
  verification gaps are in italics; any stats are plainly stated at the
  end.
- The PR URL was reported to the user.
