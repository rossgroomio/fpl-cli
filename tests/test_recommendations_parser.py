"""Tests for the recommendations markdown parser."""

import re
from pathlib import Path
from textwrap import dedent

import pytest

from fpl_cli.parsers.recommendations import parse_recommendations

REPO_ROOT = Path(__file__).resolve().parents[1]
GW_PREP_TEMPLATE = REPO_ROOT / ".agents" / "skills" / "gw-prep" / "references" / "output-template.md"
SQUAD_BUILDER_TEMPLATE = REPO_ROOT / ".agents" / "skills" / "squad-builder" / "references" / "output-template.md"


@pytest.fixture
def tmp_recs(tmp_path):
    """Write content to a temp recommendations file and return its path."""
    def _write(content: str) -> Path:
        p = tmp_path / "gw30-recommendations.md"
        p.write_text(dedent(content), encoding="utf-8")
        return p
    return _write


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:

    def test_extracts_gameweek(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 30
            generated: 2026-03-13
            ---
            ## Classic
        """)
        result = parse_recommendations(path)
        assert result["gameweek"] == 30

    def test_missing_frontmatter_returns_none_gameweek(self, tmp_recs):
        path = tmp_recs("## Classic\nSome content")
        result = parse_recommendations(path)
        assert result["gameweek"] is None


# ---------------------------------------------------------------------------
# Captain
# ---------------------------------------------------------------------------

class TestParseCaptain:

    def test_standard_captain_vice(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 30
            ---
            ## Classic
            **Captain:** João Pedro | **Vice:** Rice
        """)
        result = parse_recommendations(path)
        assert result["classic"]["captain"] == "João Pedro"
        assert result["classic"]["vice_captain"] == "Rice"

    def test_conditional_captain(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 30
            ---
            ## Classic
            **Captain:** Haaland (if confirmed fit) | **Vice:** Salah
        """)
        result = parse_recommendations(path)
        assert result["classic"]["captain"] == "Haaland"
        assert result["classic"]["vice_captain"] == "Salah"

    def test_no_captain_line(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 30
            ---
            ## Classic
            No captain info here.
        """)
        result = parse_recommendations(path)
        assert result["classic"]["captain"] is None
        assert result["classic"]["vice_captain"] is None


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

