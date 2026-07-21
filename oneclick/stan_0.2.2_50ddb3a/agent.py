"""Stan's default factory agent, bound to OneClick's task container."""

import json
import os
from pathlib import Path

from strands import Agent
from strands.sandbox.docker import DockerSandbox
from strands_stan import harness_agent

_MODEL_CONFIG_PATH = Path(__file__).parent / "model_config.json"


def _resolve_model_name(model_name: str | None) -> str | None:
    """Resolve OneClick aliases while leaving model construction to Stan."""
    if not model_name:
        return None
    try:
        config = json.loads(_MODEL_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return model_name
    model_config = config.get(model_name)
    if isinstance(model_config, dict):
        return model_config.get("model_id", model_name)
    return model_name


class MyAgent:
    def create_agent(self) -> Agent:
        sandbox = DockerSandbox(
            container=os.environ["TASK_CONTAINER_ID"],
            working_dir=os.environ.get("CONTAINER_WORKSPACE_PATH") or "/testbed",
        )
        return harness_agent(
            model=_resolve_model_name(os.environ.get("STRANDS_MODEL")),
            sandbox=sandbox,
        )
