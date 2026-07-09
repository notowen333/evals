"""`strands-evals benchmark` — run a Strands agent file inside Harbor.

Resolves the agent file, infers deps, resolves AWS creds from the current boto3
session, and execs `harbor run` with the right flags. All other harbor flags are
passed through.

Usage::

    strands-evals benchmark ./my_agent -p <task>
    strands-evals benchmark ./my_agent --task org/task-name --debug
    strands-evals benchmark ./my_agent -p <task> -n 4 --max-retries 2

    # Batch: multiple datasets under one name
    strands-evals benchmark ./my_agent --name my-experiment \\
        -d org/suite-a -d org/suite-b -d org/suite-c -n 8
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)



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


_INSTALLED_AGENT_PY = "strands_evals.benchmarks.harbor.installed.py:StrandsInstalledPyAgent"
_INSTALLED_AGENT_TS = "strands_evals.benchmarks.harbor.installed.ts:StrandsInstalledTSAgent"


def _detect_agent_type(agent_path: Path) -> str:
    """Detect whether the agent directory is Python or TypeScript.

    Returns the harbor --agent import path for the appropriate installed adapter.
    """
    if (agent_path / "package.json").exists():
        return _INSTALLED_AGENT_TS
    return _INSTALLED_AGENT_PY


def _resolve_agent_dir_and_module(agent_path: Path) -> tuple[Path, str]:
    """Resolve the agent directory and class/entry import path.

    For Python: finds a class with a create_agent method.
    For TypeScript: uses the agent_entry (defaults to agent.js).
    """
    if not agent_path.is_dir():
        raise FileNotFoundError(f"Expected a directory, got a file: {agent_path}")

    # TypeScript: has package.json, entry point is handled by the TS adapter
    if (agent_path / "package.json").exists():
        return agent_path, "agent.js"

    # Python: scan for a class with create_agent()
    for py_file in sorted(agent_path.glob("*.py")):
        lines = py_file.read_text().splitlines()

        current_class = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("class ") and ":" in stripped:
                current_class = stripped.split("(")[0].split(":")[0].replace("class ", "").strip()
            elif stripped.startswith("def create_agent") and current_class:
                return agent_path, f"{py_file.stem}:{current_class}"

    raise FileNotFoundError(
        f"No agent found in {agent_path}. Expected either:\n"
        "  - A package.json (TypeScript agent with createAgent())\n"
        "  - A .py file with a class defining create_agent()\n"
    )


def _resolve_agent_deps(agent_dir: Path) -> str | None:
    """Look for a requirements.txt in the agent directory."""
    reqs = agent_dir / "requirements.txt"
    if reqs.exists():
        deps = [line.strip() for line in reqs.read_text().splitlines() if line.strip() and not line.startswith("#")]
        return " ".join(deps)
    return None


def _build_base_harbor_command(args: argparse.Namespace) -> list[str]:
    """Build the base harbor run command (agent setup, deps, creds). Does not include dataset/job-name/output."""
    agent_path = Path(args.agent_file).resolve()
    if not agent_path.exists():
        raise FileNotFoundError(f"Agent path not found: {agent_path}")

    agent_dir, agent_module = _resolve_agent_dir_and_module(agent_path)
    installed_agent = _detect_agent_type(agent_path)

    cmd = ["harbor", "run"]

    # Agent selection (Python or TypeScript adapter based on directory contents)
    cmd.extend(["-a", installed_agent])

    # Always upload the directory
    cmd.extend(["--ak", f"agent_path={agent_dir}"])
    if installed_agent == _INSTALLED_AGENT_TS:
        cmd.extend(["--ak", f"agent_entry={agent_module}"])
    else:
        cmd.extend(["--ak", f"agent_module={agent_module}"])

    # Deps: explicit --deps flag, or auto-detect from requirements.txt in agent dir
    deps = args.deps
    if deps is None:
        deps = _resolve_agent_deps(agent_dir)
    if deps:
        cmd.extend(["--ak", f"agent_deps={deps}"])

    # Unpublished strands ref (install from git instead of PyPI)
    if args.unpublished_strands_ref:
        cmd.extend(["--ak", f"unpublished_strands_ref={args.unpublished_strands_ref}"])

    # AWS creds from current session
    aws_env = _resolve_aws_creds()
    for key, value in aws_env.items():
        cmd.extend(["--ae", f"{key}={value}"])

    return cmd


def _extract_datasets_from_harbor_args(harbor_args: list[str]) -> tuple[list[str], list[str]]:
    """Split harbor_args into (datasets, remaining_args).

    Extracts -d/--dataset values so we can handle them as batch runs.
    """
    datasets = []
    remaining = []
    skip_next = False
    for i, arg in enumerate(harbor_args):
        if skip_next:
            skip_next = False
            continue
        if arg in ("-d", "--dataset"):
            if i + 1 < len(harbor_args):
                datasets.append(harbor_args[i + 1])
                skip_next = True
        elif arg.startswith("-d=") or arg.startswith("--dataset="):
            datasets.append(arg.split("=", 1)[1])
        else:
            remaining.append(arg)
    return datasets, remaining


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


def _dataset_slug(dataset: str) -> str:
    """Turn 'org/dataset-name@1.0' into a filesystem-safe slug for job naming."""
    return dataset.replace("/", "--").replace("@", "_")


def _run_single(harbor: str, base_cmd: list[str], *, output_dir: Path, job_name: str | None,
                extra_args: list[str]) -> int:
    """Execute a single harbor run."""
    cmd = list(base_cmd)
    cmd[0] = harbor
    cmd.extend(["-o", str(output_dir)])
    if job_name:
        cmd.extend(["--job-name", job_name])
    cmd.extend(extra_args)
    logger.debug("harbor command: %s", " ".join(cmd))
    return subprocess.run(cmd).returncode


def _run(args: argparse.Namespace) -> int:
    harbor = _find_harbor()
    if not harbor:
        print("strands-evals: error: 'harbor' CLI not found. Install with: pip install harbor", file=sys.stderr)  # noqa: T201
        return 2

    base_cmd = _build_base_harbor_command(args)
    output_dir = Path(args.output) if args.output else Path("jobs")

    # Extract datasets from harbor_args to detect batch mode
    datasets, remaining_harbor_args = _extract_datasets_from_harbor_args(args.harbor_args)

    if len(datasets) <= 1:
        # Single run: pass everything through (including the dataset if present)
        extra_args = list(args.harbor_args)
        job_name = args.name
        rc = _run_single(harbor, base_cmd, output_dir=output_dir, job_name=job_name,
                         extra_args=extra_args)
        # For single runs, the job dir is the most recent subdir harbor created
        results_dir = _find_latest_job_dir(output_dir)
    else:
        # Batch mode: one harbor run per dataset, all under a shared output dir
        batch_name = args.name or _generate_batch_name()
        batch_dir = output_dir / batch_name
        batch_dir.mkdir(parents=True, exist_ok=True)

        if args.parallel:
            rc = _run_batch_parallel(harbor, base_cmd, datasets=datasets,
                                     batch_dir=batch_dir,
                                     remaining_args=remaining_harbor_args)
        else:
            rc = _run_batch_sequential(harbor, base_cmd, datasets=datasets,
                                       batch_dir=batch_dir,
                                       remaining_args=remaining_harbor_args)
        # For batch runs, the results dir is the batch dir containing all sub-runs
        results_dir = batch_dir

    # Post-run: call on_benchmark_complete if the agent defines it
    if results_dir:
        agent_dir, agent_module = _resolve_agent_dir_and_module(Path(args.agent_file).resolve())
        _invoke_on_complete(agent_dir, agent_module, results_dir)

    # Also run --post-run shell command if provided
    if args.post_run and results_dir:
        post_cmd = args.post_run.replace("{job_dir}", str(results_dir))
        logger.debug("post-run: %s", post_cmd)
        subprocess.run(post_cmd, shell=True)

    return rc


def _generate_batch_name() -> str:
    """Generate a timestamp-based batch name."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _run_batch_sequential(harbor: str, base_cmd: list[str], *, datasets: list[str],
                          batch_dir: Path, remaining_args: list[str]) -> int:
    """Run multiple datasets sequentially, all results under batch_dir."""
    worst_rc = 0
    for i, dataset in enumerate(datasets, 1):
        print(f"[{i}/{len(datasets)}] Running dataset: {dataset}", file=sys.stderr)  # noqa: T201
        job_name = _dataset_slug(dataset)
        extra_args = ["-d", dataset] + remaining_args
        rc = _run_single(harbor, base_cmd, output_dir=batch_dir, job_name=job_name,
                         extra_args=extra_args)
        worst_rc = max(worst_rc, rc)
        if rc != 0:
            print(f"  WARNING: dataset {dataset} exited with code {rc}", file=sys.stderr)  # noqa: T201
    return worst_rc