class TestParseTransfers:

    def test_single_transfer(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 29
            ---
            ## Classic
            ### Transfers
            ##### Recommended Transfer (1): Iwobi <- Miley
            Some analysis text.
            ## Draft
        """)
        result = parse_recommendations(path)
        assert len(result["classic"]["transfers"]) == 1
        assert result["classic"]["transfers"][0] == {"in": "Iwobi", "in_team": None, "out": "Miley", "out_team": None}
        assert result["classic"]["roll_transfer"] is False

    def test_numbered_transfer_format(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 25
            ---
            ## Classic
            ### Transfers
            ##### Transfer 1: Rice <- Saka
            Analysis here.
            ## Draft
        """)
        result = parse_recommendations(path)
        assert len(result["classic"]["transfers"]) == 1
        assert result["classic"]["transfers"][0] == {"in": "Rice", "in_team": None, "out": "Saka", "out_team": None}

    def test_roll_transfer_heading(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 30
            ---
            ## Classic
            ### Transfers
            ##### Recommended Transfer: Roll (bank for 2 FTs / WC)
            Roll analysis.
            ## Draft
        """)
        result = parse_recommendations(path)
        assert result["classic"]["roll_transfer"] is True
        assert len(result["classic"]["transfers"]) == 0

    def test_no_transfers_text(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 30
            ---
            ## Classic
            ### Transfers
            **No transfers this gameweek — roll FT.**
            ## Draft
        """)
        result = parse_recommendations(path)
        assert result["classic"]["roll_transfer"] is True

    def test_transfer_keeps_the_leading_initial(self, tmp_recs):
        """'L.Miley' and 'Miley' are both on a bootstrap, at the same club, so
        stripping the initial merges two players (#354)."""
        path = tmp_recs("""\
            ---
            gameweek: 29
            ---
            ## Classic
            ##### Recommended Transfer (1): Iwobi <- L.Miley
            ## Draft
        """)
        result = parse_recommendations(path)
        assert result["classic"]["transfers"][0]["out"] == "L.Miley"

    def test_transfer_with_unicode_arrow(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 29
            ---
            ## Classic
            ##### Recommended Transfer (1): Iwobi ← Miley
            ## Draft
        """)
        result = parse_recommendations(path)
        assert result["classic"]["transfers"][0]["in"] == "Iwobi"
        assert result["classic"]["transfers"][0]["out"] == "Miley"


# ---------------------------------------------------------------------------
# Waivers
# ---------------------------------------------------------------------------

class TestParseWaivers:

    def test_multiple_waivers(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 30
            ---
            ## Classic
            **Captain:** Salah | **Vice:** Rice
            ## Draft
            ### Waivers
            ##### Priority 1: Nyoni (LIV, MID) ← Wirtz
            Analysis.
            ##### Priority 2: Branthwaite (EVE, DEF) ← Saliba
            Analysis.
            ##### Priority 3: Hermansen (WHU, GK) ← Pope
            Analysis.
        """)
        result = parse_recommendations(path)
        waivers = result["draft"]["waivers"]
        assert len(waivers) == 3
        assert waivers[0] == {"priority": 1, "in": "Nyoni", "in_team": "LIV", "out": "Wirtz", "out_team": None}
        assert waivers[1] == {"priority": 2, "in": "Branthwaite", "in_team": "EVE", "out": "Saliba", "out_team": None}
        assert waivers[2] == {"priority": 3, "in": "Hermansen", "in_team": "WHU", "out": "Pope", "out_team": None}

    def test_no_waivers_recommended(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 30
            ---
            ## Classic
            ## Draft
            ### Waivers
            No waivers recommended.
        """)
        result = parse_recommendations(path)
        assert result["draft"]["waivers"] == []

    def test_waiver_with_arrow_variant(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 30
            ---
            ## Draft
            ##### Priority 1: Nyoni <- Wirtz
        """)
        result = parse_recommendations(path)
        assert len(result["draft"]["waivers"]) == 1
        assert result["draft"]["waivers"][0]["in"] == "Nyoni"


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

class TestParseMissingFile:

    def test_returns_none_for_nonexistent(self, tmp_path):
        result = parse_recommendations(tmp_path / "nonexistent.md")
        assert result is None


# ---------------------------------------------------------------------------
# Full file (end-to-end with GW30 content excerpt)
# ---------------------------------------------------------------------------

class TestParseFullFile:

    def test_end_to_end(self, tmp_recs):
        content = """\
            ---
            gameweek: 30
            generated: 2026-03-13
            deadline: 2026-03-14T13:30:00Z
            sources: FPL Agents
            ---

            ## Classic
            ### Chip Strategy
            Some chip strategy text.

            ### Transfers
            #### Transfer Analysis
            **Free Transfers:** 1 | **Bank:** £1.1m

            ##### Recommended Transfer: Roll (bank for 2 FTs / WC)
            With 4 starters blanking GW31 rolling the FT is the correct play.

            **No transfers this gameweek — roll FT.**

            ### Selection
            **Captain:** João Pedro | **Vice:** Rice

            ---

            ## Draft
            ### Waivers
            #### Waiver Proposals
            ##### Priority 1: Nyoni (LIV, MID) ← Wirtz
            Scout text.
            ##### Priority 2: Branthwaite (EVE, DEF) ← Saliba
            More text.
            ##### Priority 3: Hermansen (WHU, GK) ← Pope
            Even more text.
        """
        path = tmp_recs(content)
        result = parse_recommendations(path)

        assert result["gameweek"] == 30
        assert result["classic"]["captain"] == "João Pedro"
        assert result["classic"]["vice_captain"] == "Rice"
        assert result["classic"]["roll_transfer"] is True
        assert result["classic"]["transfers"] == []
        assert len(result["draft"]["waivers"]) == 3
        assert result["draft"]["waivers"][0]["in"] == "Nyoni"
        assert result["draft"]["waivers"][2]["out"] == "Pope"


