"""Shared test fixtures for the Harbor adapter."""

from strands import ToolContext


class FakeExecResult:
    def __init__(self, stdout: str = "", stderr: str = "", return_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code


class FakeEnvironment:
    def __init__(self, result: FakeExecResult | None = None) -> None:
        self._result = result or FakeExecResult(stdout="ok")
        self.exec_calls: list[dict] = []
        self.uploads: list[dict] = []
        self.default_user: str | None = "agent"

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.exec_calls.append({"command": command, "cwd": cwd, "env": env, "timeout_sec": timeout_sec, "user": user})
        return self._result

    async def upload_file(self, source_path, target_path):
        with open(source_path, encoding="utf-8") as handle:
            content = handle.read()
        self.uploads.append({"source_path": source_path, "target_path": target_path, "content": content})


def tool_context(environment) -> ToolContext:
    return ToolContext(
        tool_use={"toolUseId": "test", "name": "test", "input": {}},
        agent=None,
        invocation_state={"environment": environment},
    )
