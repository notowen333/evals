"""Unit tests for StrandsInstalledPyAgent (install, run, populate_context_post_run)."""

import json

import pytest
from harbor.models.agent.context import AgentContext

from strands_evals.benchmarks.harbor.installed.py.agent import (
    _RUNNER_CONTAINER_PATH,
    StrandsInstalledPyAgent,
)

from .conftest import FakeEnvironment, FakeExecResult


def _make_agent(tmp_path, **kwargs):
    return StrandsInstalledPyAgent(logs_dir=tmp_path, **kwargs)


def _install_env():
    """Fake env that fails the 'already installed?' check then succeeds for all else."""
    env = FakeEnvironment(FakeExecResult(return_code=0))
    original_exec = env.exec

    async def _exec(command, **kwargs):
        # The install check looks for "import strands" — fail it so install proceeds
        if "import strands" in command:
            env.exec_calls.append({"command": command, **kwargs})
            return FakeExecResult(return_code=1)
        return await original_exec(command, **kwargs)

    env.exec = _exec
    return env


async def test_install_uploads_runner_and_agent_file(tmp_path):
    env = _install_env()
    agent_file = tmp_path / "my_agent.py"
    agent_file.write_text("agent = None")

    agent = _make_agent(tmp_path, agent_path=str(agent_file))
    await agent.install(env)

    commands = [c["command"] for c in env.exec_calls]
    assert any("curl" in c for c in commands)
    assert any("uv" in c for c in commands)
    assert any("strands-agents" in c for c in commands)
    assert any(u["target_path"] == _RUNNER_CONTAINER_PATH for u in env.uploads)
    assert any("my_agent.py" in u["target_path"] for u in env.uploads)


async def test_install_includes_extra_deps(tmp_path):
    env = _install_env()
    agent = _make_agent(tmp_path, agent_deps="strands-agents-tools requests")
    await agent.install(env)

    commands = [c["command"] for c in env.exec_calls]
    pip_cmd = next(c for c in commands if "uv pip install" in c)
    assert "strands-agents-tools" in pip_cmd
    assert "requests" in pip_cmd


async def test_run_execs_runner_with_instruction(tmp_path):
    env = FakeEnvironment(FakeExecResult(return_code=0))
    agent = _make_agent(tmp_path, agent_module="my_agent:agent", model_name="us.anthropic.claude-sonnet-4-6")
    agent._extra_env = {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "secret", "AWS_REGION": "us-east-1"}

    await agent.run("fix the bug", env, AgentContext())

    commands = [c["command"] for c in env.exec_calls]
    run_cmd = next(c for c in commands if "runner.py" in c)
    assert "--agent" in run_cmd
    assert "my_agent:agent" in run_cmd


async def test_run_forwards_aws_creds(tmp_path):
    captured_env = {}

    async def _exec(command, cwd=None, env=None, timeout_sec=None, user=None):
        if env and "AWS_ACCESS_KEY_ID" in (env or {}):
            captured_env.update(env)
        return FakeExecResult(return_code=0)

    env = FakeEnvironment()
    env.exec = _exec

    agent = _make_agent(tmp_path, agent_module="a:a")
    agent._extra_env = {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sec"}
    await agent.run("go", env, AgentContext())

    assert captured_env.get("AWS_ACCESS_KEY_ID") == "AKIA"


async def test_run_raises_on_nonzero_exit(tmp_path):
    env = FakeEnvironment(FakeExecResult(stdout="error", stderr="fail", return_code=1))
    agent = _make_agent(tmp_path, agent_module="a:a")

    with pytest.raises(RuntimeError):
        await agent.run("go", env, AgentContext())


def test_populate_context_reads_result_json(tmp_path):
    result_data = {
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_tokens": 20,
        "stop_reason": "end_turn",
        "cycle_count": 3,
        "accumulated_usage": {"inputTokens": 500, "outputTokens": 100},
    }
    (tmp_path / "result.json").write_text(json.dumps(result_data))

    agent = _make_agent(tmp_path, model_name="bedrock/model")
    context = AgentContext()
    agent.populate_context_post_run(context)

    assert context.n_input_tokens == 500
    assert context.n_output_tokens == 100
    assert context.n_cache_tokens == 20
    assert context.metadata["stop_reason"] == "end_turn"
    assert context.metadata["cycle_count"] == 3


def test_populate_context_handles_missing_file(tmp_path):
    agent = _make_agent(tmp_path)
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.n_input_tokens is None


def test_populate_context_handles_error_result(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"error": "import failed", "stop_reason": "error"}))

    agent = _make_agent(tmp_path)
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.metadata["stop_reason"] == "error"


def test_name():
    assert StrandsInstalledPyAgent.name() == "strands-installed"


def test_version_command(tmp_path):
    agent = _make_agent(tmp_path)
    assert "strands" in agent.get_version_command()