# ---------------------------------------------------------------------------
# The templates the skills write from (#354)
# ---------------------------------------------------------------------------

def _fill_blank_cells(markdown: str) -> str:
    """Give every empty table cell a value naming its column and row.

    Row numbers run across the whole document, so the out and in of one
    transfer row share a number ("Out4", "In4") and a parser that read the
    columns the wrong way round, or paired cells from different rows, cannot
    pass. The template's own "..." elision rows are dropped, not filled.
    """
    out: list[str] = []
    headers: list[str] | None = None
    row_no = 0
    for line in markdown.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|"):
            headers = None
            out.append(line)
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if headers is None:
            headers = cells
            out.append(line)
            continue
        if set(stripped) <= set("|-: "):
            out.append(line)
            continue
        if cells[0] in {"...", "\u2026"}:
            continue
        row_no += 1
        filled = [
            c or f"{(headers[j] if j < len(headers) else 'Col').replace(' ', '')}{row_no}"
            for j, c in enumerate(cells)
        ]
        out.append("| " + " | ".join(filled) + " |")
    return "\n".join(out)


def _fenced_markdown_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("```markdown\n") + len("```markdown\n")
    return text[start:text.index("\n```", start)]


class TestParseTheSkillTemplates:
    """Parse what the skills are told to write, so a template change that
    the parser does not follow fails here instead of grading every move
    unadvised for a season."""

    def test_transfer_week_template(self, tmp_path):
        block = _fenced_markdown_block(GW_PREP_TEMPLATE)
        block = re.sub(r"^mode: .*$", "mode: transfer", block, count=1, flags=re.MULTILINE)
        block = block.replace("{season}", "2026-27").replace("{N}", "5")
        block = block.replace("{player}", "Haaland (MCI)")
        path = tmp_path / "gw5-recommendations.md"
        path.write_text(_fill_blank_cells(block), encoding="utf-8")

        result = parse_recommendations(path)

        assert result["gameweek"] == 5
        assert result["classic"]["mode"] == "transfer"
        assert result["classic"]["captain"] == "Haaland"
        assert result["classic"]["captain_team"] == "MCI"
        transfers = result["classic"]["transfers"]
        assert len(transfers) == 3
        for t in transfers:
            row = t["out"].removeprefix("Out")
            assert t["out"] == f"Out{row}" and t["in"] == f"In{row}"
        waivers = result["draft"]["waivers"]
        assert [w["priority"] for w in waivers] == [1, 2, 3, 4, 5]
        for w in waivers:
            row = w["out"].removeprefix("Drop")
            assert w["out"] == f"Drop{row}" and w["in"] == f"Claim{row}"
        assert result["classic"]["squad"] == []

    def test_wildcard_week_embeds_the_squad_builder_block(self, tmp_path):
        """gw-prep embeds squad-builder's Classic Squad a heading level down."""
        text = SQUAD_BUILDER_TEMPLATE.read_text(encoding="utf-8")
        block = text[text.index("## Classic Squad"):text.index("## Draft Rankings")]
        block = re.sub(r"^(#+) ", r"#\1 ", block, flags=re.MULTILINE)
        block = block.replace("{Wildcard|Free Hit|Season Start Classic}", "Wildcard")
        block = block.replace("{name}", "Haaland", 1).replace("{name}", "Salah", 1)
        path = tmp_path / "gw6-recommendations.md"
        path.write_text(
            "---\nseason: 2026-27\ngameweek: 6\nmode: wildcard\n---\n\n"
            "## Classic League\n\n" + _fill_blank_cells(block) + "\n## Draft League\n",
            encoding="utf-8",
        )

        result = parse_recommendations(path)

        assert result["classic"]["mode"] == "wildcard"
        assert result["classic"]["captain"] == "Haaland"
        assert result["classic"]["vice_captain"] == "Salah"
        squad = result["classic"]["squad"]
        # The template shows three Starting XI rows past its elision and four bench rows.
        assert len(squad) == 7
        assert all(p["name"].startswith("Player") for p in squad)
        assert result["classic"]["transfers"] == []
        assert result["classic"]["roll_transfer"] is False


