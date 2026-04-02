"""Base agent class that all FPL agents inherit from."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from rich.console import Console

console = Console()


class AgentStatus(Enum):
    """Status of an agent run."""

    SUCCESS = "success"
    PARTIAL = "partial"  # Some data retrieved but incomplete
    FAILED = "failed"
    PENDING_APPROVAL = "pending_approval"


@dataclass
class AgentResult:
    """Result from an agent execution."""

    agent_name: str
    status: AgentStatus
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    errors: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    requires_approval: bool = False
    pending_actions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Check if the agent run was successful."""
        return self.status in (AgentStatus.SUCCESS, AgentStatus.PARTIAL)


class Agent(ABC):
    """Base class for all FPL agents.

    Agents are responsible for specific tasks in the FPL automation pipeline.
    They can collect data, perform analysis, or take actions.
    """

    name: str = "BaseAgent"
    description: str = "Base agent class"

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize the agent with optional configuration.

        Args:
            config: Configuration dictionary for the agent.
        """
        self.config = config or {}
        self._last_run: datetime | None = None
        self._last_result: AgentResult | None = None

    async def close(self) -> None:
        """Close resources held by this agent. Override in subclasses."""
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    @abstractmethod
    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Execute the agent's main task.

        Args:
            context: Optional context data from previous agents in the pipeline.

        Returns:
            AgentResult containing the outcome of the agent's execution.
        """
        pass

    def log(self, message: str, style: str = "") -> None:
        """Log a message to the console.

        Args:
            message: Message to log.
            style: Rich style string for formatting.
        """
        prefix = f"[bold blue][{self.name}][/bold blue]"
        if style:
            console.print(f"{prefix} [{style}]{message}[/{style}]")
        else:
            console.print(f"{prefix} {message}")

    def log_success(self, message: str) -> None:
        """Log a success message."""
        self.log(message, "green")

    def log_warning(self, message: str) -> None:
        """Log a warning message."""
        self.log(message, "yellow")

    def log_error(self, message: str) -> None:
        """Log an error message."""
        self.log(message, "red bold")

    async def validate(self) -> bool:
        """Validate that the agent is properly configured.

        Override in subclasses to add specific validation logic.

        Returns:
            True if the agent is properly configured.
        """
        return True

    @property
    def last_run(self) -> datetime | None:
        """Get the timestamp of the last run."""
        return self._last_run

    @property
    def last_result(self) -> AgentResult | None:
        """Get the result of the last run."""
        return self._last_result

    def _create_result(
        self,
        status: AgentStatus,
        data: dict[str, Any] | None = None,
        message: str = "",
        errors: list[str] | None = None,
        requires_approval: bool = False,
        pending_actions: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        """Create an AgentResult with common fields populated.

        Args:
            status: Status of the agent run.
            data: Data collected or produced by the agent.
            message: Human-readable message about the result.
            errors: List of error messages if any.
            requires_approval: Whether user approval is needed.
            pending_actions: List of actions awaiting approval.

        Returns:
            Populated AgentResult.
        """
        result = AgentResult(
            agent_name=self.name,
            status=status,
            data=data or {},
            message=message,
            errors=errors or [],
            requires_approval=requires_approval,
            pending_actions=pending_actions or [],
        )
        self._last_run = result.timestamp
        self._last_result = result
        return result
