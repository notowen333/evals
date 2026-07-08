"""Harbor adapter — run a Strands agent inside Harbor (Terminal-Bench 2.0).

Subclass ``BenchmarkAgent`` and implement ``create_agent()``::

    from strands_evals.benchmarks.harbor import BenchmarkAgent

    class MyAgent(BenchmarkAgent):
        def create_agent(self):
            return Agent(model=..., tools=[...], callback_handler=None)

Then run::

    strands-evals benchmark ./my_agent/ --dataset ...
"""

from .benchmark_agent import BenchmarkAgent
from .installed import StrandsInstalledAgent

__all__ = ["BenchmarkAgent", "StrandsInstalledAgent"]