# ---------------------------------------------------------------------------
# Table format details
# ---------------------------------------------------------------------------

class TestParseTables:

    def test_transfer_table_reads_out_then_in_and_keeps_clubs(self, tmp_recs):
        """The table is Out-then-In; the heading format was In-then-Out."""
        path = tmp_recs("""\
            ---
            gameweek: 5
            mode: transfer
            ---
            ## Classic League
            ### Transfer Recommendations
            | Priority | Out | In | Outlook | This GW | Net Cost | Rationale |
            |----------|-----|----|---------|---------|----------|-----------|
            | 1 | Tzolis (ARS) | Gibbs-White (NFO) | +3 | +1 | 0 | Fixtures \\| form |
            | 2 | **B. Fernandes (MUN)** | Palmer (CHE) | +2 | +1 | -4 | |
            | 3 | - | - | | | | |
            ## Draft League
        """)
        result = parse_recommendations(path)
        assert result["classic"]["transfers"] == [
            {"in": "Gibbs-White", "in_team": "NFO", "out": "Tzolis", "out_team": "ARS"},
            {"in": "Palmer", "in_team": "CHE", "out": "B. Fernandes", "out_team": "MUN"},
        ]
        assert result["classic"]["roll_transfer"] is False

    def test_waiver_table_skips_position_codes_and_override_notes(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 5
            ---
            ## Draft League
            ### Waiver Recommendations
            | Priority | Drop | Claim | Position | Outlook | This GW | Fixture Run | Rationale |
            |----------|------|-------|----------|---------|---------|-------------|-----------|
            | 1 | Wirtz (LIV) | Nyoni (MID, LIV) | MID | | | | \u26a1 Override: Wirtz ruled out |
            | 2 | Saliba | Branthwaite \u26a1 Override: cover | DEF | | | | |
        """)
        waivers = parse_recommendations(path)["draft"]["waivers"]
        assert waivers == [
            {"priority": 1, "in": "Nyoni", "in_team": "LIV", "out": "Wirtz", "out_team": "LIV"},
            {"priority": 2, "in": "Branthwaite", "in_team": None, "out": "Saliba", "out_team": None},
        ]

    def test_free_agent_table_written_after_the_waiver_run(self, tmp_recs):
        """Once waivers have processed the draft section recommends pickups
        under `# | Drop | Pick up` instead."""
        path = tmp_recs("""\
            ---
            gameweek: 6
            ---
            ## Draft League
            ### Free Agent Pickups
            | # | Drop | Pick up | Position | Rationale |
            |---|------|---------|----------|-----------|
            | 1 | Yalcouy\u00e9 (BHA) | Manzambi (BRE) | MID | |
        """)
        waivers = parse_recommendations(path)["draft"]["waivers"]
        assert waivers == [
            {"priority": 1, "in": "Manzambi", "in_team": "BRE", "out": "Yalcouy\u00e9", "out_team": "BHA"},
        ]

    def test_recommended_captain_line_with_hyphenated_name(self, tmp_recs):
        """The rationale separator is spaced, so a hyphenated surname survives."""
        path = tmp_recs("""\
            ---
            gameweek: 5
            ---
            ## Classic League
            **Recommended captain:** Gibbs-White (NFO) \u2014 home to Burnley
        """)
        result = parse_recommendations(path)
        assert result["classic"]["captain"] == "Gibbs-White"
        assert result["classic"]["captain_team"] == "NFO"
        assert result["classic"]["vice_captain"] is None

    def test_roll_said_in_the_transfer_section(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 5
            mode: transfer
            ---
            ## Classic League
            ### Transfer Recommendations
            | Priority | Out | In | Outlook | This GW | Net Cost | Rationale |
            |----------|-----|----|---------|---------|----------|-----------|
            | 1 | - | - | | | | Roll: bank the FT for a double move |
            ## Draft League
        """)
        result = parse_recommendations(path)
        assert result["classic"]["transfers"] == []
        assert result["classic"]["roll_transfer"] is True

    def test_rolling_a_chip_is_not_rolling_the_transfer(self, tmp_recs):
        """Chip Timing talks about rolling a chip to a later week."""
        path = tmp_recs("""\
            ---
            gameweek: 5
            mode: transfer
            ---
            ## Classic League
            ### Chip Timing
            Keep rolling the wildcard to GW8.
            ### Transfer Recommendations
            Nothing clears the hit threshold.
            ## Draft League
        """)
        assert parse_recommendations(path)["classic"]["roll_transfer"] is False

    def test_transfer_week_bench_order_is_not_a_squad(self, tmp_recs):
        """A file with no mode is read for a squad only when it embeds the
        Classic Squad block, and a bench on its own never counts."""
        path = tmp_recs("""\
            ---
            gameweek: 5
            ---
            ## Classic League
            ### Classic Squad
            ### Bench Order
            | Bench Slot | Player | Score | Rationale |
            |------------|--------|-------|-----------|
            | GK | Verbruggen | 40 | |
            ### Transfer Recommendations
            | Priority | Out | In | Outlook | This GW | Net Cost | Rationale |
            |----------|-----|----|---------|---------|----------|-----------|
            | 1 | Tzolis (ARS) | Gibbs-White (NFO) | | | | |
            ## Draft League
        """)
        result = parse_recommendations(path)
        assert result["classic"]["squad"] == []
        assert result["classic"]["transfers"][0]["in"] == "Gibbs-White"

    def test_mode_read_from_the_squad_block_when_frontmatter_has_none(self, tmp_recs):
        """The file's opening `**Mode:** squad-builder` names no chip; the
        squad block's own Mode line does."""
        path = tmp_recs("""\
            ---
            gameweek: 6
            ---
            **Mode:** squad-builder
            ## Classic League
            ### Classic Squad
            #### Constraints
            **Mode:** Free Hit
            #### Starting XI
            | Pos | Player | Team | Price |
            |-----|--------|------|-------|
            | GK | Raya | ARS | 5.5 |
            | FWD | Haaland (C) | MCI | 14.5 |
            #### Bench
            | Order | Player | Team | Pos | Price | Role |
            |-------|--------|------|-----|-------|------|
            | GK | Verbruggen | BHA | GK | 4.5 | Backup |
            ## Draft League
        """)
        result = parse_recommendations(path)
        assert result["classic"]["mode"] == "freehit"
        assert result["classic"]["squad"] == [
            {"name": "Raya", "team": "ARS"},
            {"name": "Haaland", "team": "MCI"},
            {"name": "Verbruggen", "team": "BHA"},
        ]


