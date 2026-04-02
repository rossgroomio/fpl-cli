"""Tests for FPL data models."""

import pytest

from fpl_cli.models.fixture import Fixture
from fpl_cli.models.player import Player, PlayerPosition, PlayerStatus
from tests.conftest import make_fixture, make_player, make_team


class TestPlayer:
    """Tests for Player model."""

    def test_player_creation_with_aliases(self):
        """Test creating a player with API field aliases."""
        player = Player(
            id=1,
            web_name="Salah",
            first_name="Mohamed",
            second_name="Salah",
            team=14,  # alias for team_id
            element_type=3,  # alias for position (MID)
            now_cost=130,
        )
        assert player.id == 1
        assert player.web_name == "Salah"
        assert player.team_id == 14
        assert player.position == PlayerPosition.MIDFIELDER

    def test_player_price_property(self):
        """Test price calculation from now_cost."""
        player = make_player(now_cost=130)
        assert player.price == 13.0

        player = make_player(now_cost=55)
        assert player.price == 5.5

        player = make_player(now_cost=100)
        assert player.price == 10.0

    def test_player_full_name_property(self):
        """Test full name concatenation."""
        player = make_player(first_name="Mohamed", second_name="Salah")
        assert player.full_name == "Mohamed Salah"

    def test_player_is_available_property(self):
        """Test availability check."""
        available = make_player(status=PlayerStatus.AVAILABLE)
        assert available.is_available is True

        injured = make_player(status=PlayerStatus.INJURED)
        assert injured.is_available is False

        suspended = make_player(status=PlayerStatus.SUSPENDED)
        assert suspended.is_available is False

        doubtful = make_player(status=PlayerStatus.DOUBTFUL)
        assert doubtful.is_available is False

    def test_player_position_name_property(self):
        """Test position name mapping."""
        gk = make_player(position=PlayerPosition.GOALKEEPER)
        assert gk.position_name == "GK"

        defender = make_player(position=PlayerPosition.DEFENDER)
        assert defender.position_name == "DEF"

        midfielder = make_player(position=PlayerPosition.MIDFIELDER)
        assert midfielder.position_name == "MID"

        forward = make_player(position=PlayerPosition.FORWARD)
        assert forward.position_name == "FWD"

    def test_player_position_enum_values(self):
        """Test PlayerPosition enum has correct integer values."""
        assert PlayerPosition.GOALKEEPER.value == 1
        assert PlayerPosition.DEFENDER.value == 2
        assert PlayerPosition.MIDFIELDER.value == 3
        assert PlayerPosition.FORWARD.value == 4

    def test_player_status_enum_values(self):
        """Test PlayerStatus enum has correct string values."""
        assert PlayerStatus.AVAILABLE.value == "a"
        assert PlayerStatus.DOUBTFUL.value == "d"
        assert PlayerStatus.INJURED.value == "i"
        assert PlayerStatus.SUSPENDED.value == "s"
        assert PlayerStatus.NOT_AVAILABLE.value == "n"
        assert PlayerStatus.UNAVAILABLE.value == "u"

    def test_player_with_expected_stats(self):
        """Test player with xG/xA fields."""
        player = make_player(
            expected_goals=10.5,
            expected_assists=7.2,
            expected_goal_involvements=17.7,
        )
        assert player.expected_goals == 10.5
        assert player.expected_assists == 7.2
        assert player.expected_goal_involvements == 17.7

    def test_player_has_code_field(self):
        """Player model should parse the code (element_code) field."""
        player = Player(
            id=1, web_name="Salah", first_name="Mohamed", second_name="Salah",
            team=14, element_type=3, now_cost=130, code=11,
        )
        assert player.code == 11

    def test_player_default_values(self):
        """Test player default values are set correctly."""
        player = Player(
            id=1,
            web_name="Test",
            first_name="Test",
            second_name="Player",
            team=1,
            element_type=3,
            now_cost=50,
        )
        assert player.cost_change_event == 0
        assert player.cost_change_start == 0
        assert player.selected_by_percent == 0.0
        assert player.transfers_in_event == 0
        assert player.transfers_out_event == 0
        assert player.status == PlayerStatus.AVAILABLE
        assert player.total_points == 0
        assert player.minutes == 0
        # New fields default correctly
        assert player.defensive_contribution == 0
        assert player.defensive_contribution_per_90 == 0.0
        assert player.penalties_saved == 0
        assert player.value_form == 0.0
        assert player.value_season == 0.0
        assert player.penalties_order is None
        assert player.corners_and_indirect_freekicks_order is None
        assert player.direct_freekicks_order is None

    def test_player_defensive_and_value_fields(self):
        """Test defensive contribution and value metric fields."""
        player = make_player(
            defensive_contribution=30,
            defensive_contribution_per_90=1.5,
            value_form=1.2,
            value_season=18.5,
            penalties_saved=3,
        )
        assert player.defensive_contribution == 30
        assert player.defensive_contribution_per_90 == 1.5
        assert player.value_form == 1.2
        assert player.value_season == 18.5
        assert player.penalties_saved == 3

    def test_player_set_piece_order_fields(self):
        """Test nullable set-piece order fields."""
        player = make_player(
            penalties_order=1,
            corners_and_indirect_freekicks_order=2,
            direct_freekicks_order=None,
        )
        assert player.penalties_order == 1
        assert player.corners_and_indirect_freekicks_order == 2
        assert player.direct_freekicks_order is None

    def test_player_starts_field(self):
        player = make_player(starts=19)
        assert player.starts == 19

    def test_player_starts_defaults_to_zero(self):
        player = make_player()
        assert player.starts == 0

    def test_player_team_join_date_field(self):
        player = make_player(team_join_date="2026-01-09")
        assert player.team_join_date == "2026-01-09"

    def test_player_team_join_date_defaults_to_none(self):
        player = make_player()
        assert player.team_join_date is None

    def test_player_appearances_derived(self):
        player = make_player(total_points=42, points_per_game=2.0)
        assert player.appearances == 21

    def test_player_appearances_zero_ppg(self):
        player = make_player(total_points=0, points_per_game=0.0)
        assert player.appearances == 0

    def test_player_appearances_sub_only(self):
        player = make_player(starts=0, total_points=6, points_per_game=1.0)
        assert player.appearances == 6

    def test_player_appearances_capped_at_38(self):
        """Near-zero ppg doesn't inflate appearances beyond max PL gameweeks."""
        player = make_player(total_points=5, points_per_game=0.05)
        assert player.appearances == 38


