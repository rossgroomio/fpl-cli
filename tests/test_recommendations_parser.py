"""Tests for the recommendations markdown parser."""

from pathlib import Path
from textwrap import dedent

import pytest

from fpl_cli.parsers.recommendations import parse_recommendations


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
        assert result["classic"]["transfers"][0] == {"in": "Iwobi", "out": "Miley"}
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
        assert result["classic"]["transfers"][0] == {"in": "Rice", "out": "Saka"}

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

    def test_transfer_with_l_miley_initial(self, tmp_recs):
        """Leading initial like 'L.Miley' should be stripped to 'Miley'."""
        path = tmp_recs("""\
            ---
            gameweek: 29
            ---
            ## Classic
            ##### Recommended Transfer (1): Iwobi <- L.Miley
            ## Draft
        """)
        result = parse_recommendations(path)
        assert result["classic"]["transfers"][0]["out"] == "Miley"

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
        assert result["classic"]["transfers"][0] == {"in": "Iwobi", "out": "Miley"}


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
        assert waivers[0] == {"priority": 1, "in": "Nyoni", "out": "Wirtz"}
        assert waivers[1] == {"priority": 2, "in": "Branthwaite", "out": "Saliba"}
        assert waivers[2] == {"priority": 3, "in": "Hermansen", "out": "Pope"}

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
