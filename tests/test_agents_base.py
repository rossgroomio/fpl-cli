"""Tests for base agent class."""

from datetime import datetime
from typing import Any

import pytest

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus


class ConcreteAgent(Agent):
    """Concrete implementation of Agent for testing."""

    name = "TestAgent"
    description = "A test agent"

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Simple run implementation for testing."""
        if context and context.get("fail"):
            return self._create_result(
                AgentStatus.FAILED,
                message="Test failure",
                errors=["Intentional failure"],
            )
        return self._create_result(
            AgentStatus.SUCCESS,
            data={"test": "data"},
            message="Test success",
        )


class TestAgentStatus:
    """Tests for AgentStatus enum."""

    def test_status_values(self):
        """Test all status enum values."""
        assert AgentStatus.SUCCESS.value == "success"
        assert AgentStatus.PARTIAL.value == "partial"
        assert AgentStatus.FAILED.value == "failed"
        assert AgentStatus.PENDING_APPROVAL.value == "pending_approval"


class TestAgentResult:
    """Tests for AgentResult dataclass."""

    def test_result_creation(self):
        """Test creating an agent result."""
        result = AgentResult(
            agent_name="TestAgent",
            status=AgentStatus.SUCCESS,
            data={"key": "value"},
            message="Test message",
        )
        assert result.agent_name == "TestAgent"
        assert result.status == AgentStatus.SUCCESS
        assert result.data == {"key": "value"}
        assert result.message == "Test message"

    def test_result_default_values(self):
        """Test agent result default values."""
        result = AgentResult(
            agent_name="TestAgent",
            status=AgentStatus.SUCCESS,
        )
        assert result.data == {}
        assert result.message == ""
        assert result.errors == []
        assert result.requires_approval is False
        assert result.pending_actions == []
        assert isinstance(result.timestamp, datetime)

    def test_result_success_property_true(self):
        """Test success property for successful statuses."""
        success_result = AgentResult(
            agent_name="Test",
            status=AgentStatus.SUCCESS,
        )
        assert success_result.success is True

        partial_result = AgentResult(
            agent_name="Test",
            status=AgentStatus.PARTIAL,
        )
        assert partial_result.success is True

    def test_result_success_property_false(self):
        """Test success property for non-successful statuses."""
        failed_result = AgentResult(
            agent_name="Test",
            status=AgentStatus.FAILED,
        )
        assert failed_result.success is False

        pending_result = AgentResult(
            agent_name="Test",
            status=AgentStatus.PENDING_APPROVAL,
        )
        assert pending_result.success is False

    def test_result_with_errors(self):
        """Test result with error list."""
        result = AgentResult(
            agent_name="Test",
            status=AgentStatus.FAILED,
            errors=["Error 1", "Error 2"],
        )
        assert len(result.errors) == 2
        assert "Error 1" in result.errors

    def test_result_with_pending_actions(self):
        """Test result with pending actions."""
        actions = [
            {"action": "transfer", "player_id": 100},
            {"action": "captain", "player_id": 200},
        ]
        result = AgentResult(
            agent_name="Test",
            status=AgentStatus.PENDING_APPROVAL,
            requires_approval=True,
            pending_actions=actions,
        )
        assert result.requires_approval is True
        assert len(result.pending_actions) == 2


class TestAgent:
    """Tests for Agent base class."""

    @pytest.fixture
    def agent(self):
        """Create a test agent instance."""
        return ConcreteAgent()

    @pytest.fixture
    def configured_agent(self):
        """Create an agent with config."""
        return ConcreteAgent(config={"setting": "value"})

    def test_agent_initialization(self, agent):
        """Test agent initialization."""
        assert agent.name == "TestAgent"
        assert agent.description == "A test agent"
        assert agent.config == {}
        assert agent._last_run is None
        assert agent._last_result is None

    def test_agent_with_config(self, configured_agent):
        """Test agent initialization with config."""
        assert configured_agent.config == {"setting": "value"}

    @pytest.mark.asyncio
    async def test_agent_run_success(self, agent):
        """Test successful agent run."""
        result = await agent.run()

        assert result.status == AgentStatus.SUCCESS
        assert result.agent_name == "TestAgent"
        assert result.data == {"test": "data"}
        assert result.message == "Test success"

    @pytest.mark.asyncio
    async def test_agent_run_failure(self, agent):
        """Test failed agent run."""
        result = await agent.run(context={"fail": True})

        assert result.status == AgentStatus.FAILED
        assert result.message == "Test failure"
        assert "Intentional failure" in result.errors

    @pytest.mark.asyncio
    async def test_agent_run_updates_last_run(self, agent):
        """Test that run updates last_run timestamp."""
        assert agent.last_run is None

        await agent.run()

        assert agent.last_run is not None
        assert isinstance(agent.last_run, datetime)

    @pytest.mark.asyncio
    async def test_agent_run_updates_last_result(self, agent):
        """Test that run updates last_result."""
        assert agent.last_result is None

        result = await agent.run()

        assert agent.last_result is not None
        assert agent.last_result == result

    @pytest.mark.asyncio
    async def test_agent_validate_default(self, agent):
        """Test default validate returns True."""
        is_valid = await agent.validate()
        assert is_valid is True

    def test_create_result_helper(self, agent):
        """Test _create_result helper method."""
        result = agent._create_result(
            status=AgentStatus.SUCCESS,
            data={"key": "value"},
            message="Created via helper",
        )

        assert result.agent_name == "TestAgent"
        assert result.status == AgentStatus.SUCCESS
        assert result.data == {"key": "value"}
        assert result.message == "Created via helper"

    def test_create_result_with_errors(self, agent):
        """Test _create_result with errors."""
        result = agent._create_result(
            status=AgentStatus.FAILED,
            errors=["Error 1", "Error 2"],
        )

        assert result.status == AgentStatus.FAILED
        assert len(result.errors) == 2

    def test_create_result_with_pending_approval(self, agent):
        """Test _create_result with pending approval."""
        result = agent._create_result(
            status=AgentStatus.PENDING_APPROVAL,
            requires_approval=True,
            pending_actions=[{"action": "test"}],
        )

        assert result.status == AgentStatus.PENDING_APPROVAL
        assert result.requires_approval is True
        assert len(result.pending_actions) == 1


class TestAgentLogging:
    """Tests for agent logging methods."""

    @pytest.fixture
    def agent(self):
        """Create a test agent."""
        return ConcreteAgent()

    def test_log_method(self, agent, capsys):
        """Test basic log method."""
        agent.log("Test message")
        # Note: Rich console output goes to stdout
        # We're mainly testing that it doesn't raise

    def test_log_with_style(self, agent):
        """Test log with style parameter."""
        # Should not raise
        agent.log("Styled message", style="bold")

    def test_log_success(self, agent):
        """Test log_success helper."""
        # Should not raise
        agent.log_success("Success message")

    def test_log_warning(self, agent):
        """Test log_warning helper."""
        # Should not raise
        agent.log_warning("Warning message")

    def test_log_error(self, agent):
        """Test log_error helper."""
        # Should not raise
        agent.log_error("Error message")


class TestAgentInheritance:
    """Tests for agent inheritance patterns."""

    def test_must_implement_run(self):
        """Test that run must be implemented."""
        # This should raise TypeError when instantiated
        class IncompleteAgent(Agent):
            name = "Incomplete"
            description = "Missing run"

        with pytest.raises(TypeError):
            IncompleteAgent()

    def test_custom_validate(self):
        """Test custom validate implementation."""
        class ValidatingAgent(Agent):
            name = "ValidatingAgent"
            description = "Agent with custom validation"

            async def run(self, context=None):
                return self._create_result(AgentStatus.SUCCESS)

            async def validate(self):
                # Custom validation that fails
                return self.config.get("is_valid", False)

        invalid_agent = ValidatingAgent(config={"is_valid": False})
        valid_agent = ValidatingAgent(config={"is_valid": True})

        import asyncio
        assert asyncio.run(invalid_agent.validate()) is False
        assert asyncio.run(valid_agent.validate()) is True

    def test_agent_name_and_description_override(self):
        """Test that subclasses must set name and description."""
        class NamedAgent(Agent):
            name = "CustomName"
            description = "Custom description"

            async def run(self, context=None):
                return self._create_result(AgentStatus.SUCCESS)

        agent = NamedAgent()
        assert agent.name == "CustomName"
        assert agent.description == "Custom description"
