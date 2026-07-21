"""Stan's default factory agent for Harbor's installed Python adapter."""

import os

from strands import Agent
from strands_stan import harness_agent


class MyAgent:
    def create_agent(self) -> Agent:
        task_workdir = os.environ.get("HARBOR_TASK_WORKDIR")
        if task_workdir:
            os.chdir(task_workdir)
        return harness_agent(model=os.environ.get("STRANDS_MODEL") or None)
