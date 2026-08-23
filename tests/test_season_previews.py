"""Tests for the season preview intel service and its decay policy."""

from datetime import date
from pathlib import Path

import pytest
import yaml

from fpl_cli.paths import user_config_dir
from fpl_cli.season import get_season_year, season_label
from fpl_cli.services.season_previews import (
    COVERAGE_THRESHOLD,
    PL_TEAM_COUNT,
    SECTION_DECAY,
    NameMatch,
    PlayerStatus,
    SeasonPreviewsService,
    Usability,
    expired_sections,
    live_sections,
    normalise_name,
    previews_dir,
    resolve_name,
    resolve_preview_names,
    section_confidence,
    team_set_warning,
    unknown_teams,
    unresolved_players,
    write_resolved_codes,
)

CURRENT_SEASON = season_label()
PREVIOUS_SEASON = season_label(get_season_year() - 1)


def write_preview(name: str, data: dict | str, *, directory: Path | None = None) -> Path:
    """Write a preview file into the (tmp-isolated) user previews dir."""
    target = directory or previews_dir()
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{name}.yaml"
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return path


def minimal(team: str = "ARS", **overrides) -> dict:
    """A valid preview carrying just enough content to count as coverage."""
    data = {
        "schema_version": 1,
        "team": team,
        "season": CURRENT_SEASON,
        "source": "Example Weekly",
        "published": date(get_season_year(), 8, 15),
        "narrative": "Something worth knowing.",
    }
    data.update(overrides)
    return data


class TestSectionConfidence:
    """Decay curve: the mechanism that keeps intel from outliving its usefulness."""

    def test_full_confidence_on_the_plateau(self):
        for section, (full_until, _) in SECTION_DECAY.items():
            assert section_confidence(section, full_until) == 1.0
            assert section_confidence(section, 1) == 1.0

    def test_zero_at_and_after_expiry(self):
        for section, (_, expires_at) in SECTION_DECAY.items():
            assert section_confidence(section, expires_at) == 0.0
            assert section_confidence(section, expires_at + 50) == 0.0

    def test_tapers_monotonically_between(self):
        previous = 1.0
        for gw in range(1, 15):
            current = section_confidence("projected_xi", gw)
            assert current <= previous
            previous = current

    def test_injuries_die_immediately_after_gw1(self):
        # The FPL API's own news field is authoritative once the season starts.
        assert section_confidence("injuries", 1) == 1.0
        assert section_confidence("injuries", 2) == 0.0

    def test_team_strength_outlives_the_ratings_prior_blend(self):
        # team_ratings_prior.BLENDING_CUTOFF_GW is 12: GW12 is the last gameweek
        # where a prior is blended at all, so intel must still be alive there.
        assert section_confidence("team_strength", 12) > 0
        assert section_confidence("team_strength", 13) == 0.0

    def test_unknown_section_is_treated_as_expired(self):
        assert section_confidence("not_a_section", 1) == 0.0

    def test_live_and_expired_partition_every_section(self):
        for gw in (1, 5, 13):
            assert set(live_sections(gw)) | set(expired_sections(gw)) == set(SECTION_DECAY)
            assert not set(live_sections(gw)) & set(expired_sections(gw))


