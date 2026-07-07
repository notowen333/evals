"""Harbor adapter — run a Strands agent inside Harbor (Terminal-Bench 2.0).

Run any Strands agent file unchanged inside a Harbor container::

    strands-evals benchmark ./my_agent.py -p <task>

Or directly via harbor::

    harbor run --agent strands_evals.benchmarks.harbor.installed:StrandsInstalledAgent \\
        --ak agent_module=my_agent:agent --ak agent_path=./my_agent.py -p <task>
"""

from .installed import StrandsInstalledAgent

__all__ = ["StrandsInstalledAgent"]
