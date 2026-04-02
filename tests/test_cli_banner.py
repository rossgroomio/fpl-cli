"""Tests for ANSI Shadow banner display and terminal guards."""

from __future__ import annotations

from unittest.mock import PropertyMock, patch

from fpl_cli.cli._banner import _BANNER, _BANNER_WIDTH, show_banner


class TestBannerContent:
    def test_banner_is_six_lines(self):
        assert _BANNER.count("\n") == 5  # 6 lines, 5 newlines


class TestShowBanner:
    @patch("fpl_cli.cli._banner.console")
    def test_prints_banner_when_tty(self, mock_console):
        mock_console.is_terminal = True
        type(mock_console).width = PropertyMock(return_value=120)
        show_banner()
        assert mock_console.print.call_count == 2  # banner + blank line

    @patch("fpl_cli.cli._banner.console")
    def test_no_output_when_not_tty(self, mock_console):
        mock_console.is_terminal = False
        show_banner()
        mock_console.print.assert_not_called()

    @patch("fpl_cli.cli._banner.console")
    def test_no_output_when_terminal_too_narrow(self, mock_console):
        mock_console.is_terminal = True
        type(mock_console).width = PropertyMock(return_value=_BANNER_WIDTH)
        show_banner()
        mock_console.print.assert_not_called()
