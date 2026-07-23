"""Stan factory agent for Harbor installed Python adapter."""

import os

from strands import Agent
from strands_stan import harness_agent


# Bedrock providers that support prompt caching
_CACHING_PROVIDERS = frozenset({"anthropic", "us.anthropic", "global.anthropic"})


def _resolve_model():
    model_id = os.environ.get("STRANDS_MODEL")
    if not model_id:
        return None
    if model_id.startswith("openai."):
        mantle_config = {"region": os.environ.get("AWS_REGION", "us-east-1")}
        if "gpt-5" in model_id:
            from strands.models.openai_responses import OpenAIResponsesModel
            return OpenAIResponsesModel(model_id=model_id, bedrock_mantle_config=mantle_config)
        else:
            from strands.models.openai import OpenAIModel
            # Non-GPT models: strip "openai." prefix, mantle expects bare ID
            bare_id = model_id[len("openai."):]
            return OpenAIModel(model_id=bare_id, bedrock_mantle_config=mantle_config)
    return model_id


def _supports_caching(model):
    if model is None:
        return True
    if not isinstance(model, str):
        return False
    provider = model.split(".")[0]
    return provider in _CACHING_PROVIDERS


class MyAgent:
    def create_agent(self) -> Agent:
        task_workdir = os.environ.get("HARBOR_TASK_WORKDIR")
        if task_workdir:
            os.chdir(task_workdir)
        model = _resolve_model()
        caching = _supports_caching(model)
        return harness_agent(model=model, caching=caching)