class TestLoading:
    """Files that should load, and files that should be skipped with a reason."""

    def test_missing_directory_is_not_an_error(self):
        service = SeasonPreviewsService()
        assert service.get_previews() == []
        assert service.load_warnings == []

    def test_loads_a_valid_preview(self):
        write_preview("ARS", minimal())
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None
        assert preview.team == "ARS"
        assert preview.source == "Example Weekly"

    def test_team_lookup_is_case_insensitive(self):
        write_preview("ARS", minimal())
        assert SeasonPreviewsService().get_preview("ars") is not None

    def test_example_template_is_skipped(self):
        write_preview("EXAMPLE", minimal(team="EXAMPLE"))
        service = SeasonPreviewsService()
        assert service.get_previews() == []
        assert service.load_warnings == []

    def test_unreadable_yaml_is_skipped_with_a_warning(self):
        write_preview("BAD", "team: [unclosed\n")
        service = SeasonPreviewsService()
        assert service.get_previews() == []
        assert any("BAD" in w for w in service.load_warnings)

    def test_non_mapping_is_skipped(self):
        write_preview("LIST", "- one\n- two\n")
        service = SeasonPreviewsService()
        assert service.get_previews() == []
        assert any("expected a mapping" in w for w in service.load_warnings)

    def test_wrong_schema_version_is_rejected(self):
        write_preview("VER", minimal(schema_version=99))
        service = SeasonPreviewsService()
        assert service.get_previews() == []
        assert any("schema_version" in w for w in service.load_warnings)

    def test_absent_schema_version_is_tolerated(self):
        data = minimal()
        del data["schema_version"]
        write_preview("ARS", data)
        assert SeasonPreviewsService().get_preview("ARS") is not None

    @pytest.mark.parametrize("missing", ["team", "source", "published"])
    def test_missing_required_field_is_skipped(self, missing):
        data = minimal()
        del data[missing]
        write_preview("ARS", data)
        service = SeasonPreviewsService()
        assert service.get_previews() == []
        assert any(missing in w for w in service.load_warnings)

    def test_template_copy_with_sentinel_team_is_skipped_with_a_warning(self):
        # The filename check only covers the canonical EXAMPLE.yaml; a stray
        # copy must not load its fictional content as real intel.
        write_preview("EXAMPLE (1)", minimal(team="EXAMPLE"))
        service = SeasonPreviewsService()
        assert service.get_previews() == []
        assert any("template sentinel" in w for w in service.load_warnings)

    def test_previous_season_label_is_rejected(self):
        write_preview("ARS", minimal(season=PREVIOUS_SEASON))
        service = SeasonPreviewsService()
        assert service.get_previews() == []
        assert any(f"declares season {PREVIOUS_SEASON}" in w for w in service.load_warnings)

    def test_slash_separated_season_is_accepted(self):
        write_preview("ARS", minimal(season=CURRENT_SEASON.replace("-", "/")))
        assert SeasonPreviewsService().get_preview("ARS") is not None

    @pytest.mark.parametrize("variant", ["full_years", "full_years_slash", "short_years"])
    def test_alternate_current_season_formats_are_accepted(self, variant):
        start = get_season_year()
        label = {
            "full_years": f"{start}-{start + 1}",
            "full_years_slash": f"{start}/{start + 1}",
            "short_years": f"{str(start)[2:]}-{str(start + 1)[2:]}",
        }[variant]
        write_preview("ARS", minimal(season=label))
        assert SeasonPreviewsService().get_preview("ARS") is not None

    def test_pre_season_publication_date_is_current_without_a_label(self):
        # Season previews are routinely published in May/June for the season
        # starting in August; the July cutover must not call them stale.
        data = minimal(published=date(get_season_year(), 6, 15))
        del data["season"]
        write_preview("ARS", data)
        assert SeasonPreviewsService().get_preview("ARS") is not None

    def test_previous_season_detected_from_published_date_when_label_absent(self):
        data = minimal(published=date(get_season_year() - 1, 9, 1))
        del data["season"]
        write_preview("ARS", data)
        service = SeasonPreviewsService()
        assert service.get_previews() == []
        assert any("previous season" in w for w in service.load_warnings)

    def test_quoted_published_date_is_parsed(self):
        write_preview("ARS", minimal(published=f"{get_season_year()}-08-15"))
        assert SeasonPreviewsService().get_preview("ARS") is not None

    def test_duplicate_predicted_finish_warns_but_keeps_both(self):
        # From a single source's predicted table the finishes are a permutation,
        # so a shared value usually means a misread row at ingest.
        write_preview("ARS", minimal(team="ARS", predicted_finish=4))
        write_preview("LIV", minimal(team="LIV", predicted_finish=4))
        service = SeasonPreviewsService()
        assert len(service.get_previews()) == 2
        warning = next(w for w in service.load_warnings if "predict finish 4" in w)
        assert "ARS" in warning and "LIV" in warning

    def test_distinct_predicted_finishes_do_not_warn(self):
        write_preview("ARS", minimal(team="ARS", predicted_finish=4))
        write_preview("LIV", minimal(team="LIV", predicted_finish=5))
        write_preview("MCI", minimal(team="MCI"))  # no prediction at all
        service = SeasonPreviewsService()
        assert len(service.get_previews()) == 3
        assert not any("predict finish" in w for w in service.load_warnings)

    def test_duplicate_team_keeps_one_and_warns(self):
        write_preview("ARS", minimal())
        write_preview("arsenal", minimal())
        service = SeasonPreviewsService()
        assert len(service.get_previews()) == 1
        assert any("already loaded" in w for w in service.load_warnings)

    def test_explicit_directory_overrides_the_config_dir(self, tmp_path):
        elsewhere = tmp_path / "somewhere"
        write_preview("ARS", minimal(), directory=elsewhere)
        assert SeasonPreviewsService(elsewhere).get_preview("ARS") is not None
        assert SeasonPreviewsService().get_previews() == []