class TestTeam:
    """Tests for Team model."""

    def test_team_creation(self):
        """Test creating a team."""
        team = make_team(
            id=1,
            name="Arsenal",
            short_name="ARS",
            code=3,
        )
        assert team.id == 1
        assert team.name == "Arsenal"
        assert team.short_name == "ARS"
        assert team.code == 3

    def test_team_form_list_property(self):
        """Test form string to list conversion."""
        team = make_team(form="WDWLW")
        assert team.form_list == ["W", "D", "W", "L", "W"]

        # Empty form
        team_no_form = make_team(form=None)
        assert team_no_form.form_list == []

        team_empty_form = make_team(form="")
        assert team_empty_form.form_list == []

    def test_team_form_points_property(self):
        """Test form points calculation (W=3, D=1, L=0)."""
        team = make_team(form="WDWLW")  # 3+1+3+0+3 = 10
        assert team.form_points == 10

        team_all_wins = make_team(form="WWWWW")  # 15
        assert team_all_wins.form_points == 15

        team_all_losses = make_team(form="LLLLL")  # 0
        assert team_all_losses.form_points == 0

        team_all_draws = make_team(form="DDDDD")  # 5
        assert team_all_draws.form_points == 5

        # No form
        team_no_form = make_team(form=None)
        assert team_no_form.form_points == 0

    def test_team_strength_fields(self):
        """Test team strength rating fields."""
        team = make_team(
            strength=4,
            strength_overall_home=1250,
            strength_overall_away=1150,
            strength_attack_home=1200,
            strength_attack_away=1100,
            strength_defence_home=1180,
            strength_defence_away=1080,
        )
        assert team.strength == 4
        assert team.strength_overall_home == 1250
        assert team.strength_overall_away == 1150
        assert team.strength_attack_home == 1200
        assert team.strength_attack_away == 1100
        assert team.strength_defence_home == 1180
        assert team.strength_defence_away == 1080


