"""Stan's default factory agent for Harbor's installed Python adapter."""

import os

from strands import Agent
from strands_stan import harness_agent


def _resolve_model():
    model_id = os.environ.get("STRANDS_MODEL")
    if not model_id:
        return None
    if model_id.startswith("openai."):
        from strands.models.openai import OpenAIModel
        return OpenAIModel(
            model_id=model_id,
            bedrock_mantle_config={"region": os.environ.get("AWS_REGION", "us-east-1")},
        )
    return model_id


class MyAgent:
    def create_agent(self) -> Agent:
        task_workdir = os.environ.get("HARBOR_TASK_WORKDIR")
        if task_workdir:
            os.chdir(task_workdir)
        return harness_agent(model=_resolve_model())
