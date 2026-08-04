"""Stan factory agent for Harbor installed Python adapter."""

import json
import os
from functools import partial

from strands import Agent
from strands_stan import harness_agent

# Bedrock providers that support prompt caching
_CACHING_PROVIDERS = frozenset({"anthropic", "us.anthropic", "global.anthropic"})


def _task_instructions(task_workdir: str | None) -> str | None:
    if not task_workdir:
        return None
    return (
        f"You are working inside a benchmark task container. The task root is {task_workdir}. "
        "Run `pwd` before acting, resolve task files relative to that root, and use absolute paths "
        "when writing or editing files."
    )


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
            bare_id = model_id[len("openai.") :]
            return OpenAIModel(model_id=bare_id, bedrock_mantle_config=mantle_config)
    return model_id


def _supports_caching(model):
    if model is None:
        return True
    if not isinstance(model, str):
        return False
    provider = model.split(".")[0]
    return provider in _CACHING_PROVIDERS


def _mcp_tools() -> list:
    raw_servers = os.environ.get("HARBOR_MCP_SERVERS")
    if not raw_servers:
        return []

    from mcp import StdioServerParameters, stdio_client
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp import MCPClient

    clients = []
    for server in json.loads(raw_servers):
        transport = server["transport"]
        if transport == "stdio":
            parameters = StdioServerParameters(
                command=server["command"],
                args=server.get("args", []),
            )
            transport_factory = partial(stdio_client, parameters)
        elif transport == "sse":
            url = server["url"]
            transport_factory = partial(sse_client, url)
        elif transport == "streamable-http":
            url = server["url"]
            transport_factory = partial(streamablehttp_client, url=url)
        else:
            raise ValueError(f"Unsupported MCP transport: {transport}")
        clients.append(MCPClient(transport_factory))
    return clients


class MyAgent:
    def create_agent(self) -> Agent:
        task_workdir = os.environ.get("HARBOR_TASK_WORKDIR")
        if task_workdir:
            os.chdir(task_workdir)
        model = _resolve_model()
        caching = _supports_caching(model)
        return harness_agent(
            model=model,
            caching=caching,
            instructions=_task_instructions(task_workdir),
            tools=_mcp_tools(),
        )
