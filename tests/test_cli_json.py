"""Tests for shared JSON output infrastructure."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal
from enum import Enum
from io import StringIO

import pytest

from fpl_cli.cli._json import (
    _json_default,
    emit_json,
    emit_json_error,
    json_output_mode,
)


class TestJsonDefault:
    def test_datetime_to_iso(self):
        dt = datetime(2026, 3, 24, 14, 30, 0)
        assert _json_default(dt) == "2026-03-24T14:30:00"

    def test_decimal_to_float(self):
        assert _json_default(Decimal("10.5")) == 10.5

    def test_enum_to_value(self):
        class Colour(Enum):
            RED = "red"

        assert _json_default(Colour.RED) == "red"

    def test_unknown_type_raises(self):
        with pytest.raises(TypeError, match="set"):
            _json_default({1, 2, 3})


class TestEmitJson:
    def test_envelope_shape(self):
        buf = StringIO()
        emit_json("stats", [{"id": 1}], metadata={"gameweek": 30}, file=buf)
        data = json.loads(buf.getvalue())
        assert data["command"] == "stats"
        assert data["metadata"] == {"gameweek": 30}
        assert data["data"] == [{"id": 1}]

    def test_envelope_without_metadata(self):
        buf = StringIO()
        emit_json("history", [{"name": "Salah"}], file=buf)
        data = json.loads(buf.getvalue())
        assert data["command"] == "history"
        assert data["metadata"] == {}
        assert data["data"] == [{"name": "Salah"}]

    def test_serialises_datetime(self):
        buf = StringIO()
        emit_json("test", {"ts": datetime(2026, 1, 1)}, file=buf)
        data = json.loads(buf.getvalue())
        assert data["data"]["ts"] == "2026-01-01T00:00:00"

    def test_serialises_decimal(self):
        buf = StringIO()
        emit_json("test", {"price": Decimal("10.5")}, file=buf)
        data = json.loads(buf.getvalue())
        assert data["data"]["price"] == 10.5

    def test_output_is_indented(self):
        buf = StringIO()
        emit_json("test", {"a": 1}, file=buf)
        lines = buf.getvalue().strip().split("\n")
        assert len(lines) > 1  # indented, not single line


class TestEmitJsonError:
    def test_error_envelope(self):
        buf = StringIO()
        with pytest.raises(SystemExit) as exc_info:
            emit_json_error("captain", "Agent failed", file=buf)
        assert exc_info.value.code == 1
        data = json.loads(buf.getvalue())
        assert data["command"] == "captain"
        assert data["error"] == "Agent failed"

    def test_error_defaults_to_stderr(self, capsys):
        with pytest.raises(SystemExit):
            emit_json_error("test", "boom")
        captured = capsys.readouterr()
        data = json.loads(captured.err)
        assert data["error"] == "boom"


class TestJsonOutputMode:
    def test_redirects_stdout_to_stderr(self):
        with json_output_mode() as original_stdout:
            # sys.stdout should now point to stderr
            assert sys.stdout is sys.stderr
            # original_stdout should be the real stdout
            assert original_stdout is not sys.stderr

    def test_restores_stdout_after_context(self):
        original = sys.stdout
        with json_output_mode():
            pass
        assert sys.stdout is original

    def test_restores_stdout_on_exception(self):
        original = sys.stdout
        with pytest.raises(ValueError):
            with json_output_mode():
                raise ValueError("test")
        assert sys.stdout is original

    def test_print_inside_goes_to_stderr(self, capsys):
        with json_output_mode() as stdout:
            print("this goes to stderr")
            print('{"data": "json"}', file=stdout)
        captured = capsys.readouterr()
        assert "this goes to stderr" in captured.err
        assert '{"data": "json"}' in captured.out
