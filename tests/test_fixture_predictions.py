"""Tests for fixture predictions service."""

from datetime import date

import pytest
import yaml

import fpl_cli.services.fixture_predictions as fixture_predictions_mod
from fpl_cli.paths import user_config_dir
from fpl_cli.season import get_season_year
from fpl_cli.services.fixture_predictions import (
    CONFIDENCE_MULTIPLIERS,
    CONFIG_FILENAME,
    BlankPrediction,
    Confidence,
    DoublePrediction,
    FixturePredictionsService,
    build_prediction_lookup,
)

# Derived from the same helper the service uses for staleness, so fixtures stay
# on the intended side of the season cutover year-round.
CURRENT_SEASON_DATE = date.today().isoformat()
PREVIOUS_SEASON_DATE = date(get_season_year() - 1, 9, 1).isoformat()


class TestBlankPrediction:
    """Tests for BlankPrediction dataclass."""

    def test_create_blank_prediction(self):
        pred = BlankPrediction(
            gameweek=29,
            teams=["ARS", "MCI"],
            confidence=Confidence.MEDIUM,
        )
        assert pred.gameweek == 29
        assert pred.teams == ["ARS", "MCI"]
        assert pred.confidence == Confidence.MEDIUM

    def test_from_dict(self):
        data = {
            "gameweek": 29,
            "teams": ["LIV", "CHE"],
            "confidence": "low",
        }
        pred = BlankPrediction.from_dict(data)
        assert pred.gameweek == 29
        assert pred.teams == ["LIV", "CHE"]
        assert pred.confidence == Confidence.LOW

    def test_from_dict_defaults(self):
        data = {"gameweek": 29, "teams": ["ARS"]}
        pred = BlankPrediction.from_dict(data)
        assert pred.confidence == Confidence.MEDIUM

    def test_from_dict_tolerates_legacy_status_source(self):
        """Backward compat: YAML with status/source keys doesn't error."""
        data = {
            "gameweek": 29,
            "teams": ["ARS"],
            "confidence": "high",
            "status": "completed",
            "source": "fixture_schedule",
        }
        pred = BlankPrediction.from_dict(data)
        assert pred.gameweek == 29
        assert pred.confidence == Confidence.HIGH


class TestDoublePrediction:
    """Tests for DoublePrediction dataclass."""

    def test_create_double_prediction(self):
        pred = DoublePrediction(
            gameweek=34,
            teams=["ARS", "MCI", "LIV"],
            confidence=Confidence.MEDIUM,
        )
        assert pred.gameweek == 34
        assert len(pred.teams) == 3

    def test_from_dict_tolerates_legacy_status_source(self):
        """Backward compat: YAML with status/source keys doesn't error."""
        data = {
            "gameweek": 34,
            "teams": ["MCI"],
            "confidence": "high",
            "status": "completed",
            "source": "official",
        }
        pred = DoublePrediction.from_dict(data)
        assert pred.gameweek == 34
        assert pred.confidence == Confidence.HIGH


class TestConfidenceEnum:
    def test_confidence_values(self):
        assert Confidence.CONFIRMED.value == "confirmed"
        assert Confidence.HIGH.value == "high"
        assert Confidence.MEDIUM.value == "medium"
        assert Confidence.LOW.value == "low"