class TestFieldParsing:
    """Individual fields are dropped rather than taking the whole file down."""

    def test_player_status_is_parsed(self):
        write_preview("ARS", minimal(players=[{"name": "Saka", "code": 1, "status": "rotation"}]))
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None
        assert preview.players[0].status is PlayerStatus.ROTATION

    def test_invalid_status_falls_back_to_unknown(self):
        write_preview("ARS", minimal(players=[{"name": "Saka", "status": "definitely"}]))
        service = SeasonPreviewsService()
        preview = service.get_preview("ARS")
        assert preview is not None
        assert preview.players[0].status is PlayerStatus.UNKNOWN
        assert any("definitely" in w for w in service.load_warnings)

    def test_player_without_a_name_is_dropped(self):
        write_preview("ARS", minimal(players=[{"code": 1}, {"name": "Saka"}]))
        service = SeasonPreviewsService()
        preview = service.get_preview("ARS")
        assert preview is not None
        assert [p.name for p in preview.players] == ["Saka"]
        assert any("missing name" in w for w in service.load_warnings)

    def test_non_integer_code_becomes_none_with_a_warning(self):
        # A quoted "123456" parses as a string; without the warning the file is
        # silently uncorrectable, because the writer sees a code as present.
        write_preview("ARS", minimal(players=[{"name": "Saka", "code": "abc"}]))
        service = SeasonPreviewsService()
        preview = service.get_preview("ARS")
        assert preview is not None
        assert preview.players[0].code is None
        assert any("Dropping code" in w for w in service.load_warnings)

    def test_boolean_code_is_not_mistaken_for_an_integer(self):
        write_preview("ARS", minimal(players=[{"name": "Saka", "code": True}]))
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None
        assert preview.players[0].code is None

    def test_boolean_predicted_finish_is_rejected(self):
        # True is an int in Python; unguarded it would read as 1st place.
        write_preview("ARS", minimal(predicted_finish=True))
        service = SeasonPreviewsService()
        preview = service.get_preview("ARS")
        assert preview is not None
        assert preview.predicted_finish is None
        assert any("predicted_finish" in w for w in service.load_warnings)

    @pytest.mark.parametrize("value", [0, 21, "second"])
    def test_out_of_range_predicted_finish_is_dropped(self, value):
        write_preview("ARS", minimal(predicted_finish=value))
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None
        assert preview.predicted_finish is None

    def test_out_of_range_percentiles_are_dropped(self):
        write_preview("ARS", minimal(team_strength={"attack": 900, "defence": 64}))
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None and preview.team_strength is not None
        assert preview.team_strength.attack is None
        assert preview.team_strength.defence == 64

    def test_empty_team_strength_becomes_none(self):
        write_preview("ARS", minimal(team_strength={"attack": "very good"}))
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None
        assert preview.team_strength is None

    def test_bare_string_transfer_list_is_accepted(self):
        write_preview("ARS", minimal(transfers_in="One Player"))
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None
        assert preview.transfers_in == ["One Player"]


