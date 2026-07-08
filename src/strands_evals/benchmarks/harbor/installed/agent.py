"""Installed Strands agent — runs the user's agent inside the Harbor container.

The user's Strands agent runs unchanged from how it works locally. Tools (shell,
file_read, file_write, etc.) naturally hit the container's filesystem and processes
because the agent process itself is inside the container.

Usage::

    harbor run \\
      --agent strands_evals.benchmarks.harbor.installed:StrandsInstalledAgent \\
      --model us.anthropic.claude-sonnet-4-6 \\
      --ak agent_module=my_agent:agent \\
      --ak agent_path=./my_agent.py \\
      -p <task>
"""

import json
import logging
import shlex
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import (
    ApiRateLimitError,
    BaseInstalledAgent,
    CliFlag,
    EnvVar,
    ErrorPattern,
    with_prompt_template,
)
from harbor.agents.utils import get_api_key_var_names_from_model_name
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

logger = logging.getLogger(__name__)

_RUNNER_CONTAINER_PATH = "/installed-agent/runner.py"
_AGENT_INSTALL_DIR = "/installed-agent/user"
_VENV_PATH = "/installed-agent/venv"
_VENV_PYTHON = f"{_VENV_PATH}/bin/python3"
_RESULT_PATH = str(EnvironmentPaths.agent_dir / "result.json")
_LOG_PATH = str(EnvironmentPaths.agent_dir / "strands.log")


