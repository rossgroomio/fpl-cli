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

The four product skills (gw-prep, squad-builder, update-gw-prep, preview-ingest) pin a model and an effort level in their `SKILL.md` frontmatter, and every sub-agent parameter block pins its own pair, so a run does not depend on whatever the session happened to be set to (issue #393).

| Where | Model | Effort | Why |
|---|---|---|---|
| gw-prep orchestrator | opus | high | Gathers the data and assembles the report; the frontmatter also sets the level the Phase C prompts are built at |
| gw-prep C1 classic analysis | opus | high | Weighs transfers, hits, chips and captaincy against each other: the most trade-off-heavy step in the repo |
| gw-prep C2 draft analysis | sonnet | medium | No budget, chips or captains; the call is waivers and the lineup |
| squad-builder orchestrator and Phase D assembly | opus | high | Assembly enforces every cross-position constraint and settles the trade-offs the position agents only surface |
| squad-builder Phase C position research | opus | medium | Gathering and ranking within one position; the trade-offs are made in Phase D |
| update-gw-prep orchestrator and sub-agent | opus | high | Revises the classic and draft recommendations against new information, so it inherits the classic analysis's trade-offs |
| preview-ingest orchestrator and per-team agents | opus | medium | Extraction against a fixed schema: reading, not weighing |

These levels are a judgement, not a measurement. `max` is untested here and probably not worth its time and cost; a gameweek of gw-prep run twice, once at these levels and once with classic at `max`, is the experiment that would settle it. Change a level in the skill and in this table together: `tests/test_skill_effort_levels.py` holds each skill's frontmatter and every sub-agent block to a model-and-effort pair, and this table to the pairs the skills use.

How far each key reaches, per the Claude Code [skills](https://code.claude.com/docs/en/skills) and [sub-agents](https://code.claude.com/docs/en/sub-agents) docs:

- `model` in the frontmatter applies for the rest of the turn that runs the skill; the session model resumes on the next prompt. A follow-up question about the output ("why sell X?") is answered on the session model.
- `effort` in the frontmatter overrides the session effort while the skill is active. The docs do not say whether it reverts when the skill finishes.
- The `effort` line in an Agent tool block is the per-invocation parameter. It overrides the sub-agent's own definition, needs Claude Code v2.1.292 or later, and is documented for non-fork sub-agents only. `CLAUDE_CODE_EFFORT_LEVEL`, if set, beats both.
- claude.ai skill uploads and the Skills API accept only the Agent Skills spec keys (`name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools`) and fail on anything else. `model` already fell outside that set, so `effort` adds no new break; strip both before uploading.
- Codex, Cursor and Copilot ignore both keys and run at whatever their session is set to.

## Private Skills

To add a private skill that shouldn't be committed, create it under `skills/` and add the path to `.gitignore`:

```
.agents/skills/my-private-skill/
```