class TestHasContent:
    """A scaffolded stub must not masquerade as intel."""

    def test_headers_only_is_not_content(self):
        data = minimal()
        del data["narrative"]
        write_preview("ARS", data)
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None
        assert preview.has_content is False

    @pytest.mark.parametrize(
        "extra",
        [
            {"narrative": "words"},
            {"players": [{"name": "Saka"}]},
            {"predicted_finish": 4},
            {"team_strength": {"attack": 70}},
            {"transfers_in": ["Someone"]},
        ],
    )
    def test_any_real_field_counts_as_content(self, extra):
        data = minimal()
        del data["narrative"]
        data.update(extra)
        write_preview("ARS", data)
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None
        assert preview.has_content is True

    def test_stubs_are_excluded_from_coverage(self):
        stub = minimal()
        del stub["narrative"]
        for i in range(20):
            write_preview(f"T{i:02d}", {**stub, "team": f"T{i:02d}"})
        assert SeasonPreviewsService().coverage(1).teams == 0


class TestCoverage:
    """The policy that stops a half-filled preview set from biasing picks."""

    def _fill(self, count: int) -> None:
        for i in range(count):
            write_preview(f"T{i:02d}", minimal(team=f"T{i:02d}"))

    def test_no_previews_is_none(self):
        coverage = SeasonPreviewsService().coverage(1)
        assert coverage.usable_as is Usability.NONE
        assert coverage.teams == 0
        assert coverage.pct == 0.0

    def test_below_threshold_is_negative_filter_only(self):
        self._fill(10)
        coverage = SeasonPreviewsService().coverage(1)
        assert coverage.usable_as is Usability.NEGATIVE_FILTER_ONLY
        assert coverage.pct == 0.5

    def test_at_threshold_is_full(self):
        self._fill(int(PL_TEAM_COUNT * COVERAGE_THRESHOLD))
        assert SeasonPreviewsService().coverage(1).usable_as is Usability.FULL

    def test_one_short_of_threshold_is_not_full(self):
        self._fill(int(PL_TEAM_COUNT * COVERAGE_THRESHOLD) - 1)
        assert SeasonPreviewsService().coverage(1).usable_as is Usability.NEGATIVE_FILTER_ONLY

    def test_fully_decayed_coverage_is_none_however_complete(self):
        # A complete set that has aged out is worth exactly as much as no set.
        self._fill(PL_TEAM_COUNT)
        assert SeasonPreviewsService().coverage(1).usable_as is Usability.FULL
        assert SeasonPreviewsService().coverage(99).usable_as is Usability.NONE

    def test_pct_survives_a_zero_denominator(self):
        assert SeasonPreviewsService().coverage(1, total_teams=0).pct == 0.0

    def test_valid_teams_excludes_clubs_not_in_the_league(self):
        # A set carried forward across a relegation must not unlock full use on
        # the strength of files for clubs that left the league.
        self._fill(int(PL_TEAM_COUNT * COVERAGE_THRESHOLD))
        write_preview("GONE1", minimal(team="GONE1"))
        write_preview("GONE2", minimal(team="GONE2"))
        live = {f"T{i:02d}" for i in range(PL_TEAM_COUNT)}

        unfiltered = SeasonPreviewsService().coverage(1)
        filtered = SeasonPreviewsService().coverage(1, valid_teams=live)
        assert unfiltered.teams == filtered.teams + 2
        assert filtered.teams == int(PL_TEAM_COUNT * COVERAGE_THRESHOLD)

    def test_empty_valid_teams_counts_everything(self):
        # Offline there is no team list; validation must not zero coverage out.
        self._fill(2)
        assert SeasonPreviewsService().coverage(1, valid_teams=set()).teams == 2


