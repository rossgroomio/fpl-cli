"""Tests for .agents/skills/gw-prep/scripts/claimable_returnees.py.

#418: the returnee radar's `id` is the main game's element id and the waiver
pool's is the draft's, and the two differ for players registered mid-season.
The script joins the two payloads on `code`, the key both carry, so the Draft
sub-agent is handed a claimable list rather than matching two payloads itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import load_gw_prep_script

SKILL_PATH = Path(__file__).parent.parent / ".agents/skills/gw-prep/SKILL.md"

_mod = load_gw_prep_script("claimable_returnees.py")
join = _mod.join
_run = _mod._run


# ---- Fixtures ----------------------------------------------------------------

# A late addition: main id 577, draft id 575, one code. Radar rows carry the
# main id; the pool row the same person as the draft numbers him.
LATE_ADDITION = {"id": 577, "code": 9001, "web_name": "Tomiyasu", "position": "DEF", "team": "Crystal Palace"}
# Owned by a rival, and his main id happens to be a stranger's draft id.
OWNED_ID_SHARER = {"id": 575, "code": 9002, "web_name": "Cherif", "position": "MID", "team": "Coventry"}
NO_CODE = {"id": 400, "code": None, "web_name": "Codeless", "position": "FWD", "team": "Hull"}

_RADAR = {
    "command": "returnees",
    "metadata": {"window": 4, "escalation_window": 2, "stash_upgrade_margin": 10.0},
    "data": {"entries": [LATE_ADDITION, OWNED_ID_SHARER, NO_CODE], "departures": []},
}

_POOL = [
    # The late addition, under his draft id.
    {"id": 575, "code": 9001, "player_name": "Tomiyasu", "position": "DEF", "team_short": "CRY"},
    # A stranger who holds the owned player's main id in the draft numbering.
    {"id": 577, "code": 7777, "player_name": "Stroud", "position": "MID", "team_short": "HUL"},
    {"id": 10, "code": 9003, "player_name": "Somebody", "position": "GK", "team_short": "ARS"},
]

_WAIVERS = {"command": "waivers", "metadata": {"format": "draft"}, "data": {"pool": _POOL, "top_targets": []}}


def _waivers(pool: object) -> dict:
    return {"command": "waivers", "metadata": {"format": "draft"}, "data": {"pool": pool}}


# ---- The join is on code, never id ------------------------------------------


def test_late_addition_is_claimable_through_code_though_its_ids_differ():
    """Draft id 575 and main id 577 are the same person; an id join would miss him."""
    result = join(_RADAR, _WAIVERS)

    assert result["ok"] is True
    assert result["entries"] == [LATE_ADDITION]
    assert 577 not in {row["id"] for row in _POOL if row["code"] == 9001}


def test_owned_player_whose_main_id_is_a_strangers_draft_id_is_not_claimable():
    """Pool row id 577 is Stroud, not Cherif: the id collision must not pass gate 3."""
    result = join(_RADAR, _WAIVERS)

    assert {"web_name": "Cherif", "code": 9002, "reason": "owned"} in result["excluded"]
    assert OWNED_ID_SHARER not in result["entries"]


def test_entry_without_code_is_excluded_rather_than_matched_on_anything_else():
    result = join(_RADAR, _WAIVERS)

    assert {"web_name": "Codeless", "code": None, "reason": "no-code"} in result["excluded"]


def test_pool_row_without_code_is_never_a_match():
    """A null code on one side matches nothing, not every other null."""
    pool = [{"id": 400, "code": None, "player_name": "Codeless", "position": "FWD", "team_short": "HUL"}, *_POOL]

    result = join(_RADAR, _waivers(pool))

    assert result["ok"] is True
    assert NO_CODE not in result["entries"]


def test_claimable_entries_are_passed_through_unchanged_with_the_radar_metadata():
    """The sub-agent quotes radar fields, so the row it gets is the radar's row."""
    result = join(_RADAR, _WAIVERS)

    assert result["entries"][0] == LATE_ADDITION
    assert result["metadata"] == {**_RADAR["metadata"], "pool_size": 3}


# ---- Failing closed ---------------------------------------------------------


def test_missing_pool_offers_nothing_to_claim():
    waivers = {"command": "waivers", "metadata": {}, "data": {"top_targets": [LATE_ADDITION]}}

    result = join(_RADAR, waivers)

    assert result["ok"] is False
    assert result["entries"] == []
    assert result["warnings"] == [{"type": "pool-unreadable"}]


def test_pool_from_an_older_fpl_cli_without_code_offers_nothing_to_claim():
    pool = [{k: v for k, v in row.items() if k != "code"} for row in _POOL]

    result = join(_RADAR, _waivers(pool))

    assert result["ok"] is False
    assert result["entries"] == []
    assert result["warnings"] == [{"type": "pool-without-code"}]


def test_empty_pool_is_readable_and_matches_nobody():
    result = join(_RADAR, _waivers([]))

    assert result["ok"] is True
    assert result["entries"] == []
    assert {e["reason"] for e in result["excluded"]} == {"owned", "no-code"}


def test_radar_without_entries_warns_and_claims_nobody():
    result = join({"command": "returnees", "metadata": {}, "data": {}}, _WAIVERS)

    assert result["ok"] is True
    assert result["entries"] == []
    assert result["warnings"] == [{"type": "radar-unreadable"}]


# ---- Entry point --------------------------------------------------------------


def test_run_reads_both_envelopes_and_emits_json(tmp_path, capsys):
    radar_path = tmp_path / "returnees.json"
    radar_path.write_text(json.dumps(_RADAR), encoding="utf-8")
    waivers_path = tmp_path / "waivers.json"
    waivers_path.write_text(json.dumps(_WAIVERS), encoding="utf-8")

    _run(str(radar_path), str(waivers_path))

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert [e["web_name"] for e in out["entries"]] == ["Tomiyasu"]


def test_run_on_a_file_that_is_not_an_envelope_fails_closed(tmp_path, capsys):
    radar_path = tmp_path / "returnees.json"
    radar_path.write_text("[]", encoding="utf-8")
    waivers_path = tmp_path / "waivers.json"
    waivers_path.write_text(json.dumps(_WAIVERS), encoding="utf-8")

    _run(str(radar_path), str(waivers_path))

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["entries"] == []
    assert out["warnings"] == [{"type": "envelope-unreadable"}]


# ---- The skill hands the sub-agent this script's output, not the raw join -----


def test_skill_runs_the_join_before_the_draft_prompt():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert 'scripts/claimable_returnees.py"' in text
    draft_prompt = text.split("### C2 -- Draft League Analysis")[1].split("### C2.5")[0]
    assert "{returnee_radar_draft}" in draft_prompt
    # The sub-agent is told never to redo the join on `id` -- the #418 bug.
    assert "match on `id`" in draft_prompt or "never on `id`" in draft_prompt
