"""Shared base class for Strands installed agents (Python and TypeScript).

Contains the common logic for env var resolution, metrics reading, and ATIF
trajectory conversion. Language-specific subclasses implement install() and
the command assembly in run().
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

_AGENT_INSTALL_DIR = "/installed-agent/user"
_RESULT_PATH = str(EnvironmentPaths.agent_dir / "result.json")
_LOG_PATH = str(EnvironmentPaths.agent_dir / "strands.log")


class BaseStrandsInstalledAgent(BaseInstalledAgent):
    """Shared base for Python and TypeScript Strands agent adapters.

    Subclasses must implement:
      - name()
      - get_version_command()
      - install()
      - _build_run_command(agent_entry, cli_flags) -> list[str]
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

    def _build_run_command(self, cli_flags: str | None) -> list[str]:
        """Build the shell command parts for running the agent. Subclasses override this."""
        raise NotImplementedError

    def _resolve_run_env(self, instruction: str) -> tuple[dict[str, str], str]:
        """Resolve env vars and instruction for the run. Returns (env_dict, final_instruction)."""
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

        # Append MCP server descriptions to the instruction
        if self.mcp_servers:
            mcp_info = "\n\nMCP Servers:\nThe following MCP servers are available.\n"
            for server in self.mcp_servers:
                if server.transport == "stdio":
                    args_str = " ".join(server.args)
                    mcp_info += f"- {server.name}: stdio transport, command: {server.command} {args_str}\n"
                else:
                    mcp_info += f"- {server.name}: {server.transport} transport, url: {server.url}\n"
            instruction = instruction + mcp_info

        env["HARBOR_INSTRUCTION"] = instruction
        return env, instruction

    @with_prompt_template
    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        env, instruction = self._resolve_run_env(instruction)

        cli_flags = self.build_cli_flags()
        parts = self._build_run_command(cli_flags)
        command = " ".join(parts)

        try:
            await self.exec_as_agent(environment, command=command, env=env)
        finally:
            # If runner was killed before writing result.json, create a minimal fallback
            try:
                fallback = '{"stop_reason":"timeout","error":"killed before flush"}'
                await environment.exec(
                    f"[ -f {_RESULT_PATH} ] || echo '{fallback}' > {_RESULT_PATH}",
                    timeout_sec=5,
                )
            except Exception:
                pass

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
            "model": data.get("model_id") or self.model_name,
            "stop_reason": data.get("stop_reason"),
            "cycle_count": data.get("cycle_count"),
            "accumulated_usage": data.get("accumulated_usage"),
        }

        self._write_atif_trajectory(data)

    def _write_atif_trajectory(self, result_data: dict) -> None:
        """Convert conversation.json (or .jsonl fallback) to ATIF trajectory."""
        from harbor.utils.trajectory_utils import format_trajectory_json

        from .trajectory import convert_strands_to_atif

        conversation_path = self.logs_dir / "conversation.json"
        conversation_jsonl_path = self.logs_dir / "conversation.jsonl"

        messages = None
        if conversation_path.exists():
            try:
                messages = json.loads(conversation_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("path=<%s> | failed to read conversation.json: %s", conversation_path, exc)

        # Fall back to JSONL (incremental, survives timeout kills)
        if messages is None and conversation_jsonl_path.exists():
            try:
                messages = [
                    json.loads(line)
                    for line in conversation_jsonl_path.read_text().splitlines()
                    if line.strip()
                ]
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("path=<%s> | failed to read conversation.jsonl: %s", conversation_jsonl_path, exc)

        if not messages:
            logger.debug("path=<%s> | no conversation data to convert", self.logs_dir)
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
