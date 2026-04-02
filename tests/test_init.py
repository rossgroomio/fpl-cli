"""Tests for the init command tier functions, merge logic, and summary table."""

from __future__ import annotations

import os
from io import StringIO
from unittest.mock import patch

import pytest

from fpl_cli.cli.init import (
    StatusDisplay,
    _detect_ai_status,
    _detect_fines_status,
    _detect_fpl_ids_status,
    _detect_fpl_login_status,
    _detect_league_table_status,
    _mask_key,
    _render_summary_table,
    _tier_ai_features,
    _tier_fines,
    _tier_fpl_ids,
    _tier_fpl_login,
    _tier_league_table,
)


class TestStatusDisplay:
    def test_named_tuple_fields(self):
        s = StatusDisplay("Configured", "green")
        assert s.text == "Configured"
        assert s.colour == "green"


class TestMaskKey:
    def test_long_key(self):
        assert _mask_key("abcdefghij") == "abcd...ghij"

    def test_short_key(self):
        assert _mask_key("short") == "****"


class TestTierFplIds:
    def test_classic_only(self, monkeypatch):
        """Classic format collects classic IDs only."""
        responses = iter([123456, 789012])
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))

        data: dict = {}
        _tier_fpl_ids(data, "classic")

        assert data["fpl"]["classic_entry_id"] == 123456
        assert data["fpl"]["classic_league_id"] == 789012
        assert "draft_entry_id" not in data["fpl"]

    def test_format_downgrade_removes_draft_ids(self, monkeypatch):
        """Switching from both to classic removes draft IDs."""
        responses = iter([111, 222])
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))

        data: dict = {"fpl": {"classic_entry_id": 1, "draft_entry_id": 99, "draft_league_id": 88}}
        _tier_fpl_ids(data, "classic")

        assert data["fpl"]["classic_entry_id"] == 111
        assert "draft_entry_id" not in data["fpl"]
        assert "draft_league_id" not in data["fpl"]

    def test_format_downgrade_removes_classic_ids(self, monkeypatch):
        """Switching from both to draft removes classic IDs."""
        responses = iter([55])
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.echo", lambda *_a, **_kw: None)
        monkeypatch.setattr("fpl_cli.cli.init._fetch_draft_league_id", lambda eid: 42)

        data: dict = {"fpl": {"classic_entry_id": 1, "classic_league_id": 2}}
        _tier_fpl_ids(data, "draft")

        assert "classic_entry_id" not in data["fpl"]
        assert "classic_league_id" not in data["fpl"]
        assert data["fpl"]["draft_entry_id"] == 55
        assert data["fpl"]["draft_league_id"] == 42

    def test_merge_preserves_custom_keys(self, monkeypatch):
        """Merge preserves sub-keys not managed by init."""
        responses = iter([100, 200])
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))

        data: dict = {"fpl": {"classic_entry_id": 1, "custom_field": "keep_me"}}
        _tier_fpl_ids(data, "classic")

        assert data["fpl"]["classic_entry_id"] == 100
        assert data["fpl"]["custom_field"] == "keep_me"


class TestTierAiFeatures:
    def test_skip_preserves_existing(self, monkeypatch):
        """Skipping AI features preserves existing LLM config."""
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: False)


        existing_llm = {"research": {"provider": "perplexity", "model": "sonar-pro"}}
        data: dict = {"llm": existing_llm.copy()}
        _tier_ai_features(data)

        assert data["llm"]["research"]["provider"] == "perplexity"

    def test_configure_when_not_configured(self, monkeypatch):
        """Prompt says 'Configure' when not previously configured."""
        prompts_seen: list[str] = []

        def tracking_confirm(msg, **kw):
            prompts_seen.append(msg)
            return False

        monkeypatch.setattr("click.confirm", tracking_confirm)


        _tier_ai_features({})
        assert "Configure AI Features?" in prompts_seen[0]

    def test_reconfigure_when_configured(self, monkeypatch):
        """Prompt says 'Reconfigure' when already configured."""
        prompts_seen: list[str] = []

        def tracking_confirm(msg, **kw):
            prompts_seen.append(msg)
            return False

        monkeypatch.setattr("click.confirm", tracking_confirm)


        _tier_ai_features({"llm": {"research": {"provider": "perplexity"}}})
        assert "configured" in prompts_seen[0]
        assert "Reconfigure" in prompts_seen[0]


