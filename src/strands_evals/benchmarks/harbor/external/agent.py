"""Strands Agents SDK as a native Harbor external agent.

Subclass and implement ``build()`` to return your Strands ``Agent``::

    class MyAgent(StrandsAgent):
        def build(self, *, model_name, logs_dir):
            return Agent(model=BedrockModel(model_id=model_name), tools=TOOLS, callback_handler=None)

Then: ``harbor run --agent my_module:MyAgent``
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import MCPServerConfig
from strands import Agent
from strands.models.bedrock import DEFAULT_BEDROCK_MODEL_ID, BedrockModel

from ._logging import TrajectoryLogger
from .tools import ENVIRONMENT_KEY, TOOLS

logger = logging.getLogger(__name__)

_DEFAULT_SYSTEM_PROMPT = (
    "You are an expert software engineer working inside a sandboxed Linux container.\n"
    "You have tools: bash, read_file, write_file, and submit.\n"
    "Explore before you edit, verify with tests, and call submit when done."
)


class StrandsAgent(BaseAgent):
    """A Harbor external agent backed by the Strands Agents SDK.

    Subclass and override :meth:`build` to supply your own Strands ``Agent``. Everything
    else (the run loop, environment threading, cancellation, metric mapping) is handled
    here.
    """

    SUPPORTS_ATIF = False
    SUPPORTS_WINDOWS = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        mcp_servers: list[MCPServerConfig] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir, model_name=model_name, mcp_servers=mcp_servers, **kwargs)

    def build(self, *, model_name: str | None, logs_dir: Path) -> Agent:
        """Return the Strands ``Agent`` to run for a trial. Override this.

        Called once per trial. Return a fresh ``Agent`` each time.
        """
        return Agent(
            model=BedrockModel(model_id=model_name or DEFAULT_BEDROCK_MODEL_ID),
            system_prompt=_DEFAULT_SYSTEM_PROMPT,
            tools=TOOLS,
            plugins=[TrajectoryLogger(logs_dir)],
            callback_handler=None,
        )

    @staticmethod
    def name() -> str:
        return "strands"

    def version(self) -> str | None:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        await environment.exec("git config --global --add safe.directory '*'")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        agent = self.build(model_name=self.model_name, logs_dir=self.logs_dir)

        try:
            await agent.invoke_async(
                instruction,
                invocation_state={ENVIRONMENT_KEY: environment},
            )
        except asyncio.CancelledError:
            agent.cancel()
            raise
        finally:
            self._populate_context(context, agent)

    def _populate_context(self, context: AgentContext, agent: Agent) -> None:
        metrics = agent.event_loop_metrics
        usage = metrics.accumulated_usage
        context.n_input_tokens = usage.get("inputTokens")
        context.n_output_tokens = usage.get("outputTokens")
        context.n_cache_tokens = usage.get("cacheReadInputTokens")
        context.cost_usd = None
        context.metadata = {
            "model": self.model_name,
            "cycle_count": metrics.cycle_count,
            "accumulated_usage": dict(usage),
        }