class TestDecayedEmission:
    """as_dict / as_dicts strip what the gameweek has aged out."""

    def _rich(self) -> None:
        write_preview(
            "ARS",
            minimal(
                predicted_finish=2,
                team_strength={"attack": 86, "defence": 99},
                transfers_in=["New Arrival"],
                players=[
                    {
                        "name": "Saliba",
                        "code": 462424,
                        "status": "starter",
                        "injury": "Out until January.",
                        "role_change": "Moves to the left of the pair.",
                        "set_pieces": ["corners"],
                        "penalties": True,
                        "new_signing": True,
                        "notes": "Key defender.",
                    }
                ],
            ),
        )

    def test_gw1_emits_everything(self):
        self._rich()
        emitted = SeasonPreviewsService().as_dicts(1)[0]
        player = emitted["players"][0]
        assert emitted["predicted_finish"] == 2
        assert emitted["transfers_in"] == ["New Arrival"]
        assert {"status", "injury", "role_change", "set_pieces", "penalties", "new_signing"} <= set(player)

    def test_injuries_and_transfers_gone_by_gw5(self):
        self._rich()
        emitted = SeasonPreviewsService().as_dicts(5)[0]
        player = emitted["players"][0]
        assert "injury" not in player
        assert "transfers_in" not in emitted
        assert player["status"] == "starter"
        assert emitted["section_confidence"]["projected_xi"] == 0.5

    def test_new_signing_flag_outlives_the_window_until_real_minutes(self):
        # A deadline-day signing has no PL minutes when the window shuts, so the
        # flag decays with projected_xi (real minutes exist by then), not with
        # the team-level transfer lists (superseded by the roster at the close).
        self._rich()
        at_gw5 = SeasonPreviewsService().as_dicts(5)[0]
        assert "transfers_in" not in at_gw5
        assert at_gw5["players"][0]["new_signing"] is True
        at_gw7 = SeasonPreviewsService().as_dicts(7)[0]
        assert "new_signing" not in at_gw7["players"][0]

    def test_everything_gone_by_gw13(self):
        self._rich()
        assert SeasonPreviewsService().as_dicts(13) == []

    def test_player_reduced_to_a_bare_name_is_dropped(self):
        write_preview("ARS", minimal(players=[{"name": "Saliba", "code": 1, "injury": "Out."}]))
        service = SeasonPreviewsService()
        assert service.as_dicts(1)[0]["players"][0]["injury"] == "Out."
        assert "players" not in service.as_dicts(5)[0]

    def test_unknown_status_is_not_emitted(self):
        write_preview("ARS", minimal(players=[{"name": "Saka", "code": 1, "notes": "Good."}]))
        emitted = SeasonPreviewsService().as_dicts(1)[0]
        assert "status" not in emitted["players"][0]

    def test_contentless_previews_never_reach_consumers(self):
        data = minimal()
        del data["narrative"]
        write_preview("ARS", data)
        assert SeasonPreviewsService().as_dicts(1) == []

    def test_section_confidence_lists_only_live_sections(self):
        self._rich()
        emitted = SeasonPreviewsService().as_dicts(5)[0]
        assert "injuries" not in emitted["section_confidence"]
        assert set(emitted["section_confidence"]) == set(live_sections(5))

    def test_sections_present_names_only_sections_with_data(self):
        # A file with no set-piece notes must not claim live set-piece intel
        # just because the section has not aged out yet.
        write_preview("ARS", minimal(players=[{"name": "Saliba", "code": 1, "injury": "Out."}]))
        emitted = SeasonPreviewsService().as_dicts(1)[0]
        present = set(emitted["sections_present"])
        assert {"injuries", "narrative"} <= present  # minimal() carries a narrative
        assert "set_piece_duty" not in present
        assert "team_strength" not in present

    def test_sections_present_drops_expired_sections(self):
        write_preview("ARS", minimal(players=[{"name": "Saliba", "code": 1, "injury": "Out."}]))
        emitted = SeasonPreviewsService().as_dicts(5)[0]
        assert "injuries" not in emitted["sections_present"]


