"""Base class for benchmark agents — the user-facing interface.

Subclass ``BenchmarkAgent`` and implement ``create_agent()``. Optionally override
``invoke()`` for custom invocation logic and ``on_complete()`` for post-run hooks.

Example::

    from strands import Agent
    from strands.models.bedrock import BedrockModel
    from strands_tools import shell, file_read, file_write
    from strands_evals.benchmarks.harbor import BenchmarkAgent

    class MyAgent(BenchmarkAgent):
        def create_agent(self):
            return Agent(
                model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-6"),
                tools=[shell, file_read, file_write],
                callback_handler=None,
            )
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from strands import Agent
from strands.agent.agent_result import AgentResult


class BenchmarkAgent(ABC):
    """Interface for agents that run inside Harbor benchmarks.

    Required:
        create_agent() — return a fresh Strands Agent for a trial.

    Optional overrides:
        invoke() — customize how the agent is called (default: agent(instruction)).
        on_complete() — callback after the benchmark job finishes (default: no-op).
    """

    @abstractmethod
    def create_agent(self) -> Agent:
        """Return a configured Strands Agent. Called once per trial."""
        ...

    def invoke(self, agent: Agent, instruction: str, **kwargs: Any) -> AgentResult:
        """Invoke the agent with the task instruction. Override for custom logic."""
        return agent(instruction, **kwargs)

    def on_complete(self, job_dir: Path, results: dict[str, Any]) -> None:  # noqa: B027
        """Called after the benchmark run completes. Override for uploads, notifications, etc."""