class StrandsInstalledAgent(BaseInstalledAgent):
    """Run any Strands agent inside the Harbor container.

    The user provides their agent file and a module:attribute pointing at the Agent
    instance. We install strands + their deps into the container and exec a runner that
    imports and invokes their agent. Tools work transparently because the process is
    inside the container.
    """

    SUPPORTS_ATIF = True
    SUPPORTS_WINDOWS = False

    CLI_FLAGS = [
        CliFlag(
            "max_turns",
            cli="--max-turns",
            type="int",
            env_fallback="STRANDS_MAX_TURNS",
        ),
    ]

    ENV_VARS = [
        EnvVar(
            "non_interactive",
            env="STRANDS_NON_INTERACTIVE",
            type="bool",
            default=True,
            bool_true="true",
            bool_false="false",
        ),
        EnvVar(
            "bypass_tool_consent",
            env="BYPASS_TOOL_CONSENT",
            type="bool",
            default=True,
            bool_true="true",
            bool_false="false",
        ),
    ]

    ERROR_PATTERNS = [
        ErrorPattern(r"ThrottlingException", ApiRateLimitError),
        ErrorPattern(r"ModelThrottledException", ApiRateLimitError),
        *BaseInstalledAgent.ERROR_PATTERNS,
    ]

    def __init__(
        self,
        logs_dir: Path,
        agent_module: str = "agent:agent",
        agent_path: str | None = None,
        agent_deps: str = "strands-agents-tools",
        strands_version: str = ">=1.42.0",
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir, **kwargs)
        self._agent_module = agent_module
        self._agent_path = Path(agent_path) if agent_path else None
        self._agent_deps = agent_deps or ""
        self._strands_version = strands_version

    @staticmethod
    @override
    def name() -> str:
        return "strands-installed"

    @override
    def get_version_command(self) -> str | None:
        return f'{_VENV_PYTHON} -c "import strands; print(strands.__version__)"'

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # Fast-path: skip install entirely if venv already has strands (idempotent on retries)
        check_result = await environment.exec(
            command=f'[ -f {_VENV_PYTHON} ] && {_VENV_PYTHON} -c "import strands" 2>/dev/null',
        )
        if check_result.return_code == 0:
            self.logger.debug("Strands venv already installed, skipping")
        else:
            # Ensure curl is present (needed to fetch the uv installer)
            await self.exec_as_root(
                environment,
                command="command -v curl >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq curl)",
                env={"DEBIAN_FRONTEND": "noninteractive"},
            )

            # Create venv dir writable by the non-root agent user
            agent_user = environment.default_user or "root"
            await self.exec_as_root(
                environment,
                command=f"mkdir -p {_VENV_PATH} && chown {agent_user}:{agent_user} {_VENV_PATH}",
            )

            # Install uv, create an isolated venv, and install strands + user deps
            deps = [f"strands-agents{self._strands_version}"]
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
            if self._agent_path.is_dir():
                await environment.upload_dir(source_dir=self._agent_path, target_dir=_AGENT_INSTALL_DIR)
            else:
                target = f"{_AGENT_INSTALL_DIR}/{self._agent_path.name}"
                await environment.upload_file(source_path=self._agent_path, target_path=target)

    @with_prompt_template
    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # Merge env vars: declarative ENV_VARS first, then extra_env (CLI --ae overrides)
        env: dict[str, str] = {}
        env.update(self.resolve_env_vars())
        env.update(self._extra_env)

        # Inject model name and resolve matching provider API keys (e.g. AWS creds for Bedrock)
        if self.model_name:
            env["STRANDS_MODEL"] = self.model_name
            try:
                for var in get_api_key_var_names_from_model_name(self.model_name):
                    value = self._get_env(var)
                    if value:
                        env[var] = value
            except ValueError:
                pass

        # Forward OpenTelemetry config so traces from the container reach the host collector
        for var in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_HEADERS",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_SERVICE_NAME",
        ):
            value = self._get_env(var)
            if value:
                env[var] = value

        # Append MCP server descriptions to the instruction so the agent knows what's available
        if self.mcp_servers:
            mcp_info = "\n\nMCP Servers:\nThe following MCP servers are available.\n"
            for server in self.mcp_servers:
                if server.transport == "stdio":
                    args_str = " ".join(server.args)
                    mcp_info += f"- {server.name}: stdio transport, command: {server.command} {args_str}\n"
                else:
                    mcp_info += f"- {server.name}: {server.transport} transport, url: {server.url}\n"
            instruction = instruction + mcp_info

        # Pass instruction as an env var — avoids shell quoting issues with arbitrary text
        env["HARBOR_INSTRUCTION"] = instruction
        agent_module = shlex.quote(self._agent_module)

        # Build the runner invocation: cd into agent dir, run via venv python, tee output
        cli_flags = self.build_cli_flags()

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
        command = " ".join(parts)

        await self.exec_as_agent(environment, command=command, env=env)

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        """Read metrics from the result.json that Harbor downloaded to logs_dir."""
        result_path = self.logs_dir / "result.json"
        if not result_path.exists():
            logger.warning("path=<%s> | result.json not found in logs_dir", result_path)
            return

        try:
            data = json.loads(result_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("path=<%s> | failed to parse result.json: %s", result_path, exc)
            return

        if data.get("error"):
            logger.warning("agent_error=<%s> | runner reported an error", data["error"][:200])

        context.n_input_tokens = data.get("input_tokens")
        context.n_output_tokens = data.get("output_tokens")
        context.n_cache_tokens = data.get("cache_tokens")
        context.cost_usd = None
        context.metadata = {
            "model": self.model_name,
            "stop_reason": data.get("stop_reason"),
            "cycle_count": data.get("cycle_count"),
            "accumulated_usage": data.get("accumulated_usage"),
        }

        # Convert conversation to ATIF trajectory
        self._write_atif_trajectory(data)

    def _write_atif_trajectory(self, result_data: dict) -> None:
        """Convert conversation.json to ATIF and write trajectory.json."""
        from harbor.utils.trajectory_utils import format_trajectory_json

        from .trajectory import convert_strands_to_atif

        conversation_path = self.logs_dir / "conversation.json"
        if not conversation_path.exists():
            logger.debug("path=<%s> | no conversation.json to convert", conversation_path)
            return

        try:
            messages = json.loads(conversation_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("path=<%s> | failed to read conversation: %s", conversation_path, exc)
            return

        try:
            trajectory = convert_strands_to_atif(
                messages,
                agent_name=self.name(),
                agent_version=result_data.get("strands_version") or self.version() or "unknown",
                model_name=result_data.get("model_id") or self.model_name,
                result_data=result_data,
            )
            trajectory_path = self.logs_dir / "trajectory.json"
            trajectory_path.write_text(format_trajectory_json(trajectory.to_json_dict()))
            logger.debug("path=<%s> | wrote ATIF trajectory", trajectory_path)
        except Exception as exc:
            logger.debug("trajectory_error=<%s> | failed to convert to ATIF", exc)
