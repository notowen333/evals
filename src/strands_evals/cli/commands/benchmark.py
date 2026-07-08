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
    """Resolve the agent directory and class import path.

    Finds a class with a create_agent method in any .py file.
    """
    if not agent_path.is_dir():
        raise FileNotFoundError(f"Expected a directory, got a file: {agent_path}")

    for py_file in sorted(agent_path.glob("*.py")):
        lines = py_file.read_text().splitlines()

        # Find a class that has a create_agent method
        current_class = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("class ") and ":" in stripped:
                current_class = stripped.split("(")[0].split(":")[0].replace("class ", "").strip()
            elif stripped.startswith("def create_agent") and current_class:
                return agent_path, f"{py_file.stem}:{current_class}"

    raise FileNotFoundError(
        f"No class with create_agent() found in {agent_path}. "
        "Your agent directory must contain a .py file with:\n\n"
        "    class MyAgent:\n"
        "        def create_agent(self):\n"
        "            return Agent(...)\n"
    )


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

    # Job name: explicit --name sets the harbor job directory name
    if args.name:
        cmd.extend(["--job-name", args.name])

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


def _find_latest_job_dir(output_dir: Path) -> Path | None:
    """Find the most recent job subdirectory in the output dir."""
    if not output_dir.exists():
        return None
    subdirs = [d for d in sorted(output_dir.iterdir(), reverse=True) if d.is_dir()]
    return subdirs[0] if subdirs else None


def _run(args: argparse.Namespace) -> int:
    harbor = _find_harbor()
    if not harbor:
        print("strands-evals: error: 'harbor' CLI not found. Install with: pip install harbor", file=sys.stderr)  # noqa: T201
        return 2

    # Resolve output dir: explicit -o or default to ./jobs
    output_dir = Path(args.output) if args.output else Path("jobs")

    cmd = _build_harbor_command(args)
    cmd[0] = harbor  # replace "harbor" with resolved path

    # Inject -o if the user specified it (otherwise harbor defaults to ./jobs)
    if args.output:
        cmd.extend(["-o", str(output_dir)])

    logger.debug("harbor command: %s", " ".join(cmd))

    result = subprocess.run(cmd)

    # Post-run: call on_benchmark_complete if the agent defines it
    job_dir = _find_latest_job_dir(output_dir)
    if job_dir:
        agent_dir, agent_module = _resolve_agent_dir_and_module(Path(args.agent_file).resolve())
        _invoke_on_complete(agent_dir, agent_module, job_dir)

    # Also run --post-run shell command if provided
    if args.post_run and job_dir:
        post_cmd = args.post_run.replace("{job_dir}", str(job_dir))
        logger.debug("post-run: %s", post_cmd)
        subprocess.run(post_cmd, shell=True)

    return result.returncode


def _invoke_on_complete(agent_dir: Path, agent_module: str, job_dir: Path) -> None:
    """Import the agent class and call on_benchmark_complete if it defines one."""
    import importlib
    import sys as _sys

    if str(agent_dir) not in _sys.path:
        _sys.path.insert(0, str(agent_dir))

    try:
        module_path, symbol_name = agent_module.rsplit(":", 1)
        mod = importlib.import_module(module_path)
        symbol = getattr(mod, symbol_name)
        if isinstance(symbol, type):
            instance = symbol()
            hook = getattr(instance, "on_benchmark_complete", None)
            if hook is not None:
                import json

                results = {}
                results_path = job_dir / "result.json"
                if results_path.exists():
                    results = json.loads(results_path.read_text())
                hook(job_dir, results)
    except Exception:
        logger.debug("on_benchmark_complete failed", exc_info=True)


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
        help="directory containing a .py file that defines `create_agent()` returning a Strands Agent",
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        default=None,
        help="name for this benchmark run (used as the job directory name)",
    )
    parser.add_argument(
        "-o", "--output",
        metavar="DIR",
        default=None,
        help="directory to store job results (default: ./jobs)",
    )
    parser.add_argument(
        "--post-run",
        metavar="CMD",
        default=None,
        help="command to run after the job completes. {job_dir} is replaced with the results path.",
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
