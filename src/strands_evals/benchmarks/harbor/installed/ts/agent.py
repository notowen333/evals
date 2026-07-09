"""Installed Strands TypeScript agent — runs a TS agent inside the Harbor container.

Usage::

    harbor run \\
      --agent strands_evals.benchmarks.harbor.installed.ts:StrandsInstalledTSAgent \\
      --model us.anthropic.claude-sonnet-4-6 \\
      --ak agent_path=./my-ts-agent \\
      -p <task>
"""

import shlex
from pathlib import Path
from typing import Any, override

from harbor.environments.base import BaseEnvironment

from ..base_strands_adapter import BaseStrandsInstalledAgent, _AGENT_INSTALL_DIR, _RESULT_PATH, _LOG_PATH

_RUNNER_CONTAINER_PATH = "/installed-agent/runner.mjs"
_NODE_VERSION = "22"


class StrandsInstalledTSAgent(BaseStrandsInstalledAgent):
    """Run any Strands TypeScript agent inside the Harbor container."""

    def __init__(
        self,
        logs_dir: Path,
        agent_path: str | None = None,
        agent_entry: str = "agent.js",
        node_version: str = _NODE_VERSION,
        unpublished_strands_ref: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir, **kwargs)
        self._agent_path = Path(agent_path) if agent_path else None
        self._agent_entry = agent_entry
        self._node_version = node_version
        self._unpublished_strands_ref = unpublished_strands_ref

    @staticmethod
    @override
    def name() -> str:
        return "strands-installed-ts"

    @override
    def get_version_command(self) -> str | None:
        return (
            '. ~/.nvm/nvm.sh && node -e '
            f"\"console.log(require('{_AGENT_INSTALL_DIR}/node_modules/@strands-agents/sdk/package.json').version)\""
        )

    @override
    def _build_run_command(self, cli_flags: str | None) -> list[str]:
        agent_entry = shlex.quote(self._agent_entry)
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
        return parts

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # Fast-path: skip if node_modules already has @strands-agents/sdk
        check_result = await environment.exec(
            command=f'[ -d {_AGENT_INSTALL_DIR}/node_modules/@strands-agents/sdk ] && echo ok',
        )
        if check_result.return_code == 0 and "ok" in (check_result.stdout or ""):
            self.logger.debug("Strands TS agent already installed, skipping")
        else:
            # Install curl + git (curl for nvm installer, git for --unpublished-strands-ref)
            await self.exec_as_root(
                environment,
                command=(
                    "(command -v curl >/dev/null && command -v git >/dev/null) || "
                    "(apt-get update -qq && apt-get install -y -qq curl git)"
                ),
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

            # Install npm dependencies, optionally overriding @strands-agents/sdk with a git ref
            install_cmd = "npm install --production"
            if self._unpublished_strands_ref:
                ref = self._unpublished_strands_ref
                if not ref.startswith("git+"):
                    ref = f"git+{ref}"
                if "#" not in ref:
                    ref = f"{ref}#subdirectory=strands-ts"
                install_cmd += f" && npm install {shlex.quote(ref)}"

            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    '. "$HOME/.nvm/nvm.sh" && '
                    f"cd {_AGENT_INSTALL_DIR} && "
                    f"{install_cmd}"
                ),
            )

        # Upload the runner script
        runner_src = Path(__file__).parent / "runner.mjs"
        local_copy = self.logs_dir / "runner.mjs"
        local_copy.parent.mkdir(parents=True, exist_ok=True)
        local_copy.write_text(runner_src.read_text())
        await environment.upload_file(source_path=local_copy, target_path=_RUNNER_CONTAINER_PATH)
