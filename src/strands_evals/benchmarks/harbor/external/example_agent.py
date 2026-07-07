"""Reference agent — copy this file, edit build(), and run it with Harbor.

    harbor run --agent strands_evals.benchmarks.harbor.external.example_agent:ExampleAgent \\
        --model global.anthropic.claude-sonnet-4-6
"""

from pathlib import Path

from strands import Agent
from strands.models.bedrock import BedrockModel

from strands_evals.benchmarks.harbor import TOOLS, StrandsAgent


class ExampleAgent(StrandsAgent):
    """A minimal Strands agent for Terminal-Bench tasks."""

    def build(self, *, model_name: str | None, logs_dir: Path) -> Agent:
        return Agent(
            model=BedrockModel(model_id=model_name or "global.anthropic.claude-sonnet-4-6"),
            system_prompt=(
                "You are an expert software engineer working in a sandboxed Linux container. "
                "Use the tools to read files, run commands, make changes, and verify them. "
                "Call submit when the task is complete."
            ),
            tools=TOOLS,
            callback_handler=None,
        )