class TestFixture:
    """Tests for Fixture model."""

    def test_fixture_creation_with_aliases(self):
        """Test creating a fixture with API field aliases."""
        fixture = Fixture(
            id=1,
            event=25,  # alias for gameweek
            team_h=1,  # alias for home_team_id
            team_a=2,  # alias for away_team_id
            team_h_difficulty=3,
            team_a_difficulty=4,
        )
        assert fixture.id == 1
        assert fixture.gameweek == 25
        assert fixture.home_team_id == 1
        assert fixture.away_team_id == 2
        assert fixture.home_difficulty == 3
        assert fixture.away_difficulty == 4

    def test_fixture_is_blank_property(self):
        """Test blank gameweek detection."""
        blank = make_fixture(gameweek=None)
        assert blank.is_blank is True

        regular = make_fixture(gameweek=25)
        assert regular.is_blank is False

    def test_fixture_get_difficulty_for_team(self):
        """Test getting difficulty for specific team."""
        fixture = make_fixture(
            home_team_id=1,
            away_team_id=2,
            home_difficulty=2,
            away_difficulty=4,
        )
        assert fixture.get_difficulty_for_team(1) == 2  # Home team
        assert fixture.get_difficulty_for_team(2) == 4  # Away team

    def test_fixture_get_difficulty_invalid_team(self):
        """Test getting difficulty for team not in fixture raises error."""
        fixture = make_fixture(home_team_id=1, away_team_id=2)
        with pytest.raises(ValueError, match="Team 99 not in fixture"):
            fixture.get_difficulty_for_team(99)

    def test_fixture_is_home_for_team(self):
        """Test home/away detection for team."""
        fixture = make_fixture(home_team_id=1, away_team_id=2)
        assert fixture.is_home_for_team(1) is True
        assert fixture.is_home_for_team(2) is False
        assert fixture.is_home_for_team(99) is False

    def test_fixture_get_opponent_id(self):
        """Test getting opponent ID."""
        fixture = make_fixture(home_team_id=1, away_team_id=2)
        assert fixture.get_opponent_id(1) == 2  # Home team's opponent
        assert fixture.get_opponent_id(2) == 1  # Away team's opponent

    def test_fixture_get_opponent_invalid_team(self):
        """Test getting opponent for team not in fixture raises error."""
        fixture = make_fixture(home_team_id=1, away_team_id=2)
        with pytest.raises(ValueError, match="Team 99 not in fixture"):
            fixture.get_opponent_id(99)

    def test_fixture_get_goal_scorers(self):
        """Test extracting goal scorers from stats."""
        fixture = make_fixture(
            finished=True,
            stats=[
                {"identifier": "goals_scored", "h": [{"element": 10, "value": 2}], "a": [{"element": 20, "value": 1}]},
            ]
        )
        scorers = fixture.get_goal_scorers()
        assert len(scorers) == 2
        assert {"element": 10, "value": 2} in scorers
        assert {"element": 20, "value": 1} in scorers

    def test_fixture_get_assists(self):
        """Test extracting assists from stats."""
        fixture = make_fixture(
            finished=True,
            stats=[
                {"identifier": "assists", "h": [{"element": 11, "value": 1}], "a": []},
            ]
        )
        assists = fixture.get_assists()
        assert len(assists) == 1
        assert assists[0] == {"element": 11, "value": 1}

    def test_fixture_get_bonus(self):
        """Test extracting bonus points (sorted descending)."""
        fixture = make_fixture(
            finished=True,
            stats=[
                {"identifier": "bonus", "h": [{"element": 10, "value": 2}], "a": [{"element": 20, "value": 3}, {"element": 21, "value": 1}]},
            ]
        )
        bonus = fixture.get_bonus()
        assert len(bonus) == 3
        assert bonus[0]["value"] == 3  # Highest first
        assert bonus[1]["value"] == 2
        assert bonus[2]["value"] == 1

    def test_fixture_get_red_cards(self):
        """Test extracting red cards from stats."""
        fixture = make_fixture(
            finished=True,
            stats=[
                {"identifier": "red_cards", "h": [{"element": 15, "value": 1}], "a": []},
            ]
        )
        red_cards = fixture.get_red_cards()
        assert len(red_cards) == 1
        assert red_cards[0] == {"element": 15, "value": 1}

    def test_fixture_get_red_cards_multiple(self):
        """Test extracting multiple red cards from home and away."""
        fixture = make_fixture(
            finished=True,
            stats=[
                {"identifier": "red_cards", "h": [{"element": 10, "value": 1}], "a": [{"element": 20, "value": 1}]},
            ]
        )
        red_cards = fixture.get_red_cards()
        assert len(red_cards) == 2
        assert {"element": 10, "value": 1} in red_cards
        assert {"element": 20, "value": 1} in red_cards

    def test_fixture_get_own_goals(self):
        """Test extracting own goals from stats."""
        fixture = make_fixture(
            finished=True,
            stats=[
                {"identifier": "own_goals", "h": [], "a": [{"element": 25, "value": 1}]},
            ]
        )
        own_goals = fixture.get_own_goals()
        assert len(own_goals) == 1
        assert own_goals[0] == {"element": 25, "value": 1}

    def test_fixture_empty_stats(self):
        """Test fixture with no stats."""
        fixture = make_fixture(stats=[])
        assert fixture.get_goal_scorers() == []
        assert fixture.get_assists() == []
        assert fixture.get_bonus() == []
        assert fixture.get_red_cards() == []
        assert fixture.get_own_goals() == []




