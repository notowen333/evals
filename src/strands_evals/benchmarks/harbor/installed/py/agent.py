"""Installed Strands Python agent — runs the user's agent inside the Harbor container.

Usage::

    harbor run \\
      --agent strands_evals.benchmarks.harbor.installed:StrandsInstalledPyAgent \\
      --model us.anthropic.claude-sonnet-4-6 \\
      --ak agent_module=my_agent:agent \\
      --ak agent_path=./my_agent.py \\
      -p <task>
"""

import shlex
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import EnvVar
from harbor.environments.base import BaseEnvironment
from harbor.models.trial.paths import EnvironmentPaths

from ..base_strands import BaseStrandsInstalledAgent, _AGENT_INSTALL_DIR, _RESULT_PATH, _LOG_PATH

_RUNNER_CONTAINER_PATH = "/installed-agent/runner.py"
_VENV_PATH = "/installed-agent/venv"
_VENV_PYTHON = f"{_VENV_PATH}/bin/python3"


class StrandsInstalledPyAgent(BaseStrandsInstalledAgent):
    """Run any Strands Python agent inside the Harbor container."""

    ENV_VARS = [
        *BaseStrandsInstalledAgent.ENV_VARS,
        EnvVar(
            "bypass_tool_consent",
            env="BYPASS_TOOL_CONSENT",
            type="bool",
            default=True,
            bool_true="true",
            bool_false="false",
        ),
    ]

    def __init__(
        self,
        logs_dir: Path,
        agent_module: str = "agent:agent",
        agent_path: str | None = None,
        agent_deps: str = "strands-agents-tools",
        strands_version: str = ">=1.45.0",
        unpublished_strands_ref: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir, **kwargs)
        self._agent_module = agent_module
        self._agent_path = Path(agent_path) if agent_path else None
        self._agent_deps = agent_deps or ""
        self._strands_version = strands_version
        self._unpublished_strands_ref = unpublished_strands_ref

    @staticmethod
    @override
    def name() -> str:
        return "strands-installed"

    @override
    def get_version_command(self) -> str | None:
        return f'{_VENV_PYTHON} -c "import strands; print(strands.__version__)"'

    @override
    def _build_run_command(self, cli_flags: str | None) -> list[str]:
        agent_module = shlex.quote(self._agent_module)
        parts = [
            f"cd {_AGENT_INSTALL_DIR} &&",
            f"{_VENV_PYTHON} {_RUNNER_CONTAINER_PATH}",
            f"--agent {agent_module}",
            '--instruction "$HARBOR_INSTRUCTION"',
            f"--output {_RESULT_PATH}",
        ]
        if cli_flags:
            parts.append(cli_flags)
        parts.append(f"2>&1 | tee {_LOG_PATH}")
        return parts

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # Fast-path: skip install entirely if venv already has strands (idempotent on retries)
        check_result = await environment.exec(
            command=f'[ -f {_VENV_PYTHON} ] && {_VENV_PYTHON} -c "import strands" 2>/dev/null',
        )
        if check_result.return_code == 0:
            self.logger.debug("Strands venv already installed, skipping")
        else:
            # Ensure curl + git are present (curl for uv installer, git for --unpublished-strands-ref)
            await self.exec_as_root(
                environment,
                command=(
                    "(command -v curl >/dev/null && command -v git >/dev/null) || "
                    "(apt-get update -qq && apt-get install -y -qq curl git)"
                ),
                env={"DEBIAN_FRONTEND": "noninteractive"},
            )

            # Create venv dir writable by the non-root agent user
            agent_user = environment.default_user or "root"
            await self.exec_as_root(
                environment,
                command=f"mkdir -p {_VENV_PATH} && chown {agent_user}:{agent_user} {_VENV_PATH}",
            )

            # Install uv, create an isolated venv, and install strands + user deps
            if self._unpublished_strands_ref:
                url = self._unpublished_strands_ref
                if not url.startswith("git+"):
                    url = f"git+{url}"
                if "#" not in url:
                    url = f"{url}#subdirectory=strands-py"
                strands_dep = f"strands-agents @ {url}"
            else:
                strands_dep = f"strands-agents{self._strands_version}"

            deps = [strands_dep]
            if self._agent_deps:
                for dep in self._agent_deps.replace(",", " ").split():
                    if dep.strip():
                        deps.append(dep.strip())
            deps_str = " ".join(shlex.quote(d) for d in deps)

            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    "curl -LsSf https://astral.sh/uv/install.sh | sh && "
                    'if [ -f "$HOME/.local/bin/env" ]; then source "$HOME/.local/bin/env"; fi && '
                    f"uv venv {_VENV_PATH} --clear && "
                    f"source {_VENV_PATH}/bin/activate && "
                    f"uv pip install {deps_str}"
                ),
            )

        # Upload the runner script that will invoke the user's agent
        runner_src = Path(__file__).parent / "runner.py"
        local_copy = self.logs_dir / "runner.py"
        local_copy.parent.mkdir(parents=True, exist_ok=True)
        local_copy.write_text(runner_src.read_text())
        await environment.upload_file(source_path=local_copy, target_path=_RUNNER_CONTAINER_PATH)

        # Upload the user's agent source into the container
        if self._agent_path:
            await self.exec_as_root(environment, command=f"mkdir -p {_AGENT_INSTALL_DIR}")
            agent_logs_source = str(EnvironmentPaths.agent_dir / "source")
            await self.exec_as_root(environment, command=f"mkdir -p {agent_logs_source}")
            if self._agent_path.is_dir():
                await environment.upload_dir(source_dir=self._agent_path, target_dir=_AGENT_INSTALL_DIR)
                await environment.upload_dir(source_dir=self._agent_path, target_dir=agent_logs_source)
                await environment.exec(
                    f"find {_AGENT_INSTALL_DIR} {agent_logs_source}"
                    " -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true",
                    timeout_sec=5,
                )
            else:
                target = f"{_AGENT_INSTALL_DIR}/{self._agent_path.name}"
                await environment.upload_file(source_path=self._agent_path, target_path=target)
                await environment.upload_file(
                    source_path=self._agent_path,
                    target_path=f"{agent_logs_source}/{self._agent_path.name}",
                )