class TestValidators:
    """Cross-checks against the real roster, driven by the CLI."""

    def test_unknown_teams_flags_codes_with_no_premier_league_team(self):
        write_preview("ARS", minimal())
        write_preview("XYZ", minimal(team="XYZ"))
        previews = SeasonPreviewsService().get_previews()
        assert unknown_teams(previews, {"ARS", "LIV"}) == ["XYZ"]

    def test_unresolved_players_lists_notes_with_no_code(self):
        write_preview("ARS", minimal(players=[{"name": "Nameless"}, {"name": "Known", "code": 5}]))
        previews = SeasonPreviewsService().get_previews()
        assert unresolved_players(previews) == [("ARS", "Nameless")]


class TestPreviewsDir:
    """Path resolution must honour an override set after import."""

    def test_resolves_under_the_current_config_dir(self):
        assert previews_dir() == user_config_dir() / "previews"

    def test_follows_a_relocated_config_dir(self, tmp_path, monkeypatch):
        relocated = tmp_path / "relocated"
        relocated.mkdir()
        monkeypatch.setenv("FPL_CLI_CONFIG_DIR", str(relocated))
        assert previews_dir() == relocated / "previews"


class _FakePlayer:
    """Minimal stand-in carrying the four attributes resolution reads."""

    def __init__(self, code: int, web_name: str, first_name: str, second_name: str):
        self.code = code
        self.web_name = web_name
        self.first_name = first_name
        self.second_name = second_name


ARS_SQUAD = [
    _FakePlayer(208706, "Bruno G.", "Bruno", "Guimarães Rodriguez Moura"),
    _FakePlayer(184029, "Ødegaard", "Martin", "Ødegaard"),
    _FakePlayer(439509, "Tzolis", "Christos", "Tzolis"),
    _FakePlayer(462424, "Saliba", "William", "Saliba"),
    _FakePlayer(226597, "Gabriel", "Gabriel", "dos Santos Magalhães"),
    _FakePlayer(205651, "G.Jesus", "Gabriel", "Fernando de Jesus"),
    _FakePlayer(219847, "Havertz", "Kai", "Havertz"),
]


class TestNormaliseName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Ødegaard", "odegaard"),
            ("Bruno G.", "bruno g"),
            ("Guimarães", "guimaraes"),
            ("  Kai   Havertz ", "kai havertz"),
            # Non-decomposable letters, folded by the shared strip_diacritics
            # table rather than a local NFKD pass that would leave them intact.
            ("Łukasz Fabiański", "lukasz fabianski"),
            ("Đorđe Petrović", "dorde petrovic"),
        ],
    )
    def test_normalisation(self, raw, expected):
        assert normalise_name(raw) == expected


