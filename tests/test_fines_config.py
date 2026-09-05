"""Tests for fines configuration parsing."""

import pytest

from fpl_cli.cli._context import ConfigError
from fpl_cli.cli._fines_config import FineRule, parse_fines_config


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
    ]

    @pytest.mark.parametrize("settings,expected", MALFORMED)
    def test_it_raises_config_error_and_says_what_is_wrong(self, settings, expected):
        with pytest.raises(ConfigError, match=expected):
            parse_fines_config(settings)

    def test_config_error_is_still_a_value_error(self):
        """The type narrowed; the base did not, so existing callers are unaffected."""
        assert issubclass(ConfigError, ValueError)
