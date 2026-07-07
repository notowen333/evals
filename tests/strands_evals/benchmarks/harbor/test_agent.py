"""Unit tests for StrandsAgent (build override, run, metrics, cancellation)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from harbor.models.agent.context import AgentContext

from strands_evals.benchmarks.harbor.external.agent import StrandsAgent

from .conftest import FakeEnvironment, FakeExecResult


def _fake_metrics(input_tokens=100, output_tokens=50, cache_tokens=10, cycles=3):
    usage = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens,
        "cacheReadInputTokens": cache_tokens,
    }
    return SimpleNamespace(accumulated_usage=usage, cycle_count=cycles)


def _mock_strands_agent(metrics=None, invoke_side_effect=None):
    agent = MagicMock()
    agent.invoke_async = AsyncMock(return_value=MagicMock(), side_effect=invoke_side_effect)
    agent.event_loop_metrics = metrics or _fake_metrics()
    return agent


def test_static_metadata():
    assert StrandsAgent.name() == "strands"
    assert StrandsAgent.SUPPORTS_ATIF is False
    assert StrandsAgent.SUPPORTS_WINDOWS is False


def test_version(tmp_path):
    assert StrandsAgent(logs_dir=tmp_path).version() == "0.1.0"


async def test_setup_configures_git(tmp_path):
    env = FakeEnvironment(FakeExecResult(return_code=0))
    await StrandsAgent(logs_dir=tmp_path).setup(env)
    assert env.exec_calls[0]["command"] == "git config --global --add safe.directory '*'"


async def test_build_override_is_used_by_run(tmp_path):
    strands_agent = _mock_strands_agent()
    seen = {}

    class MyAgent(StrandsAgent):
        def build(self, *, model_name, logs_dir):
            seen["model_name"] = model_name
            seen["logs_dir"] = logs_dir
            return strands_agent

    context = AgentContext()
    await MyAgent(logs_dir=tmp_path, model_name="my-model").run("go", FakeEnvironment(), context)

    assert seen == {"model_name": "my-model", "logs_dir": tmp_path}
    strands_agent.invoke_async.assert_awaited_once()
    assert context.n_input_tokens == 100
    assert context.metadata["model"] == "my-model"


async def test_run_passes_environment_via_invocation_state(tmp_path):
    strands_agent = _mock_strands_agent()
    env = FakeEnvironment()

    class MyAgent(StrandsAgent):
        def build(self, **_):
            return strands_agent

    await MyAgent(logs_dir=tmp_path).run("go", env, AgentContext())
    _, kwargs = strands_agent.invoke_async.call_args
    assert kwargs["invocation_state"]["environment"] is env


async def test_cancellation_calls_cancel_and_reraises(tmp_path):
    strands_agent = _mock_strands_agent(
        metrics=_fake_metrics(input_tokens=7, output_tokens=0, cache_tokens=0),
        invoke_side_effect=asyncio.CancelledError(),
    )

    class MyAgent(StrandsAgent):
        def build(self, **_):
            return strands_agent

    context = AgentContext()
    with pytest.raises(asyncio.CancelledError):
        await MyAgent(logs_dir=tmp_path).run("go", FakeEnvironment(), context)

    strands_agent.cancel.assert_called_once()
    assert context.n_input_tokens == 7
