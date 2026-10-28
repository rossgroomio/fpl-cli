"""Tests for fines configuration parsing."""

import pytest

from fpl_cli.cli._context import ConfigError
from fpl_cli.cli._fines_config import (
    PLAYER_OF_THE_MONTH_VOUCHER,
    FineRule,
    VoucherRule,
    parse_fines_config,
)


class TestParseFinesConfig:
    def test_missing_fines_section_returns_none(self):
        assert parse_fines_config({}) is None

    def test_empty_fines_section_returns_none(self):
        assert parse_fines_config({"fines": {}}) is None

    def test_fines_with_no_rules_returns_none(self):
        assert parse_fines_config({"fines": {"classic": [], "draft": []}}) is None

    def test_valid_config_all_three_rule_types(self):
        settings = {
            "fines": {
                "escalation_note": "Fines double each GW",
                "classic": [
                    {"type": "last-place", "use_net_points": True, "penalty": "Pint on video"},
                    {"type": "red-card", "penalty": "Pint on video"},
                ],
                "draft": [
                    {"type": "last-place"},
                    {"type": "below-threshold", "threshold": 25, "penalty": "Pint on video"},
                ],
            }
        }
        config = parse_fines_config(settings)
        assert config is not None
        assert len(config.classic) == 2
        assert len(config.draft) == 2
        assert config.escalation_note == "Fines double each GW"

        assert config.classic[0] == FineRule(
            type="last-place", penalty="Pint on video"
        )
        assert config.classic[1] == FineRule(
            type="red-card", penalty="Pint on video"
        )
        assert config.draft[0] == FineRule(
            type="last-place", penalty="Fine triggered"
        )
        assert config.draft[1] == FineRule(
            type="below-threshold", threshold=25, penalty="Pint on video"
        )

    def test_classic_only_config(self):
        settings = {"fines": {"classic": [{"type": "red-card"}]}}
        config = parse_fines_config(settings)
        assert config is not None
        assert len(config.classic) == 1
        assert len(config.draft) == 0
        assert config.escalation_note is None

    def test_draft_only_config(self):
        settings = {"fines": {"draft": [{"type": "below-threshold", "threshold": 30}]}}
        config = parse_fines_config(settings)
        assert config is not None
        assert len(config.classic) == 0
        assert len(config.draft) == 1

    def test_defaults_applied_when_optional_fields_omitted(self):
        settings = {"fines": {"classic": [{"type": "last-place"}]}}
        config = parse_fines_config(settings)
        assert config is not None
        rule = config.classic[0]
        assert rule.penalty == "Fine triggered"
        assert rule.threshold is None

    def test_unknown_rule_type_raises(self):
        settings = {"fines": {"classic": [{"type": "own-goal"}]}}
        with pytest.raises(ConfigError, match="Unknown fine rule type 'own-goal'"):
            parse_fines_config(settings)

    def test_missing_type_field_raises(self):
        settings = {"fines": {"classic": [{"penalty": "Pint"}]}}
        with pytest.raises(ConfigError, match="missing required 'type' field"):
            parse_fines_config(settings)

    def test_non_string_penalty_raises(self):
        settings = {"fines": {"classic": [{"type": "last-place", "penalty": ["a", "b"]}]}}
        with pytest.raises(ConfigError, match="penalty must be a string"):
            parse_fines_config(settings)

    def test_below_threshold_missing_threshold_raises(self):
        settings = {"fines": {"draft": [{"type": "below-threshold"}]}}
        with pytest.raises(ConfigError, match="requires a 'threshold' value"):
            parse_fines_config(settings)

    def test_use_net_points_in_rule_silently_ignored(self):
        """Old configs with per-rule use_net_points are parsed without error."""
        settings = {"fines": {"classic": [{"type": "last-place", "use_net_points": True}]}}
        config = parse_fines_config(settings)
        assert config is not None
        assert not hasattr(config.classic[0], "use_net_points")


