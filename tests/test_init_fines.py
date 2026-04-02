"""Tests for fines configuration in the init flow."""

from fpl_cli.cli._fines_config import VALID_RULE_TYPES
from fpl_cli.cli.init import _prompt_fines_config


class TestPromptFinesConfig:
    def test_classic_and_draft_rules(self, monkeypatch):
        """All rules enabled with custom settings."""
        confirm_responses = iter([
            True,   # last-place classic?
            True,   # below-threshold classic?
            True,   # red-card classic?
            True,   # last-place draft?
            True,   # below-threshold draft?
        ])
        prompt_responses = iter([
            "",     # penalty (last-place classic) - empty = no penalty key
            25,     # threshold (below-threshold classic)
            "",     # penalty (below-threshold classic)
            "",     # penalty (red-card classic)
            "",     # penalty (last-place draft)
            25,     # threshold (below-threshold draft)
            "",     # penalty (below-threshold draft)
            "Fines double each GW",  # escalation note
        ])
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: next(confirm_responses))
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(prompt_responses))

        result = _prompt_fines_config("both", {})

        assert len(result["classic"]) == 3
        assert result["classic"][0]["type"] == "last-place"
        assert "use_net_points" not in result["classic"][0]
        assert result["classic"][1]["type"] == "below-threshold"
        assert "use_net_points" not in result["classic"][1]
        assert result["classic"][2]["type"] == "red-card"
        assert len(result["draft"]) == 2
        assert result["draft"][0]["type"] == "last-place"
        assert result["draft"][1]["type"] == "below-threshold"
        assert result["draft"][1]["threshold"] == 25
        assert result["escalation_note"] == "Fines double each GW"

    def test_no_rules_enabled(self, monkeypatch):
        """All rules declined."""
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: False)
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: "")

        result = _prompt_fines_config("both", {})

        assert "classic" not in result
        assert "draft" not in result

    def test_classic_only_format(self, monkeypatch):
        """Classic-only format skips draft rules."""
        confirm_responses = iter([
            True,   # last-place classic?
            False,  # below-threshold classic?
            False,  # red-card classic?
        ])
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: next(confirm_responses))
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: "")  # penalty + escalation

        result = _prompt_fines_config("classic", {})

        assert len(result.get("classic", [])) == 1
        assert "draft" not in result

    def test_existing_config_used_as_defaults(self, monkeypatch):
        """Existing config values are passed as defaults (we just check it doesn't crash)."""
        existing = {
            "classic": [
                {"type": "last-place", "penalty": "Pint on video"},
            ],
            "escalation_note": "Fines double each GW",
        }
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: False)
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: "")

        result = _prompt_fines_config("both", existing)
        # All declined, so no rules
        assert "classic" not in result


class TestInitFinesCoverage:
    def test_all_valid_rule_types_offered_in_init(self, monkeypatch):
        """Init flow must cover every rule type in VALID_RULE_TYPES."""
        offered_types: set[str] = set()

        def tracking_confirm(msg, **kw):
            for rt in VALID_RULE_TYPES:
                if rt in msg.lower() or rt.replace("-", " ") in msg.lower():
                    offered_types.add(rt)
            return True

        monkeypatch.setattr("click.confirm", tracking_confirm)
        monkeypatch.setattr("click.prompt", lambda *_a, **kw: kw.get("default", ""))

        _prompt_fines_config("both", {})
        assert offered_types == VALID_RULE_TYPES, (
            f"Init flow missing rule types: {VALID_RULE_TYPES - offered_types}"
        )
