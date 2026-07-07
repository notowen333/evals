"""Unit tests for the Strands tools."""

import pytest
from strands import ToolContext

from strands_evals.benchmarks.harbor.external.tools import ENVIRONMENT_KEY, bash, read_file, submit, write_file

from .conftest import FakeEnvironment, FakeExecResult, tool_context


async def _call(tool, environment, **inputs):
    return await tool._tool_func(**inputs, tool_context=tool_context(environment))


async def test_bash_returns_combined_output():
    env = FakeEnvironment(FakeExecResult(stdout="out\n", stderr="err\n", return_code=0))
    result = await _call(bash, env, command="echo hi")
    assert result == "out\nerr"
    assert env.exec_calls[0]["command"] == "echo hi"
    assert env.exec_calls[0]["timeout_sec"] == 180


async def test_bash_reports_exit_code_when_no_output():
    env = FakeEnvironment(FakeExecResult(stdout="", stderr="", return_code=3))
    result = await _call(bash, env, command="false")
    assert result == "(exit code 3)"


async def test_read_file_success():
    env = FakeEnvironment(FakeExecResult(stdout="file contents", return_code=0))
    result = await _call(read_file, env, path="/app/x.py")
    assert result == "file contents"
    assert env.exec_calls[0]["command"] == "cat /app/x.py"


async def test_read_file_error():
    env = FakeEnvironment(FakeExecResult(stderr="No such file", return_code=1))
    result = await _call(read_file, env, path="/nope")
    assert "exit 1" in result
    assert "No such file" in result


async def test_write_file_uploads_content():
    env = FakeEnvironment(FakeExecResult(return_code=0))
    result = await _call(write_file, env, path="/app/pkg/new.py", content="print('x')\n")
    assert result == "ok"
    assert env.exec_calls[0]["command"] == "mkdir -p /app/pkg"
    assert env.uploads[0]["target_path"] == "/app/pkg/new.py"
    assert env.uploads[0]["content"] == "print('x')\n"


async def test_write_file_mkdir_failure():
    env = FakeEnvironment(FakeExecResult(stderr="denied", return_code=1))
    result = await _call(write_file, env, path="/root/x", content="data")
    assert "Failed to create parent directory" in result
    assert env.uploads == []


async def test_submit_includes_diff_summary():
    env = FakeEnvironment(FakeExecResult(stdout=" file.py | 2 +-", return_code=0))
    result = await _call(submit, env)
    assert "Submitted" in result
    assert "file.py" in result


async def test_tool_raises_without_environment():
    ctx = ToolContext(tool_use={"toolUseId": "t", "name": "bash", "input": {}}, agent=None, invocation_state={})
    with pytest.raises(RuntimeError, match=ENVIRONMENT_KEY):
        await bash._tool_func(command="ls", tool_context=ctx)


def test_tool_context_hidden_from_model_schema():
    assert list(bash.tool_spec["inputSchema"]["json"]["properties"].keys()) == ["command"]
    assert list(write_file.tool_spec["inputSchema"]["json"]["properties"].keys()) == ["path", "content"]