class TestEveryRejectionIsAConfigError:
    """#170: one exception type, so one boundary per command covers the block.

    `config_failure_boundary` catches `ConfigError` and nothing wider -- a
    bug in our own code should still surface as a traceback. That only works
    if every way a hand-edited `fines:` block can be wrong arrives as one.
    The shape cases below are why this class exists: each of them used to
    reach `.get` on something that has none and raise `AttributeError`, which
    no boundary catches, so `fpl status` exited 1 with an empty stdout and a
    traceback on stderr -- the exact failure the rule-type checks were fixed
    for.
    """

    MALFORMED = [
        pytest.param({"fines": "wooden spoon"}, "'fines' must be a mapping", id="fines-scalar"),
        pytest.param(
            {"fines": {"classic": "last-place"}},
            "'fines.classic' must be a list", id="format-scalar",
        ),
        pytest.param(
            {"fines": {"draft": [["last-place"]]}},
            "must be a mapping with a 'type' field", id="rule-not-a-mapping",
        ),
        pytest.param(
            {"fines": {"classic": ["last-place"]}},
            "must be a mapping with a 'type' field", id="rule-is-a-bare-string",
        ),
        pytest.param(
            {"fines": {"classic": [{"type": "wooden_spoon_XYZ"}]}},
            "Unknown fine rule type", id="unknown-type",
        ),
        pytest.param(
            {"fines": {"draft": [{"penalty": "Pint"}]}},
            "missing required 'type' field", id="no-type",
        ),
        pytest.param(
            {"fines": {"draft": [{"type": "below-threshold"}]}},
            "requires a 'threshold' value", id="no-threshold",
        ),
        pytest.param(
            {"fines": {"classic": [{"type": "last-place", "penalty": 5}]}},
            "penalty must be a string", id="non-string-penalty",
        ),
        pytest.param(
            {"fines": {"classic": [{"type": "below-threshold", "threshold": "40"}]}},
            "threshold must be a number", id="quoted-threshold",
        ),
        pytest.param(
            {"fines": {"classic": [{"type": "below-threshold", "threshold": True}]}},
            "threshold must be a number", id="bool-threshold",
        ),
        pytest.param(
            {"fines": {"clasic": [{"type": "last-place"}]}},
            "Unknown key", id="misspelled-format-key",
        ),
        pytest.param(
            {"fines": {"Classic": [{"type": "last-place"}]}},
            "Unknown key", id="wrong-case-format-key",
        ),
    ]

    @pytest.mark.parametrize("settings,expected", MALFORMED)
    def test_it_raises_config_error_and_says_what_is_wrong(self, settings, expected):
        with pytest.raises(ConfigError, match=expected):
            parse_fines_config(settings)

    def test_config_error_is_still_a_value_error(self):
        """The type narrowed; the base did not, so existing callers are unaffected."""
        assert issubclass(ConfigError, ValueError)


class TestWhatStaysLenient:
    """Not everything odd in the block is an error, and the line has a reason.

    A value that ends up being *used* is checked: `penalty` reaches a Rich
    console and `threshold` reaches `user_pts < rule.threshold`, so a wrong
    type there is a defect however it got in. A key nobody reads is left
    alone at rule level, because a stray one cannot make a rule vanish -- the
    rule still parses and still runs. A stray key at *block* level is the
    opposite and is rejected above: `clasic:` empties the format's rule list,
    and every caller downstream reads an empty list as "no fines configured".
    """

    def test_a_float_threshold_still_parses(self):
        """It compares correctly and always has; narrowing to `int` now would
        reject a settings.yaml that works today."""
        settings = {"fines": {"classic": [{"type": "below-threshold", "threshold": 30.5}]}}

        config = parse_fines_config(settings)

        assert config is not None
        assert config.classic[0].threshold == 30.5

    def test_an_unread_key_inside_a_rule_is_still_ignored(self):
        settings = {"fines": {"classic": [{"type": "last-place", "use_net_points": True}]}}

        config = parse_fines_config(settings)

        assert config is not None
        assert config.classic[0] == FineRule(type="last-place")

    def test_a_threshold_on_a_rule_that_never_reads_one_is_still_type_checked(self):
        """`FineRule.threshold` is typed and stored whatever the rule type, so
        a value that could never be compared is still worth naming."""
        settings = {"fines": {"classic": [{"type": "last-place", "threshold": "40"}]}}

        with pytest.raises(ConfigError, match="threshold must be a number"):
            parse_fines_config(settings)

    def test_all_three_documented_block_keys_are_accepted(self):
        settings = {
            "fines": {
                "classic": [{"type": "last-place"}],
                "draft": [{"type": "red-card"}],
                "escalation_note": "Fines double each GW",
            },
        }

        config = parse_fines_config(settings)

        assert config is not None
        assert config.escalation_note == "Fines double each GW"


