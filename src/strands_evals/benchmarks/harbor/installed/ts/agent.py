"""Installed Strands TypeScript agent — runs a TS agent inside the Harbor container.

The user's Strands TS agent runs unchanged from how it works locally. Tools
naturally hit the container's filesystem and processes because the agent process
itself is inside the container.

Usage::

    harbor run \\
      --agent strands_evals.benchmarks.harbor.installed.ts:StrandsInstalledTSAgent \\
      --model us.anthropic.claude-sonnet-4-6 \\
      --ak agent_path=./my-ts-agent \\
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

_RUNNER_CONTAINER_PATH = "/installed-agent/runner.mjs"
_AGENT_INSTALL_DIR = "/installed-agent/user"
_NODE_VERSION = "22"
_RESULT_PATH = str(EnvironmentPaths.agent_dir / "result.json")
_LOG_PATH = str(EnvironmentPaths.agent_dir / "strands.log")


class StrandsInstalledTSAgent(BaseInstalledAgent):
    """Run any Strands TypeScript agent inside the Harbor container.

    The user provides a directory with a package.json and a module exporting
    createAgent(). We install Node + their deps, then exec a runner.mjs that
    imports and invokes their agent.
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
    ]

    ERROR_PATTERNS = [
        ErrorPattern(r"ThrottlingException", ApiRateLimitError),
        ErrorPattern(r"ModelThrottledException", ApiRateLimitError),
        *BaseInstalledAgent.ERROR_PATTERNS,
    ]

    def __init__(
        self,
        logs_dir: Path,
        agent_path: str | None = None,
        agent_entry: str = "agent.js",
        node_version: str = _NODE_VERSION,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir, **kwargs)
        self._agent_path = Path(agent_path) if agent_path else None
        self._agent_entry = agent_entry
        self._node_version = node_version

    @staticmethod
    @override
    def name() -> str:
        return "strands-installed-ts"

    @override
    def get_version_command(self) -> str | None:
        return f". ~/.nvm/nvm.sh && node -e \"console.log(require('{_AGENT_INSTALL_DIR}/node_modules/@strands-agents/sdk/package.json').version)\""

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # Fast-path: skip if node_modules already has @strands-agents/sdk
        check_result = await environment.exec(
            command=f'[ -d {_AGENT_INSTALL_DIR}/node_modules/@strands-agents/sdk ] && echo ok',
        )
        if check_result.return_code == 0 and "ok" in (check_result.stdout or ""):
            self.logger.debug("Strands TS agent already installed, skipping")
        else:
            # Install curl (needed for nvm installer)
            await self.exec_as_root(
                environment,
                command="command -v curl >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq curl)",
                env={"DEBIAN_FRONTEND": "noninteractive"},
            )

            # Install Node via nvm
            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    "curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash && "
                    'export NVM_DIR="$HOME/.nvm" && '
                    '\\. "$NVM_DIR/nvm.sh" && '
                    f"nvm install {self._node_version} && "
                    "node --version && npm --version"
                ),
            )

        # Upload user's agent directory
        if self._agent_path:
            agent_user = environment.default_user or "root"
            await self.exec_as_root(
                environment,
                command=f"mkdir -p {_AGENT_INSTALL_DIR} && chown {agent_user}:{agent_user} {_AGENT_INSTALL_DIR}",
            )
            await environment.upload_dir(source_dir=self._agent_path, target_dir=_AGENT_INSTALL_DIR)

            # Install npm dependencies
            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    '. "$HOME/.nvm/nvm.sh" && '
                    f"cd {_AGENT_INSTALL_DIR} && "
                    "npm install --production"
                ),
            )

        # Upload the runner script
        runner_src = Path(__file__).parent / "runner.mjs"
        local_copy = self.logs_dir / "runner.mjs"
        local_copy.parent.mkdir(parents=True, exist_ok=True)
        local_copy.write_text(runner_src.read_text())
        await environment.upload_file(source_path=local_copy, target_path=_RUNNER_CONTAINER_PATH)

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

        # Inject model name and resolve matching provider API keys
        if self.model_name:
            env["STRANDS_MODEL"] = self.model_name
            try:
                for var in get_api_key_var_names_from_model_name(self.model_name):
                    value = self._get_env(var)
                    if value:
                        env[var] = value
            except ValueError:
                pass

        # Forward OpenTelemetry config so traces reach the host collector
        for var in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_HEADERS",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_SERVICE_NAME",
        ):
            value = self._get_env(var)
            if value:
                env[var] = value

        # Pass instruction as env var to avoid shell quoting issues
        env["HARBOR_INSTRUCTION"] = instruction
        agent_entry = shlex.quote(self._agent_entry)

        # Build the runner invocation
        cli_flags = self.build_cli_flags()

        parts = [
            '. "$HOME/.nvm/nvm.sh" &&',
            f"cd {_AGENT_INSTALL_DIR} &&",
            f"node {_RUNNER_CONTAINER_PATH}",
            f"--agent {agent_entry}",
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
        """Read metrics from result.json that Harbor downloaded to logs_dir."""
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

        from ..trajectory import convert_strands_to_atif

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
                model_name=self.model_name,
                result_data=result_data,
            )
            trajectory_path = self.logs_dir / "trajectory.json"
            trajectory_path.write_text(format_trajectory_json(trajectory.to_json_dict()))
            logger.debug("path=<%s> | wrote ATIF trajectory", trajectory_path)
        except Exception as exc:
            logger.debug("trajectory_error=<%s> | failed to convert to ATIF", exc)