def _run_batch_parallel(harbor: str, base_cmd: list[str], *, datasets: list[str],
                        batch_dir: Path, remaining_args: list[str]) -> int:
    """Run multiple datasets concurrently, all results under batch_dir."""
    worst_rc = 0
    print(f"Running {len(datasets)} datasets in parallel", file=sys.stderr)  # noqa: T201

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(datasets)) as pool:
        futures = {}
        for dataset in datasets:
            job_name = _dataset_slug(dataset)
            extra_args = ["-d", dataset] + remaining_args
            future = pool.submit(_run_single, harbor, base_cmd, output_dir=batch_dir,
                                 job_name=job_name, extra_args=extra_args)
            futures[future] = dataset

        for future in concurrent.futures.as_completed(futures):
            dataset = futures[future]
            try:
                rc = future.result()
            except Exception as exc:
                logger.error("dataset %s raised: %s", dataset, exc)
                rc = 1
            worst_rc = max(worst_rc, rc)
            status = "OK" if rc == 0 else f"FAILED (exit {rc})"
            print(f"  {dataset}: {status}", file=sys.stderr)  # noqa: T201

    return worst_rc


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
        "--parallel",
        action="store_true",
        default=False,
        help="run multiple datasets concurrently (default: sequential)",
    )
    parser.add_argument(
        "--post-run",
        metavar="CMD",
        default=None,
        help="command to run after the job completes. {job_dir} is replaced with the results path.",
    )
    parser.add_argument(
        "--unpublished-strands-ref",
        metavar="URL",
        default=None,
        help=(
            "install strands-agents from a git URL instead of PyPI. "
            "e.g. https://github.com/your-fork/sdk-python@your-branch"
        ),
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
