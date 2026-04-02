# .agents/

Agent skills for fpl-cli. Each subdirectory in `skills/` is a self-contained skill with a `SKILL.md` entry point.

## Discovery

Claude Code discovers skills via the `.claude/skills/` symlink, which points here. Other agent tools can read `skills/` directly.

For a complete inventory of CLI commands, analysis agents, and skills, see [TOOLS.md](TOOLS.md).

## Adapting Skills

Skills are showcase examples - they work out of the box but are designed to be customised:

- `<!-- ADAPT: ... -->` comments mark sections you'll want to change for your setup
- Output paths use `[YOUR_OUTPUT_DIR]` placeholders - replace with your preferred location
- Supplementary data sources (newsletters, external reports) are noted but not required
- All CLI data gathering uses `--format json` for structured output

## Private Skills

To add a private skill that shouldn't be committed, create it under `skills/` and add the path to `.gitignore`:

```
.agents/skills/my-private-skill/
```
