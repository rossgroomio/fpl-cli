"""Tests for the football-data.org API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from fpl_cli.api.football_data import FootballDataClient


@pytest.fixture
def client():
    with patch.dict("os.environ", {"FOOTBALL_DATA_API_KEY": "test-key"}):
        return FootballDataClient()


@pytest.fixture
def mock_standings_response():
    return {
        "competition": {"name": "Premier League"},
        "standings": [
            {
                "type": "TOTAL",
                "table": [
                    {
                        "position": 1,
                        "team": {
                            "id": 64,
                            "name": "Liverpool FC",
                            "shortName": "Liverpool",
                            "tla": "LIV",
                        },
                        "playedGames": 29,
                        "won": 22,
                        "draw": 5,
                        "lost": 2,
                        "goalDifference": 41,
                        "points": 71,
                    },
                    {
                        "position": 2,
                        "team": {
                            "id": 57,
                            "name": "Arsenal FC",
                            "shortName": "Arsenal",
                            "tla": "ARS",
                        },
                        "playedGames": 29,
                        "won": 18,
                        "draw": 7,
                        "lost": 4,
                        "goalDifference": 30,
                        "points": 61,
                    },
                ],
            },
            {
                "type": "HOME",
                "table": [],
            },
        ],
    }


def test_is_configured_true(client):
    assert client.is_configured is True


def test_is_configured_false():
    with patch.dict("os.environ", {}, clear=True):
        c = FootballDataClient()
        assert c.is_configured is False


async def test_get_standings_parses_response(client, mock_standings_response):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_standings_response
    mock_response.raise_for_status = MagicMock()

    client._http = AsyncMock()
    client._http.get.return_value = mock_response

    result = await client.get_standings()

    assert len(result) == 2

    liverpool = result[0]
    assert liverpool["position"] == 1
    assert liverpool["name"] == "Liverpool"
    assert liverpool["short_name"] == "LIV"
    assert liverpool["played"] == 29
    assert liverpool["win"] == 22
    assert liverpool["draw"] == 5
    assert liverpool["loss"] == 2
    assert liverpool["goal_difference"] == 41
    assert liverpool["points"] == 71

    arsenal = result[1]
    assert arsenal["position"] == 2
    assert arsenal["name"] == "Arsenal"
    assert arsenal["short_name"] == "ARS"


async def test_get_standings_returns_empty_on_http_error(client):
    client._http = AsyncMock()
    client._http.get.side_effect = httpx.HTTPStatusError(
        "Server Error",
        request=httpx.Request("GET", "https://example.com"),
        response=httpx.Response(500),
    )

    result = await client.get_standings()

    assert result == []


async def test_get_standings_returns_empty_when_not_configured():
    with patch.dict("os.environ", {}, clear=True):
        c = FootballDataClient()
        result = await c.get_standings()

    assert result == []
