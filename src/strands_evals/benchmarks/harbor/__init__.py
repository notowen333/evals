"""Harbor adapters — run a Strands agent inside Harbor (Terminal-Bench 2.0).

Two approaches:

**Installed (transparent)** — user's agent runs unchanged inside the container::

    harbor run --agent strands_evals.benchmarks.harbor.installed:StrandsInstalledAgent \\
        --ak agent_module=my_agent:agent --ak agent_path=./my_agent.py

**External (explicit)** — LLM on host, tools route to container via build()::

    class MyAgent(StrandsAgent):
        def build(self, *, model_name, logs_dir):
            return Agent(model=..., tools=TOOLS, callback_handler=None)
"""

from .external import TOOLS, StrandsAgent, bash, read_file, submit, write_file
from .installed import StrandsInstalledAgent

__all__ = ["StrandsAgent", "StrandsInstalledAgent", "TOOLS", "bash", "read_file", "write_file", "submit"]