class TestPenaltyPhrasings:
    """#503: the league's own ways of naming its forfeit, so the editorial can
    vary them instead of writing the penalty out every time."""

    def test_absent_everywhere_is_empty(self):
        config = parse_fines_config({"fines": {"classic": [{"type": "last-place", "penalty": "Pint on video"}]}})
        assert config is not None
        assert config.penalty_phrasings == ()
        assert config.classic[0].penalty_phrasings is None
        assert config.phrasings_for("Pint on video") == ()

    def test_the_block_list_covers_the_forfeit_no_rule_claims(self):
        config = parse_fines_config({"fines": {
            "penalty_phrasings": ["pint of shame", "penance pint"],
            "classic": [{"type": "last-place", "penalty": "Pint on video"}],
            "draft": [{"type": "last-place", "penalty": "Pint on video"}],
        }})
        assert config is not None
        assert config.phrasings_for("Pint on video") == ("pint of shame", "penance pint")

    def test_a_rules_own_list_replaces_the_block_for_its_forfeit(self):
        config = parse_fines_config({"fines": {
            "penalty_phrasings": ["pint of shame"],
            "classic": [
                {"type": "last-place", "penalty": "Pint on video"},
                {"type": "red-card", "penalty": "Buy the round", "penalty_phrasings": ["the round"]},
            ],
        }})
        assert config is not None
        assert config.phrasings_for("Pint on video") == ("pint of shame",)
        assert config.phrasings_for("Buy the round") == ("the round",)

    def test_a_forfeit_one_rule_claims_never_takes_the_block_through_another(self):
        """Review: two rules sharing a forfeit, one with its own list and one
        inheriting, used to hand that forfeit both lists."""
        config = parse_fines_config({"fines": {
            "penalty_phrasings": ["pint of shame"],
            "classic": [
                {"type": "last-place", "penalty": "Pint on video", "penalty_phrasings": ["penance pint"]},
                {"type": "red-card", "penalty": "Pint on video"},
            ],
        }})
        assert config is not None
        assert config.phrasings_for("Pint on video") == ("penance pint",)

    def test_an_empty_rule_list_opts_its_forfeit_out_of_the_block(self):
        config = parse_fines_config({"fines": {
            "penalty_phrasings": ["pint of shame"],
            "classic": [
                {"type": "last-place", "penalty": "Pint on video"},
                {"type": "red-card", "penalty": "Buy the round", "penalty_phrasings": []},
            ],
        }})
        assert config is not None
        assert config.classic[1].penalty_phrasings == ()
        assert config.phrasings_for("Buy the round") == ()
        assert config.phrasings_for("Pint on video") == ("pint of shame",)

    def test_a_block_list_over_two_unclaimed_forfeits_is_refused(self):
        """Review: it would hand "pint of shame" to the editorial as a way of
        saying "Buy the group a round", changing the forfeit."""
        with pytest.raises(ConfigError, match="the rules carry 2 different ones"):
            parse_fines_config({"fines": {
                "penalty_phrasings": ["pint of shame"],
                "classic": [
                    {"type": "last-place", "penalty": "Pint on video"},
                    {"type": "red-card", "penalty": "Buy the group a round"},
                ],
            }})

    def test_rules_naming_no_forfeit_do_not_count_against_the_block(self):
        config = parse_fines_config({"fines": {
            "penalty_phrasings": ["pint of shame"],
            "classic": [
                {"type": "last-place", "penalty": "Pint on video"},
                {"type": "red-card"},
                {"type": "below-threshold", "threshold": 20, "penalty": "  "},
            ],
        }})
        assert config is not None
        assert [rule.forfeit for rule in config.classic] == ["Pint on video", None, None]

    def test_blanks_and_repeats_are_dropped_and_entries_stripped(self):
        config = parse_fines_config({"fines": {
            "penalty_phrasings": [" pint of shame ", "", "pint of shame", "   ", "forfeit pint"],
            "classic": [{"type": "last-place"}],
        }})
        assert config is not None
        assert config.penalty_phrasings == ("pint of shame", "forfeit pint")

    @pytest.mark.parametrize("raw", ["pint of shame", {"a": "b"}, 3])
    def test_a_block_list_that_is_not_a_list_is_refused(self, raw):
        with pytest.raises(ConfigError, match="'fines.penalty_phrasings' must be a list of strings"):
            parse_fines_config({"fines": {"penalty_phrasings": raw, "classic": [{"type": "last-place"}]}})

    def test_a_rule_list_with_a_non_string_entry_is_refused_by_rule(self):
        with pytest.raises(ConfigError, match="Fine rule 'red-card' penalty_phrasings must be a list of strings"):
            parse_fines_config({"fines": {"classic": [
                {"type": "red-card", "penalty_phrasings": ["the round", 5]},
            ]}})


