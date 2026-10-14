# .agents/

Agent skills for fpl-cli. Each subdirectory in `skills/` is a self-contained skill with a `SKILL.md` entry point.

## Discovery

Claude Code discovers skills via the `.claude/skills/` symlink, which points here. Other agent tools can read `skills/` directly.

For a complete inventory of CLI commands, analysis agents, and skills, see [TOOLS.md](TOOLS.md).

## Adapting Skills

Skills are showcase examples - they work out of the box but are designed to be customised:

- `<!-- ADAPT: ... -->` comments mark sections you'll want to change for your setup
- Output paths use `[YOUR_OUTPUT_DIR]` placeholders - replace with your preferred location
- Script invocations use a `[YOUR_PYTHON]` placeholder - replace with the interpreter fpl-cli is installed on. Helper scripts import the `fpl_cli` package, which a standalone `fpl` on `PATH` (uv tool, pipx) does not provide; get it wrong and they exit 1 with a JSON error envelope naming the cause
- Supplementary data sources (newsletters, external reports) are noted but not required
- All CLI data gathering uses `--format json` for structured output

## Model and effort

The four product skills (gw-prep, squad-builder, update-gw-prep, preview-ingest) pin a model and an effort level in their `SKILL.md` frontmatter, and every `Agent tool parameters` block pins its own pair, so a run does not depend on whatever the session happened to be set to (issue #393). The skills carry the reasons and point here; this table is the one place the levels are written down outside the blocks themselves.

| Where | Model | Effort | Why |
|---|---|---|---|
| gw-prep orchestrator | opus | high | Gathers the data and assembles the report; the frontmatter also sets the level the Phase C prompts are built at |
| gw-prep C1 classic analysis | opus | high | Weighs transfers, hits, chips and captaincy against each other: the most trade-off-heavy step in the repo |
| gw-prep C2 draft analysis | sonnet | medium | No budget, chips or captains; the call is waivers and the lineup |
| squad-builder orchestrator and Phase D assembly | opus | high | Assembly enforces every cross-position constraint and settles the trade-offs the position agents only surface |
| squad-builder Phase C position research | opus | medium | Gathering and ranking within one position; the trade-offs are made in Phase D |
| update-gw-prep orchestrator and sub-agent | opus | high | Revises the classic and draft recommendations against new information, so it inherits the classic analysis's trade-offs |
| preview-ingest orchestrator and per-team agents | opus | medium | Extraction against a fixed schema: reading, not weighing |

These levels are a judgement, not a measurement. `max` is untested here and probably not worth its time and cost; a gameweek of gw-prep run twice, once at these levels and once with classic at `max`, is the experiment that would settle it. Change a level in the block and in its row together: `tests/test_skill_effort_levels.py` reads each skill's frontmatter and every `Agent tool parameters` block, and holds each row of this table to the block its Where cell names (`orchestrator` is the frontmatter; the other labels are the sub-agent blocks in document order), so a swapped, missing or misspelt row fails there rather than being dropped.

How far each key reaches, quoted from the Claude Code docs as read on 14 October 2026:

- `model` in the frontmatter ([skills, Frontmatter reference](https://code.claude.com/docs/en/skills#frontmatter-reference)): "The override applies for the rest of the current turn and isn't saved to settings. The session model resumes when you send your next prompt." So a follow-up question about the output ("why sell X?") is answered on the session model.
- `effort` in the frontmatter (same table): "Effort level when this skill is active. Overrides the session effort level." The docs do not say whether it reverts when the skill finishes.
- `effort` in an `Agent tool parameters` block is the per-invocation parameter ([sub-agents, Choose an effort level](https://code.claude.com/docs/en/sub-agents#choose-an-effort-level)): "When you ask Claude to run a non-fork subagent at a specific effort level, it can also pass an `effort` parameter for that invocation. The parameter overrides the `effort` field and stays in effect when the subagent is resumed. The `CLAUDE_CODE_EFFORT_LEVEL` environment variable takes precedence over both. The per-invocation parameter requires Claude Code v2.1.292 or later."
- claude.ai skill uploads and the Skills API ([skills, Using skill frontmatter outside Claude Code](https://code.claude.com/docs/en/skills#using-skill-frontmatter-outside-claude-code)) accept only the Agent Skills spec keys, `name`, `description`, `license`, `compatibility`, `metadata` and `allowed-tools`, and fail on anything else. `model` already fell outside that set, so `effort` adds no new break; strip both before uploading.
- Codex, Cursor and Copilot (named in each skill's `compatibility` block) read neither key, so they run at whatever their session is set to.

## Private Skills

To add a private skill that shouldn't be committed, create it under `skills/` and add the path to `.gitignore`:

```
.agents/skills/my-private-skill/
```