class TestTierLeagueTable:
    def test_configure_writes_env_file(self, monkeypatch, tmp_path):
        """Tier 3 writes FOOTBALL_DATA_API_KEY to .env file."""
        env_file = tmp_path / ".env"
        monkeypatch.setattr("fpl_cli.cli.init._env_file", lambda: env_file)

        responses = iter([True, "test-api-key-123"])
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.echo", lambda *_a, **_kw: None)


        _tier_league_table()

        content = env_file.read_text()
        assert "FOOTBALL_DATA_API_KEY" in content
        assert "test-api-key-123" in content

    def test_skip_preserves_existing_env(self, monkeypatch, tmp_path):
        """Skipping Tier 3 preserves existing .env content."""
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER_VAR=keep_me\n")
        monkeypatch.setattr("fpl_cli.cli.init._env_file", lambda: env_file)

        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: False)


        _tier_league_table()

        assert "OTHER_VAR=keep_me" in env_file.read_text()

    def test_update_existing_key(self, monkeypatch, tmp_path):
        """Updating an existing FOOTBALL_DATA_API_KEY preserves other vars."""
        env_file = tmp_path / ".env"
        env_file.write_text('OTHER_VAR="keep"\nFOOTBALL_DATA_API_KEY="old-key"\n')
        monkeypatch.setattr("fpl_cli.cli.init._env_file", lambda: env_file)

        responses = iter([True, "new-key"])
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.echo", lambda *_a, **_kw: None)


        _tier_league_table()

        content = env_file.read_text()
        assert "new-key" in content
        assert "OTHER_VAR" in content

    def test_env_file_permissions(self, monkeypatch, tmp_path):
        """The .env file gets 0o600 permissions on non-Windows."""
        if os.name == "nt":
            pytest.skip("POSIX permissions only")

        env_file = tmp_path / ".env"
        monkeypatch.setattr("fpl_cli.cli.init._env_file", lambda: env_file)

        responses = iter([True, "key-123"])
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.echo", lambda *_a, **_kw: None)


        _tier_league_table()

        assert oct(env_file.stat().st_mode & 0o777) == oct(0o600)

    def test_env_file_pre_created_before_write(self, monkeypatch, tmp_path):
        """The .env file is pre-created with 0o600 before set_key writes to it."""
        if os.name == "nt":
            pytest.skip("POSIX permissions only")

        env_file = tmp_path / ".env"
        monkeypatch.setattr("fpl_cli.cli.init._env_file", lambda: env_file)

        permissions_during_write: list[int] = []
        original_set_key = __import__("dotenv").set_key

        def tracking_set_key(*args, **kwargs):
            permissions_during_write.append(env_file.stat().st_mode & 0o777)
            return original_set_key(*args, **kwargs)

        monkeypatch.setattr("fpl_cli.cli.init.set_key", tracking_set_key)

        responses = iter([True, "key-123"])
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.echo", lambda *_a, **_kw: None)


        _tier_league_table()

        assert permissions_during_write[0] == 0o600

    def test_strips_whitespace_from_key(self, monkeypatch, tmp_path):
        """Whitespace is stripped from the API key input."""
        env_file = tmp_path / ".env"
        monkeypatch.setattr("fpl_cli.cli.init._env_file", lambda: env_file)

        responses = iter([True, "  key-with-spaces  "])
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.echo", lambda *_a, **_kw: None)


        _tier_league_table()

        content = env_file.read_text()
        assert "key-with-spaces" in content
        assert "  key-with-spaces  " not in content


class TestTierFines:
    def test_skip_preserves_existing_fines(self, monkeypatch):
        """Skipping fines tier preserves existing fines config."""
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: False)


        existing_fines = {"classic": [{"type": "last-place"}]}
        data: dict = {"fines": existing_fines.copy()}
        _tier_fines(data, "classic")

        assert data["fines"]["classic"][0]["type"] == "last-place"

    def test_context_aware_prompt_when_configured(self, monkeypatch):
        """Prompt shows 'configured' when fines already set."""
        prompts_seen: list[str] = []

        def tracking_confirm(msg, **kw):
            prompts_seen.append(msg)
            return False

        monkeypatch.setattr("click.confirm", tracking_confirm)


        _tier_fines({"fines": {"classic": [{"type": "last-place"}]}}, "classic")
        assert "configured" in prompts_seen[0]