class TestResolveName:
    """Preview prose names people the way a reader would; codes must still land."""

    def test_exact_display_name(self):
        match = resolve_name("Saliba", ARS_SQUAD)
        assert (match.code, match.how) == (462424, "exact")

    def test_full_name_matches_a_short_display_name(self):
        # The game shows "Bruno G."; the preview writes "Bruno Guimaraes".
        match = resolve_name("Bruno Guimaraes", ARS_SQUAD)
        assert (match.code, match.how) == (208706, "fuzzy")

    @pytest.mark.parametrize("spelling", ["Ødegaard", "Odegaard", "ødegaard"])
    def test_accents_do_not_block_a_match(self, spelling):
        assert resolve_name(spelling, ARS_SQUAD).code == 184029

    def test_first_and_last_name(self):
        assert resolve_name("Christos Tzolis", ARS_SQUAD).code == 439509

    def test_exact_match_wins_over_a_shared_first_name(self):
        # "Gabriel" is also Gabriel Jesus's first name; the exact display-name
        # match must settle it rather than reporting an ambiguity.
        match = resolve_name("Gabriel", ARS_SQUAD)
        assert (match.code, match.how) == (226597, "exact")

    def test_genuine_ambiguity_is_reported_not_guessed(self):
        squad = [
            _FakePlayer(1, "Silva", "Bernardo", "Silva"),
            _FakePlayer(2, "Silva", "Thiago", "Silva"),
        ]
        match = resolve_name("Silva", squad)
        assert match.how == "ambiguous"
        assert match.code is None
        assert len(match.candidates) == 2

    def test_unknown_name_is_unmatched(self):
        match = resolve_name("Nonexistent Player", ARS_SQUAD)
        assert match.how == "unmatched"
        assert match.code is None

    def test_empty_name_is_unmatched(self):
        assert resolve_name("   ", ARS_SQUAD).how == "unmatched"

    def test_as_dict_omits_absent_fields(self):
        assert resolve_name("Nope", ARS_SQUAD).as_dict() == {"name": "Nope", "how": "unmatched"}


class TestResolvePreviewNames:
    def test_only_missing_by_default(self):
        write_preview("ARS", minimal(players=[
            {"name": "Saliba"},
            {"name": "Havertz", "code": 219847},
        ]))
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None
        assert [m.name for m in resolve_preview_names(preview, ARS_SQUAD)] == ["Saliba"]

    def test_all_re_resolves_coded_players(self):
        write_preview("ARS", minimal(players=[
            {"name": "Saliba"},
            {"name": "Havertz", "code": 219847},
        ]))
        preview = SeasonPreviewsService().get_preview("ARS")
        assert preview is not None
        matches = resolve_preview_names(preview, ARS_SQUAD, only_missing=False)
        assert [m.name for m in matches] == ["Saliba", "Havertz"]


class TestWriteResolvedCodes:
    def test_writes_codes_and_preserves_comments(self):
        path = write_preview("ARS", (
            "schema_version: 1\n"
            "team: ARS\n"
            f'season: "{CURRENT_SEASON}"\n'
            "source: Example Weekly\n"
            f"published: {get_season_year()}-08-15\n"
            "\n"
            "# a comment that must survive\n"
            "players:\n"
            '  - name: "Saliba"    # trailing comment\n'
            "    status: starter\n"
        ))
        service = SeasonPreviewsService()
        preview = service.get_preview("ARS")
        assert preview is not None
        written = write_resolved_codes(path, resolve_preview_names(preview, ARS_SQUAD))

        text = path.read_text()
        assert written == 1
        assert "code: 462424" in text
        assert "# a comment that must survive" in text
        assert "# trailing comment" in text
        assert '  - name: "Saliba"' in text  # indentation preserved

    def test_existing_codes_are_never_overwritten_by_default(self):
        path = write_preview("ARS", minimal(players=[{"name": "Saliba", "code": 999}]))
        matches = [NameMatch(name="Saliba", code=462424, matched_name="Saliba", how="exact")]
        assert write_resolved_codes(path, matches) == 0
        assert "999" in path.read_text()

    def test_overwrite_replaces_a_differing_code(self):
        # The --all path re-resolves coded players, so its writes must be able
        # to replace one -- otherwise the table shows a fix that never saves.
        path = write_preview("ARS", minimal(players=[{"name": "Saliba", "code": 999}]))
        matches = [NameMatch(name="Saliba", code=462424, matched_name="Saliba", how="exact")]
        assert write_resolved_codes(path, matches, overwrite=True) == 1
        assert "code: 462424" in path.read_text()
        assert "999" not in path.read_text()

    def test_overwrite_leaves_a_matching_code_alone(self):
        path = write_preview("ARS", minimal(players=[{"name": "Saliba", "code": 462424}]))
        matches = [NameMatch(name="Saliba", code=462424, matched_name="Saliba", how="exact")]
        assert write_resolved_codes(path, matches, overwrite=True) == 0

    def test_string_code_is_replaced_without_overwrite(self):
        # The loader discards a quoted code, so the player counts as unresolved;
        # the writer must agree, or the file is permanently stuck.
        path = write_preview("ARS", minimal(players=[{"name": "Saliba", "code": "999"}]))
        matches = [NameMatch(name="Saliba", code=462424, matched_name="Saliba", how="exact")]
        assert write_resolved_codes(path, matches) == 1
        assert "code: 462424" in path.read_text()

    def test_quoted_whitespace_around_a_name_still_writes(self):
        # The loader strips names, so the match is keyed on "Saliba"; the raw
        # scalar keeps its quoted padding and must still be found.
        path = write_preview("ARS", (
            "schema_version: 1\n"
            "team: ARS\n"
            f'season: "{CURRENT_SEASON}"\n'
            "source: Example Weekly\n"
            f"published: {get_season_year()}-08-15\n"
            "players:\n"
            '  - name: " Saliba "\n'
        ))
        matches = [NameMatch(name="Saliba", code=462424, matched_name="Saliba", how="exact")]
        assert write_resolved_codes(path, matches) == 1
        assert "462424" in path.read_text()

    def test_no_matches_leaves_the_file_untouched(self):
        path = write_preview("ARS", minimal(players=[{"name": "Saliba"}]))
        before = path.read_text()
        assert write_resolved_codes(path, [NameMatch(name="Saliba")]) == 0
        assert path.read_text() == before


