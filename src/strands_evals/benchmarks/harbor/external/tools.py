"""Strands tools that execute inside a Harbor environment."""

import logging
import os
import shlex
import tempfile

from harbor.environments.base import BaseEnvironment
from strands import ToolContext, tool

logger = logging.getLogger(__name__)

ENVIRONMENT_KEY = "environment"


def _environment(tool_context: ToolContext) -> BaseEnvironment:
    env = tool_context.invocation_state.get(ENVIRONMENT_KEY)
    if env is None:
        raise RuntimeError(
            f"key=<{ENVIRONMENT_KEY}> | no Harbor environment in invocation_state; "
            "the agent must pass it via invoke_async(invocation_state=...)"
        )
    return env


@tool(context=True)
async def bash(command: str, tool_context: ToolContext) -> str:
    """Execute a bash command in the sandboxed task container.

    Args:
        command: The bash command to run.

    Returns:
        Combined stdout and stderr, or an exit-code message when there is no output.
    """
    environment = _environment(tool_context)
    logger.debug("command=<%s> | running bash", command)
    result = await environment.exec(command, timeout_sec=180)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    return output if output else f"(exit code {result.return_code})"


@tool(context=True)
async def read_file(path: str, tool_context: ToolContext) -> str:
    """Read the contents of a file in the task container.

    Args:
        path: Absolute path to the file.

    Returns:
        The file contents, or an error message if the read fails.
    """
    environment = _environment(tool_context)
    result = await environment.exec(f"cat {shlex.quote(path)}", timeout_sec=30)
    if result.return_code != 0:
        return f"Error reading {path} (exit {result.return_code}): {result.stderr or ''}".strip()
    return result.stdout or ""


@tool(context=True)
async def write_file(path: str, content: str, tool_context: ToolContext) -> str:
    """Write content to a file in the task container, creating parent directories.

    Args:
        path: Absolute path to the destination file.
        content: The content to write.

    Returns:
        'ok' on success, or an error message.
    """
    environment = _environment(tool_context)
    parent = os.path.dirname(path)
    if parent:
        result = await environment.exec(f"mkdir -p {shlex.quote(parent)}", timeout_sec=30)
        if result.return_code != 0:
            return f"Failed to create parent directory (exit {result.return_code}): {result.stderr or ''}".strip()

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        await environment.upload_file(source_path=tmp_path, target_path=path)
    finally:
        if tmp_path is not None:
            os.unlink(tmp_path)
    return "ok"


@tool(context=True)
async def submit(tool_context: ToolContext) -> str:
    """Signal that the task is complete.

    Returns:
        A confirmation message including a git diff summary when available.
    """
    environment = _environment(tool_context)
    result = await environment.exec("git diff --stat HEAD", timeout_sec=30)
    summary = (result.stdout or result.stderr or "").strip() or "(no diff output)"
    return f"Submitted. Git diff summary:\n{summary}"


TOOLS = [bash, read_file, write_file, submit]