class TestDraftVouchers:
    """#492: the Player of the Month voucher, a grant configured beside the
    rules but never ruled against anyone."""

    def test_a_voucher_parses_with_its_penalty_and_overrides(self):
        settings = {"fines": {
            "draft": [{"type": "last-place", "penalty": "Pint on video"}],
            "draft_vouchers": [{
                "type": "player-of-the-month", "penalty": "Pint on video",
                "overrides": {"2026-09": 60307, "2026-10": 475168},
            }],
        }}

        config = parse_fines_config(settings)

        assert config is not None
        assert config.draft_vouchers == [VoucherRule(
            type="player-of-the-month", penalty="Pint on video",
            overrides={"2026-09": 60307, "2026-10": 475168},
        )]
        assert config.draft_voucher(PLAYER_OF_THE_MONTH_VOUCHER) == config.draft_vouchers[0]
        assert config.draft_vouchers[0].forfeit == "Pint on video"

    def test_vouchers_alone_are_a_config_that_rules_nothing(self):
        """A league can opt into the voucher without a single fine rule, and
        every reader of the rule lists still sees none."""
        config = parse_fines_config({"fines": {"draft_vouchers": [{"type": "player-of-the-month"}]}})

        assert config is not None
        assert config.classic == config.draft == []
        assert config.draft_vouchers[0].forfeit is None

    def test_a_month_written_as_the_feed_writes_it_is_padded(self):
        config = parse_fines_config({"fines": {"draft_vouchers": [
            {"type": "player-of-the-month", "overrides": {"2026-9": 60307}},
        ]}})

        assert config is not None
        assert config.draft_vouchers[0].overrides == {"2026-09": 60307}

    def test_no_voucher_of_a_type_is_none(self):
        config = parse_fines_config({"fines": {"draft": [{"type": "last-place"}]}})

        assert config is not None
        assert config.draft_voucher(PLAYER_OF_THE_MONTH_VOUCHER) is None

    @pytest.mark.parametrize(("vouchers", "expected"), [
        pytest.param("player-of-the-month", "'fines.draft_vouchers' must be a list of vouchers", id="scalar"),
        pytest.param(["player-of-the-month"], "must be a mapping with a 'type' field", id="bare-string"),
        pytest.param([{"penalty": "Pint"}], "missing required 'type' field", id="no-type"),
        pytest.param([{"type": "last-place"}], "Unknown voucher type 'last-place'", id="a-rule-type"),
        # Hashed by the type check, so a list there once raised `TypeError`
        # past the config boundary (#170).
        pytest.param(
            [{"type": ["player-of-the-month"]}], "Unknown voucher type \\['player-of-the-month'\\]",
            id="listed-type",
        ),
        # The override the "not checked" warnings point to, dropped by a typo.
        pytest.param(
            [{"type": "player-of-the-month", "override": {"2026-09": 60307}}],
            "Unknown key\\(s\\) in voucher 'player-of-the-month': 'override'", id="override-typo",
        ),
        pytest.param(
            [{"type": "player-of-the-month", "penalti": "Pint"}], "'penalti'. Valid keys: ", id="penalty-typo",
        ),
        pytest.param(
            [{"type": "player-of-the-month", "overrides": {"2026-9": 60307, "2026-09": 475168}}],
            "'2026-9' and '2026-09' are the same month", id="month-twice",
        ),
        pytest.param([{"type": "player-of-the-month", "penalty": 5}], "penalty must be a string", id="penalty"),
        pytest.param(
            [{"type": "player-of-the-month"}, {"type": "player-of-the-month"}],
            "lists 'player-of-the-month' more than once", id="twice",
        ),
        pytest.param(
            [{"type": "player-of-the-month", "overrides": [60307]}],
            'must map a month \\("2026-09"\\) to a player code', id="overrides-list",
        ),
        pytest.param(
            [{"type": "player-of-the-month", "overrides": {"September": 60307}}],
            "'September' is not a month", id="month-name",
        ),
        pytest.param(
            [{"type": "player-of-the-month", "overrides": {"2026-13": 60307}}],
            "'2026-13' is not a month", id="month-13",
        ),
        pytest.param(
            [{"type": "player-of-the-month", "overrides": {"2026-09": "60307"}}],
            "must be his FPL code, a whole number", id="quoted-code",
        ),
        pytest.param(
            [{"type": "player-of-the-month", "overrides": {"2026-09": True}}],
            "must be his FPL code, a whole number", id="bool-code",
        ),
        pytest.param(
            [{"type": "player-of-the-month", "penalty_phrasings": "pint of shame"}],
            "penalty_phrasings must be a list of strings", id="phrasings",
        ),
    ])
    def test_every_mistake_is_a_config_error_saying_what_is_wrong(self, vouchers, expected):
        with pytest.raises(ConfigError, match=expected):
            parse_fines_config({"fines": {"draft_vouchers": vouchers}})

    def test_a_date_yaml_read_as_a_date_is_no_month(self):
        """`2026-09-01:` unquoted is a date to YAML, never a month."""
        import datetime

        with pytest.raises(ConfigError, match="is not a month"):
            parse_fines_config({"fines": {"draft_vouchers": [
                {"type": "player-of-the-month", "overrides": {datetime.date(2026, 9, 1): 60307}},
            ]}})

    def test_a_misspelled_block_key_is_still_refused_by_name(self):
        with pytest.raises(ConfigError, match="Unknown key.*'draft_voucher'"):
            parse_fines_config({"fines": {"draft_voucher": [{"type": "player-of-the-month"}]}})

    def test_the_vouchers_own_phrasings_name_its_forfeit(self):
        config = parse_fines_config({"fines": {"draft_vouchers": [{
            "type": "player-of-the-month", "penalty": "Shot of Jäger",
            "penalty_phrasings": ["the Jäger", "a shot"],
        }]}})

        assert config is not None
        assert config.phrasings_for("Shot of Jäger") == ("the Jäger", "a shot")

    def test_the_block_list_covers_a_voucher_sharing_the_rules_forfeit(self):
        config = parse_fines_config({"fines": {
            "penalty_phrasings": ["pint of shame"],
            "draft": [{"type": "last-place", "penalty": "Pint on video"}],
            "draft_vouchers": [{"type": "player-of-the-month", "penalty": "Pint on video"}],
        }})

        assert config is not None
        assert config.phrasings_for("Pint on video") == ("pint of shame",)

    def test_a_block_list_over_a_rule_and_a_voucher_with_different_forfeits_is_refused(self):
        """The block's list would hand the rule's phrasings to the voucher's
        forfeit, which changes it."""
        with pytest.raises(ConfigError, match="2 different ones"):
            parse_fines_config({"fines": {
                "penalty_phrasings": ["pint of shame"],
                "draft": [{"type": "last-place", "penalty": "Pint on video"}],
                "draft_vouchers": [{"type": "player-of-the-month", "penalty": "Buy the round"}],
            }})