class TestTeamSetWarning:
    """Promotion/relegation drift, reported through the shared team-set helper."""

    def _previews(self, teams: list[str]) -> list:
        for team in teams:
            write_preview(team, minimal(team=team))
        return SeasonPreviewsService().get_previews()

    def test_silent_without_a_live_team_list(self):
        previews = self._previews(["ARS"])
        coverage = SeasonPreviewsService().coverage(1)
        assert team_set_warning(previews, set(), coverage) is None

    def test_partial_coverage_names_only_the_clubs_that_should_not_be_there(self):
        # Missing clubs are work not yet done, not drift: naming them would
        # report 18 "problems" to somebody who has written two previews.
        previews = self._previews(["ARS", "IPS"])
        coverage = SeasonPreviewsService().coverage(1)
        message = team_set_warning(previews, {"ARS", "LIV", "MCI"}, coverage)
        assert message is not None
        assert "IPS" in message
        assert "LIV" not in message

    def test_partial_coverage_is_silent_when_every_club_is_real(self):
        previews = self._previews(["ARS"])
        coverage = SeasonPreviewsService().coverage(1)
        assert team_set_warning(previews, {"ARS", "LIV"}, coverage) is None

    def test_full_coverage_reports_both_directions(self):
        # The season-rollover case: last season's set, bulk-edited to the new
        # label, so it still covers relegated clubs and misses promoted ones.
        live = {f"T{i:02d}" for i in range(19)} | {"PROMOTED"}
        stored = [f"T{i:02d}" for i in range(19)] + ["RELEGATED"]
        previews = self._previews(stored)
        coverage = SeasonPreviewsService().coverage(1)
        assert coverage.usable_as is Usability.FULL

        message = team_set_warning(previews, live, coverage)
        assert message is not None
        assert "PROMOTED" in message
        assert "RELEGATED" in message

    def test_full_coverage_with_matching_sets_is_silent(self):
        live = {f"T{i:02d}" for i in range(20)}
        previews = self._previews(sorted(live))
        coverage = SeasonPreviewsService().coverage(1)
        assert team_set_warning(previews, live, coverage) is None