# ---------------------------------------------------------------------------
# Review findings on #401
# ---------------------------------------------------------------------------

class TestParseRobustness:

    @pytest.mark.parametrize(("cell", "team"), [
        ("Salah (vs WHU)", None),
        ("Salah (home vs WHU)", None),
        ("Salah (FDR 2)", None),
        ("Hermansen (WHU, GK)", "WHU"),
        ("Haaland (MCI) (C)", "MCI"),
    ])
    def test_a_club_is_read_only_from_a_bracket_of_codes(self, tmp_recs, cell, team):
        """The squad-builder captain line puts a rationale in the bracket, so
        an opponent there is not the player's club."""
        path = tmp_recs(f"""\
            ---
            gameweek: 6
            ---
            ## Classic League
            **Captain:** {cell} | **Vice:** Saka
        """)
        result = parse_recommendations(path)
        assert result["classic"]["captain"] in {"Salah", "Hermansen", "Haaland"}
        assert result["classic"]["captain_team"] == team

    def test_a_dash_only_placeholder_row_does_not_cost_the_row_above(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 5
            mode: transfer
            ---
            ## Classic League
            ### Transfer Recommendations
            | Priority | Out | In |
            |----------|-----|----|
            | 1 | Tzolis (ARS) | Gibbs-White (NFO) |
            | - | - | - |
            ## Draft League
        """)
        transfers = parse_recommendations(path)["classic"]["transfers"]
        assert [(t["out"], t["in"]) for t in transfers] == [("Tzolis", "Gibbs-White")]

    def test_a_gw_update_appended_after_the_draft_section_is_not_read(self, tmp_recs):
        """update-gw-prep appends `## GW Update` with a revised Transfers table."""
        path = tmp_recs("""\
            ---
            gameweek: 5
            mode: transfer
            ---
            ## Classic League
            ### Transfer Recommendations
            | Priority | Out | In |
            |----------|-----|----|
            | 1 | Tzolis (ARS) | Gibbs-White (NFO) |
            ## Draft League
            ### Waiver Recommendations
            | Priority | Drop | Claim | Position |
            |----------|------|-------|----------|
            | 1 | Wirtz (LIV) | Nyoni (LIV) | MID |

            ## GW Update
            ### Transfers
            | Out | In | Why |
            |-----|----|-----|
            | Tzolis (ARS) | Palmer (CHE) | late news |
        """)
        result = parse_recommendations(path)
        assert [w["in"] for w in result["draft"]["waivers"]] == ["Nyoni"]
        assert [t["in"] for t in result["classic"]["transfers"]] == ["Gibbs-White"]

    def test_a_gw_update_on_a_classic_only_file_is_not_read(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 5
            mode: transfer
            ---
            ## Classic League
            ### Transfer Recommendations
            | Priority | Out | In |
            |----------|-----|----|
            | 1 | Tzolis (ARS) | Gibbs-White (NFO) |

            ## GW Update
            ### Transfers
            | Out | In | Why |
            |-----|----|-----|
            | Tzolis (ARS) | Palmer (CHE) | late news |
        """)
        assert [t["in"] for t in parse_recommendations(path)["classic"]["transfers"]] == ["Gibbs-White"]

    @pytest.mark.parametrize("line", [
        "> **Captain:** Salah | **Vice:** Haaland",
        "1. **Captain:** Salah | **Vice:** Haaland",
        "**Formation:** 3-4-3 | **Captain:** Salah | **Vice:** Haaland",
    ])
    def test_captain_line_is_found_wherever_it_sits(self, tmp_recs, line):
        path = tmp_recs(f"""\
            ---
            gameweek: 6
            ---
            ## Classic League
            {line}
        """)
        result = parse_recommendations(path)
        assert result["classic"]["captain"] == "Salah"
        assert result["classic"]["vice_captain"] == "Haaland"

    def test_a_rolled_ankle_in_a_rationale_is_not_a_roll(self, tmp_recs):
        """No move parsed is exactly what an unrecognised column produces, so
        the roll test must not fire on any "rolled" in the section."""
        path = tmp_recs("""\
            ---
            gameweek: 5
            mode: transfer
            ---
            ## Classic League
            ### Transfer Recommendations
            | Priority | Sell | Bring in | Rationale |
            |----------|------|----------|-----------|
            | 1 | Saka (ARS) | Palmer (CHE) | Saka rolled his ankle in training |
            ## Draft League
        """)
        assert parse_recommendations(path)["classic"]["roll_transfer"] is False

    def test_rolling_the_free_transfer_said_in_prose_is_a_roll(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 5
            mode: transfer
            ---
            ## Classic League
            ### Transfer Recommendations
            Nothing clears the bar, so we are rolling the free transfer into GW6.
            ## Draft League
        """)
        assert parse_recommendations(path)["classic"]["roll_transfer"] is True

    def test_an_undemoted_squad_block_stays_inside_the_classic_section(self, tmp_recs):
        path = tmp_recs("""\
            ---
            gameweek: 6
            mode: wildcard
            ---
            ## Classic League
            ## Classic Squad
            ### Starting XI
            | Pos | Player | Team |
            |-----|--------|------|
            | GK | Raya | ARS |
            **Captain:** Haaland | **Vice:** Saka
            ## Draft League
        """)
        result = parse_recommendations(path)
        assert result["classic"]["squad"] == [{"name": "Raya", "team": "ARS"}]
        assert result["classic"]["captain"] == "Haaland"
