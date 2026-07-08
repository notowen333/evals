"""`strands-evals benchmark` — run a Strands agent file inside Harbor.

Resolves the agent file, infers deps, resolves AWS creds from the current boto3
session, and execs `harbor run` with the right flags. All other harbor flags are
passed through.

Usage::

    strands-evals benchmark ./my_agent.py -p <task>
    strands-evals benchmark ./my_agent.py --task org/task-name --debug
    strands-evals benchmark ./my_agent.py -p <task> -n 4 --max-retries 2
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_INSTALLED_AGENT = "strands_evals.benchmarks.harbor.installed:StrandsInstalledAgent"


def _resolve_aws_creds() -> dict[str, str]:
    """Resolve AWS credentials from the current boto3 session (supports SSO, profiles, env)."""
    try:
        import boto3

        session = boto3.Session()
        creds = session.get_credentials()
        if creds is None:
            return {}
        frozen = creds.get_frozen_credentials()
        env = {}
        if frozen.access_key:
            env["AWS_ACCESS_KEY_ID"] = frozen.access_key
        if frozen.secret_key:
            env["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
        if frozen.token:
            env["AWS_SESSION_TOKEN"] = frozen.token
        if session.region_name:
            env["AWS_REGION"] = session.region_name
        return env
    except Exception:
        logger.debug("failed to resolve AWS creds from boto3 session", exc_info=True)
        return {}


def _resolve_agent_dir_and_module(agent_path: Path) -> tuple[Path, str]:
    """Resolve the agent directory and module:attribute import path.

    The path must be a directory containing agent.py (which exports an `agent` attribute).
    """
    if not agent_path.is_dir():
        raise FileNotFoundError(f"Expected a directory, got a file: {agent_path}")
    if not (agent_path / "agent.py").exists():
        raise FileNotFoundError(f"No agent.py found in {agent_path}")
    return agent_path, "agent:provide_agent"


def _resolve_agent_deps(agent_dir: Path) -> str | None:
    """Look for a requirements.txt in the agent directory."""
    reqs = agent_dir / "requirements.txt"
    if reqs.exists():
        deps = [line.strip() for line in reqs.read_text().splitlines() if line.strip() and not line.startswith("#")]
        return " ".join(deps)
    return None


def _build_harbor_command(args: argparse.Namespace) -> list[str]:
    """Build the harbor run command from our args + passthrough."""
    agent_path = Path(args.agent_file).resolve()
    if not agent_path.exists():
        raise FileNotFoundError(f"Agent path not found: {agent_path}")

    agent_dir, agent_module = _resolve_agent_dir_and_module(agent_path)

    cmd = ["harbor", "run"]

    # Agent selection
    cmd.extend(["-a", _INSTALLED_AGENT])

    # Always upload the directory (file's parent or the dir itself)
    cmd.extend(["--ak", f"agent_path={agent_dir}"])
    cmd.extend(["--ak", f"agent_module={agent_module}"])

    # Deps: explicit --deps flag, or auto-detect from requirements.txt in agent dir
    deps = args.deps
    if deps is None:
        deps = _resolve_agent_deps(agent_dir)
    if deps:
        cmd.extend(["--ak", f"agent_deps={deps}"])

    # AWS creds from current session
    aws_env = _resolve_aws_creds()
    for key, value in aws_env.items():
        cmd.extend(["--ae", f"{key}={value}"])

    # Pass through all remaining harbor flags
    cmd.extend(args.harbor_args)

    return cmd


def _find_harbor() -> str | None:
    """Find the harbor CLI — check the current Python env's bin dir first."""
    bin_dir = Path(sys.executable).parent
    local = bin_dir / "harbor"
    if local.exists():
        return str(local)
    return shutil.which("harbor")


def _run(args: argparse.Namespace) -> int:
    harbor = _find_harbor()
    if not harbor:
        print("strands-evals: error: 'harbor' CLI not found. Install with: pip install harbor", file=sys.stderr)  # noqa: T201
        return 2

    cmd = _build_harbor_command(args)
    cmd[0] = harbor  # replace "harbor" with resolved path
    logger.debug("harbor command: %s", " ".join(cmd))

    result = subprocess.run(cmd)
    return result.returncode


def add_subparser(
    subparsers: argparse._SubParsersAction,
    parent: argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "benchmark",
        parents=[parent],
        help="run a Strands agent file inside Harbor (Terminal-Bench)",
        description=(
            "Run an arbitrary Strands agent file inside a Harbor container. "
            "The agent runs unchanged — tools hit the container natively. "
            "AWS creds are resolved from your current session automatically. "
            "All other flags are passed through to `harbor run`."
        ),
    )
    parser.add_argument(
        "agent_file",
        metavar="AGENT_DIR",
        help="directory containing agent.py that defines `provide_agent()` returning a Strands Agent",
    )
    parser.add_argument(
        "--deps",
        metavar="DEPS",
        default=None,
        help=(
            "pip dependencies to install in the container (space-separated). "
            "Auto-detected from requirements.txt in the agent directory if not specified."
        ),
    )
    parser.add_argument(
        "harbor_args",
        nargs=argparse.REMAINDER,
        help="remaining arguments passed through to `harbor run` (e.g. -p <task>, --debug, -n 4)",
    )
    parser.set_defaults(func=_run)
    return parser
