"""Tests for the Stan benchmark wrapper."""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def stan_wrapper(monkeypatch):
    fake_stan = types.ModuleType("strands_stan")
    fake_stan.harness_agent = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "strands_stan", fake_stan)

    wrapper_path = Path(__file__).parents[4] / "strands-infra-runner" / "agents" / "stan" / "agent.py"
    spec = importlib.util.spec_from_file_location("benchmark_stan_wrapper", wrapper_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_mcp_tools_register_all_harbor_transports(stan_wrapper, monkeypatch):
    servers = [
        {
            "name": "local",
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
            "url": None,
        },
        {
            "name": "events",
            "transport": "sse",
            "command": None,
            "args": [],
            "url": "http://events/sse",
        },
        {
            "name": "tau3",
            "transport": "streamable-http",
            "command": None,
            "args": [],
            "url": "http://tau3/mcp",
        },
    ]
    monkeypatch.setenv("HARBOR_MCP_SERVERS", json.dumps(servers))

    clients = stan_wrapper._mcp_tools()

    assert len(clients) == 3
    assert all(client._transport_callable is not None for client in clients)


def test_create_agent_passes_mcp_tools_and_task_root(stan_wrapper, monkeypatch, tmp_path):
    monkeypatch.setenv("HARBOR_TASK_WORKDIR", str(tmp_path))
    monkeypatch.delenv("HARBOR_MCP_SERVERS", raising=False)

    agent_kwargs = stan_wrapper.MyAgent().create_agent()

    assert agent_kwargs["tools"] == []
    assert str(tmp_path) in agent_kwargs["instructions"]
