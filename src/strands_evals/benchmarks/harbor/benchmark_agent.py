"""Protocol defining the benchmark agent contract.

This is typing-only — agent files never import this at runtime. The runner uses
duck typing: any class with ``create_agent()`` works. ``invoke_agent()`` and
``on_benchmark_complete()`` are optional.

The implicit contract:
    - create_agent() -> Agent           (required)
    - invoke_agent(agent, instruction, **kwargs) -> AgentResult  (optional)
    - on_benchmark_complete(job_dir, results) -> None            (optional)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from strands import Agent
from strands.agent.agent_result import AgentResult


@runtime_checkable
class BenchmarkAgent(Protocol):
    """Protocol for benchmark agents. For type checking only — never subclass this."""

    def create_agent(self) -> Agent: ...
    def invoke_agent(self, agent: Agent, instruction: str, **kwargs: Any) -> AgentResult: ...
    def on_benchmark_complete(self, job_dir: Path, results: dict[str, Any]) -> None: ...