class TestTierFplLogin:
    def test_keyring_unavailable(self, monkeypatch):
        """Returns silently when keyring probe fails."""
        monkeypatch.setattr("fpl_cli.cli.init._keyring_available", lambda: False)
        # Should not raise or prompt
        _tier_fpl_login()

    def test_skip_when_declined(self, monkeypatch):
        """Preserves state when user declines."""
        monkeypatch.setattr("fpl_cli.cli.init._keyring_available", lambda: True)
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: False)


        with patch("keyring.get_password", return_value=None):
            _tier_fpl_login()

    def test_configure_stores_credentials(self, monkeypatch):
        """Configured tier stores email and password in keyring."""
        monkeypatch.setattr("fpl_cli.cli.init._keyring_available", lambda: True)


        stored: dict[str, str] = {}
        responses = iter([True, "test@example.com", "secret123"])
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))

        with patch("keyring.get_password", return_value=None), \
             patch("keyring.set_password", side_effect=lambda svc, key, val: stored.update({key: val})):
            _tier_fpl_login()

        assert stored["email"] == "test@example.com"
        assert stored["password"] == "secret123"

    def test_reconfigure_keeps_existing_password(self, monkeypatch):
        """Pressing Enter on password keeps existing credential in keyring."""
        monkeypatch.setattr("fpl_cli.cli.init._keyring_available", lambda: True)


        stored: dict[str, str] = {}
        # confirm=True, email keeps existing "old@example.com", password empty
        responses = iter([True, "old@example.com", ""])
        monkeypatch.setattr("click.confirm", lambda *_a, **_kw: next(responses))
        monkeypatch.setattr("click.prompt", lambda *_a, **_kw: next(responses))

        with patch("keyring.get_password", return_value="old@example.com"), \
             patch("keyring.set_password", side_effect=lambda svc, key, val: stored.update({key: val})):
            _tier_fpl_login()

        assert stored["email"] == "old@example.com"
        assert "password" not in stored


class TestDetectStatuses:
    def test_fpl_ids_both(self):
        data = {"fpl": {"classic_entry_id": 1, "draft_league_id": 2}}
        result = _detect_fpl_ids_status(data)
        assert "both" in result.text
        assert result.colour == "green"

    def test_fpl_ids_classic_only(self):
        data = {"fpl": {"classic_entry_id": 1}}
        result = _detect_fpl_ids_status(data)
        assert "classic" in result.text

    def test_fpl_ids_not_configured(self):
        result = _detect_fpl_ids_status({})
        assert result.colour == "red"

    def test_ai_skipped(self):
        result = _detect_ai_status({})
        assert result.text == "Skipped"

    def test_fines_configured(self):
        data = {"fines": {"classic": [{"type": "last-place"}]}}
        result = _detect_fines_status(data)
        assert result.text == "Configured"
        assert result.colour == "green"

    def test_fines_empty(self):
        result = _detect_fines_status({"fines": {}})
        assert result.text == "Skipped"

    def test_league_table_from_env_file(self, monkeypatch, tmp_path):
        """Reads from .env file, not os.environ."""
        env_file = tmp_path / ".env"
        env_file.write_text('FOOTBALL_DATA_API_KEY="abc123"\n')
        monkeypatch.setattr("fpl_cli.cli.init._env_file", lambda: env_file)

        result = _detect_league_table_status()
        assert result.text == "Configured"
        assert result.colour == "green"

    def test_league_table_missing(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        monkeypatch.setattr("fpl_cli.cli.init._env_file", lambda: env_file)

        result = _detect_league_table_status()
        assert result.text == "Skipped"

    def test_fpl_login_configured(self):
        with patch("keyring.get_password", return_value="test@example.com"):
            result = _detect_fpl_login_status()
        assert result.text == "Configured"
        assert result.colour == "green"

    def test_fpl_login_keyring_unavailable(self):
        with patch("keyring.get_password", side_effect=Exception("No backend")):
            result = _detect_fpl_login_status()
        assert "unavailable" in result.text.lower()
        assert result.colour == "yellow"


class TestRenderSummaryTable:
    def test_renders_all_tier_names(self, monkeypatch, tmp_path):
        """Summary table renders without error and contains all tier names."""
        env_file = tmp_path / ".env"
        monkeypatch.setattr("fpl_cli.cli.init._env_file", lambda: env_file)

        output = StringIO()
        from rich.console import Console

        test_console = Console(file=output, width=120)
        monkeypatch.setattr("fpl_cli.cli.init.console", test_console)

        with patch("keyring.get_password", return_value=None):
            _render_summary_table({"fpl": {"classic_entry_id": 123}})

        rendered = output.getvalue()
        assert "FPL IDs" in rendered
        assert "AI Features" in rendered
        assert "football-data.org" in rendered
        assert "Fines Tracking" in rendered
        assert "FPL Login" in rendered
        assert "Setup Summary" in rendered


class TestFormatDefault:
    def test_defaults_to_classic_when_classic_configured(self):
        """resolve_format returns CLASSIC when only classic IDs present."""
        from fpl_cli.cli._context import Format, resolve_format

        data = {"fpl": {"classic_entry_id": 123}}
        result = resolve_format(data)
        assert result == Format.CLASSIC

    def test_defaults_to_both_when_no_config(self):
        """resolve_format returns None when no IDs configured."""
        from fpl_cli.cli._context import resolve_format

        result = resolve_format({})
        assert result is None
