"""Harbor adapter — run a Strands agent inside Harbor (Terminal-Bench 2.0).

Define a class with ``create_agent()`` in your agent directory::

    class MyAgent:
        def create_agent(self):
            return Agent(model=..., tools=[...], callback_handler=None)

Then run::

    strands-evals benchmark ./my_agent/ --dataset ...

Optional methods (duck-typed, no import needed):
    - invoke_agent(agent, instruction, **kwargs) — custom invocation
    - on_benchmark_complete(job_dir, results) — post-run callback
"""

from .benchmark_agent import BenchmarkAgent
from .installed import StrandsInstalledAgent

__all__ = ["BenchmarkAgent", "StrandsInstalledAgent"]