class TestFixturePredictionsService:
    @pytest.fixture
    def temp_config(self, tmp_path):
        config_path = tmp_path / "fixture_predictions.yaml"
        initial_data = {
            "metadata": {"last_updated": CURRENT_SEASON_DATE, "notes": "test"},
            "predicted_blanks": [
                {"gameweek": 28, "teams": ["ARS"], "confidence": "medium"},
                {"gameweek": 31, "teams": ["MCI", "WOL"], "confidence": "high"},
                {"gameweek": 34, "teams": [], "confidence": "medium"},
            ],
            "predicted_doubles": [
                {"gameweek": 27, "teams": [], "confidence": "high"},
                {"gameweek": 33, "teams": [], "confidence": "high"},
            ],
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(initial_data, f)
        return config_path

    @pytest.fixture
    def service(self, temp_config):
        return FixturePredictionsService(config_path=temp_config)

    def test_get_predicted_blanks_all(self, service):
        blanks = service.get_predicted_blanks()
        assert len(blanks) == 3
        assert blanks[0].gameweek == 28
        assert blanks[2].gameweek == 34

    def test_get_predicted_blanks_by_gw(self, service):
        blanks = service.get_predicted_blanks(gw=31)
        assert len(blanks) == 1
        assert blanks[0].teams == ["MCI", "WOL"]

    def test_get_predicted_blanks_min_gw(self, service):
        blanks = service.get_predicted_blanks(min_gw=30)
        assert len(blanks) == 2
        assert all(b.gameweek >= 30 for b in blanks)

    def test_get_predicted_blanks_gw_and_min_gw(self, service):
        blanks = service.get_predicted_blanks(gw=28, min_gw=30)
        assert len(blanks) == 0

    def test_get_predicted_doubles_all(self, service):
        doubles = service.get_predicted_doubles()
        assert len(doubles) == 2

    def test_get_predicted_doubles_min_gw(self, service):
        doubles = service.get_predicted_doubles(min_gw=30)
        assert len(doubles) == 1
        assert doubles[0].gameweek == 33

    def test_predictions_sorted_by_gameweek(self, service):
        blanks = service.get_predicted_blanks()
        gameweeks = [b.gameweek for b in blanks]
        assert gameweeks == sorted(gameweeks)

    def test_get_metadata(self, service):
        metadata = service.get_metadata()
        assert metadata["last_updated"] == CURRENT_SEASON_DATE

    def test_missing_config_returns_empty(self, tmp_path):
        service = FixturePredictionsService(config_path=tmp_path / "nonexistent.yaml")
        assert service.get_predicted_blanks() == []
        assert service.get_predicted_doubles() == []

    def test_from_dict_backward_compat_with_legacy_yaml(self, tmp_path):
        """YAML with legacy status/source fields loads without error."""
        config_path = tmp_path / "legacy.yaml"
        data = {
            "metadata": {"last_updated": CURRENT_SEASON_DATE},
            "predicted_blanks": [
                {
                    "gameweek": 29,
                    "teams": ["ARS"],
                    "confidence": "high",
                    "status": "confirmed",
                    "source": "fixture_schedule",
                }
            ],
            "predicted_doubles": [
                {
                    "gameweek": 33,
                    "teams": ["MCI"],
                    "confidence": "medium",
                    "status": "predicted",
                    "source": "manual",
                }
            ],
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

        service = FixturePredictionsService(config_path=config_path)
        blanks = service.get_predicted_blanks()
        doubles = service.get_predicted_doubles()
        assert len(blanks) == 1
        assert blanks[0].confidence == Confidence.HIGH
        assert len(doubles) == 1

    def test_stale_predictions_return_empty(self, tmp_path):
        """Predictions from a previous season are suppressed."""
        config_path = tmp_path / "stale.yaml"
        data = {
            "metadata": {"last_updated": PREVIOUS_SEASON_DATE, "notes": "old season"},
            "predicted_blanks": [
                {"gameweek": 29, "teams": ["ARS"], "confidence": "high"},
            ],
            "predicted_doubles": [
                {"gameweek": 33, "teams": ["MCI"], "confidence": "medium"},
            ],
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

        service = FixturePredictionsService(config_path=config_path)
        assert service.get_predicted_blanks() == []
        assert service.get_predicted_doubles() == []
        assert service.is_stale is True

    def test_current_season_predictions_not_stale(self, service):
        """Predictions from the current season are served normally."""
        assert service.is_stale is False
        assert len(service.get_predicted_blanks()) == 3


class TestLayeredLookup:
    """A fixture_predictions.yaml in the user config dir overrides the shipped copy."""

    @staticmethod
    def _write(path, last_updated, blank_gw):
        data = {
            "metadata": {"last_updated": last_updated, "notes": "test"},
            "predicted_blanks": [{"gameweek": blank_gw, "teams": ["ARS"], "confidence": "high"}],
            "predicted_doubles": [],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

    @pytest.fixture
    def shipped(self, tmp_path, monkeypatch):
        """Fresh stand-in for the shipped package copy (blank at GW25)."""
        shipped = tmp_path / "shipped" / "fixture_predictions.yaml"
        self._write(shipped, CURRENT_SEASON_DATE, 25)
        monkeypatch.setattr(fixture_predictions_mod, "CONFIG_FILE", shipped)
        return shipped

    @pytest.fixture
    def user_file(self):
        return user_config_dir() / CONFIG_FILENAME

    def test_user_copy_wins_over_shipped(self, shipped, user_file):
        self._write(user_file, CURRENT_SEASON_DATE, 20)
        service = FixturePredictionsService()
        assert [b.gameweek for b in service.get_predicted_blanks()] == [20]
        assert service.config_path == user_file

    def test_falls_back_to_shipped_without_user_copy(self, shipped, user_file):
        service = FixturePredictionsService()
        assert [b.gameweek for b in service.get_predicted_blanks()] == [25]
        assert service.config_path == shipped

    def test_stale_user_copy_falls_through_to_shipped(self, shipped, user_file):
        self._write(user_file, PREVIOUS_SEASON_DATE, 20)
        service = FixturePredictionsService()
        assert [b.gameweek for b in service.get_predicted_blanks()] == [25]
        assert service.is_stale is False

    def test_stale_user_copy_fall_through_is_reported(self, shipped, user_file):
        """A deliberately-placed override that gets ignored must say so."""
        self._write(user_file, PREVIOUS_SEASON_DATE, 20)
        service = FixturePredictionsService()
        service.get_predicted_blanks()
        assert any(
            str(user_file) in w and "previous season" in w for w in service.load_warnings
        )

    def test_stale_shipped_copy_alone_is_not_double_reported(self, tmp_path, monkeypatch):
        """A stale final candidate is covered by is_stale, not load_warnings."""
        stale_shipped = tmp_path / "shipped.yaml"
        self._write(stale_shipped, PREVIOUS_SEASON_DATE, 25)
        monkeypatch.setattr(
            "fpl_cli.services.fixture_predictions.CONFIG_FILE", stale_shipped
        )
        service = FixturePredictionsService()
        assert service.get_predicted_blanks() == []
        assert service.is_stale is True
        assert service.load_warnings == []

    def test_unreadable_user_copy_falls_through_to_shipped(self, shipped, user_file):
        user_file.write_text("[unclosed", encoding="utf-8")
        service = FixturePredictionsService()
        assert [b.gameweek for b in service.get_predicted_blanks()] == [25]
        assert service.config_path == shipped
        assert any(str(user_file) in w for w in service.load_warnings)

    def test_unopenable_user_copy_falls_through_to_shipped(self, shipped, user_file):
        """An OSError (here: a directory where a file is expected) is survivable too."""
        user_file.mkdir(parents=True)
        service = FixturePredictionsService()
        assert [b.gameweek for b in service.get_predicted_blanks()] == [25]
        assert service.config_path == shipped
        assert any(str(user_file) in w for w in service.load_warnings)

    def test_non_mapping_user_copy_falls_through_to_shipped(self, shipped, user_file):
        """Valid YAML that is not a mapping must not crash the loader."""
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_text("- alpha\n- beta\n", encoding="utf-8")
        service = FixturePredictionsService()
        assert [b.gameweek for b in service.get_predicted_blanks()] == [25]
        assert service.is_stale is False
        assert any("mapping" in w for w in service.load_warnings)

    def test_empty_user_copy_falls_through_to_shipped(self, shipped, user_file):
        """A zero-byte file (interrupted write) must not mask the shipped copy."""
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_text("", encoding="utf-8")
        service = FixturePredictionsService()
        assert [b.gameweek for b in service.get_predicted_blanks()] == [25]
        assert service.config_path == shipped

    def test_half_written_user_copy_falls_through_to_shipped(self, shipped, user_file):
        """A file with prediction keys but no entries is not a usable override."""
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_text("predicted_blanks:\n", encoding="utf-8")
        service = FixturePredictionsService()
        assert [b.gameweek for b in service.get_predicted_blanks()] == [25]
        assert service.config_path == shipped

    def test_config_path_is_none_when_nothing_usable(self, tmp_path, monkeypatch, user_file):
        """config_path must not name a file the service never read."""
        monkeypatch.setattr(
            fixture_predictions_mod, "CONFIG_FILE", tmp_path / "absent" / "fixture_predictions.yaml"
        )
        service = FixturePredictionsService()
        assert service.get_predicted_blanks() == []
        assert service.config_path is None

    def test_explicit_path_read_error_propagates(self, tmp_path):
        """A caller that named a file is told when it cannot be parsed."""
        explicit = tmp_path / "explicit.yaml"
        explicit.write_text("[unclosed", encoding="utf-8")
        service = FixturePredictionsService(config_path=explicit)
        with pytest.raises(yaml.YAMLError):
            service.get_predicted_blanks()

    def test_explicit_empty_file_is_not_an_error(self, tmp_path):
        """With no later candidate, an empty file just means 'no predictions'."""
        explicit = tmp_path / "explicit.yaml"
        explicit.write_text("", encoding="utf-8")
        service = FixturePredictionsService(config_path=explicit)
        assert service.get_predicted_blanks() == []
        assert service.load_warnings == []

    def test_all_copies_stale_reports_stale_and_empty(self, tmp_path, monkeypatch, user_file):
        shipped = tmp_path / "shipped" / "fixture_predictions.yaml"
        self._write(shipped, PREVIOUS_SEASON_DATE, 25)
        monkeypatch.setattr(fixture_predictions_mod, "CONFIG_FILE", shipped)
        self._write(user_file, PREVIOUS_SEASON_DATE, 20)
        service = FixturePredictionsService()
        assert service.get_predicted_blanks() == []
        assert service.is_stale is True

    def test_explicit_config_path_bypasses_layering(self, shipped, user_file, tmp_path):
        self._write(user_file, CURRENT_SEASON_DATE, 20)
        explicit = tmp_path / "explicit.yaml"
        self._write(explicit, CURRENT_SEASON_DATE, 30)
        service = FixturePredictionsService(config_path=explicit)
        assert [b.gameweek for b in service.get_predicted_blanks()] == [30]
        assert service.config_path == explicit


# -- build_prediction_lookup tests --


def _make_team_map() -> dict[int, object]:
    """Build a minimal team_map: team_id -> object with .short_name."""

    class _Team:
        def __init__(self, tid: int, short_name: str):
            self.id = tid
            self.short_name = short_name

    return {
        1: _Team(1, "ARS"),
        2: _Team(2, "CHE"),
        3: _Team(3, "LIV"),
        4: _Team(4, "MCI"),
    }


def _make_service(tmp_path, blanks=None, doubles=None):
    """Create a FixturePredictionsService with given predictions."""
    data = {
        "metadata": {"last_updated": CURRENT_SEASON_DATE, "notes": "test"},
        "predicted_blanks": blanks or [],
        "predicted_doubles": doubles or [],
    }
    config = tmp_path / "fixture_predictions.yaml"
    with open(config, "w", encoding="utf-8") as f:
        yaml.dump(data, f)
    return FixturePredictionsService(config_path=config)


class TestBuildPredictionLookup:
    """Tests for build_prediction_lookup()."""

    def test_blanks_and_doubles_mixed_confidence(self, tmp_path):
        service = _make_service(
            tmp_path,
            blanks=[
                {"gameweek": 34, "teams": ["ARS", "CHE"], "confidence": "high"},
            ],
            doubles=[
                {"gameweek": 33, "teams": ["LIV", "MCI"], "confidence": "medium"},
            ],
        )
        team_map = _make_team_map()
        lookup = build_prediction_lookup(service, team_map, min_gw=33)

        # GW33 doubles
        assert lookup[33][3] == ("double", 0.5)  # LIV
        assert lookup[33][4] == ("double", 0.5)  # MCI
        # GW34 blanks
        assert lookup[34][1] == ("blank", 0.8)  # ARS
        assert lookup[34][2] == ("blank", 0.8)  # CHE

    def test_unknown_team_skipped(self, tmp_path):
        service = _make_service(
            tmp_path,
            blanks=[
                {"gameweek": 34, "teams": ["ARS", "XYZ"], "confidence": "high"},
            ],
        )
        team_map = _make_team_map()
        lookup = build_prediction_lookup(service, team_map, min_gw=34)

        assert 1 in lookup[34]  # ARS resolved
        # XYZ not in any GW entry
        for gw_teams in lookup.values():
            assert all(isinstance(tid, int) for tid in gw_teams)

    def test_empty_predictions_returns_empty(self, tmp_path):
        service = _make_service(tmp_path)
        lookup = build_prediction_lookup(service, _make_team_map(), min_gw=30)
        assert lookup == {}

    def test_double_overrides_blank_same_gw(self, tmp_path):
        service = _make_service(
            tmp_path,
            blanks=[
                {"gameweek": 34, "teams": ["ARS"], "confidence": "high"},
            ],
            doubles=[
                {"gameweek": 34, "teams": ["ARS"], "confidence": "low"},
            ],
        )
        lookup = build_prediction_lookup(service, _make_team_map(), min_gw=34)
        # Double takes precedence even at lower confidence
        assert lookup[34][1] == ("double", 0.25)

    def test_highest_confidence_wins_same_type(self, tmp_path):
        service = _make_service(
            tmp_path,
            blanks=[
                {"gameweek": 34, "teams": ["ARS"], "confidence": "low"},
                {"gameweek": 34, "teams": ["ARS"], "confidence": "high"},
            ],
        )
        lookup = build_prediction_lookup(service, _make_team_map(), min_gw=34)
        # High confidence (0.8) wins over low (0.25)
        assert lookup[34][1] == ("blank", 0.8)

    def test_confidence_multiplier_values(self):
        assert CONFIDENCE_MULTIPLIERS[Confidence.CONFIRMED] == 1.0
        assert CONFIDENCE_MULTIPLIERS[Confidence.HIGH] == 0.8
        assert CONFIDENCE_MULTIPLIERS[Confidence.MEDIUM] == 0.5
        assert CONFIDENCE_MULTIPLIERS[Confidence.LOW] == 0.25
