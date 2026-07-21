"""Container entrypoint invoked by AWSCEATAgentTaskExecutor's DockerAgentBridge.

Option (b): the agent runs in THIS (agent) image, and its tools ``docker exec``
into the Task Container the TaskExecutor passes as ``{task_container_id}``.

DockerAgentBridge substitutes its command template. Registered ``harbor_command``:

    python3 -m strand_agent.run_infer \\
        --dataset-container-id     {task_container_id} \\
        --problem-statement        {instruction} \\
        --output-dir               {output_dir} \\
        --container-workspace-path  {workdir} \\
        --model-name               <model_name>     # from harbor_required_params

Responsibilities (thin — the real work is reused from ``runner.py``):
  1. Map the bridge's args to the env vars ``agent.py`` reads
     (``TASK_CONTAINER_ID`` / ``CONTAINER_WORKSPACE_PATH`` / ``STRANDS_MODEL``).
  2. Delegate to the reused ``runner.py``, which imports the agent module, runs
     the instruction, and writes ``result.json`` + ``conversation.json``.
  3. Re-emit token usage as ``vibe_metrics.json`` in the shape
     ``DockerAgentBridge._parse_metrics`` reads.

The patch is NOT produced here — DockerAgentBridge extracts it via ``git diff``
inside the task container after this process exits.
"""

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

from strand_agent import RELEASE_ID
from strand_agent.trajectory import write_atif_trajectory

_PKG = __package__ or "strand_agent"
_AGENT_SPEC = f"{_PKG}.agent:MyAgent"


def _write_vibe_metrics(output_dir: Path, result: dict) -> None:
    """Translate runner.py's result.json into DockerAgentBridge's vibe_metrics.json."""
    usage = result.get("accumulated_usage") or {}
    token_usage = [usage] if usage else []
    metrics = {
        "token_usage": token_usage,
        "stop_reason": result.get("stop_reason"),
        "cycle_count": result.get("cycle_count"),
        "model_name": result.get("model_id"),
    }
    (output_dir / "vibe_metrics.json").write_text(json.dumps(metrics, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="DockerAgentBridge entrypoint for strands-agent.")
    parser.add_argument("--dataset-container-id", required=True, help="Task container id ({task_container_id}).")
    parser.add_argument("--problem-statement", required=True, help="The task instruction ({instruction}).")
    parser.add_argument("--output-dir", required=True, help="Artifact output dir ({output_dir}, host bind-mounted).")
    parser.add_argument(
        "--container-workspace-path", default="/testbed", help="Workdir inside the task container ({workdir})."
    )
    parser.add_argument("--model-name", default=None, help="Bedrock model id or alias (resolved in agent.py).")
    parser.add_argument("--max-iterations", type=int, default=None, help="Max event-loop cycles.")
    args, _unknown = parser.parse_known_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Point agent.py's DockerSandbox at the passed task container.
    os.environ["TASK_CONTAINER_ID"] = args.dataset_container_id
    os.environ["CONTAINER_WORKSPACE_PATH"] = args.container_workspace_path
    if args.model_name:
        os.environ["STRANDS_MODEL"] = args.model_name

    result_path = output_dir / "result.json"
    runner_argv = [
        "runner.py",
        "--agent",
        _AGENT_SPEC,
        "--instruction",
        args.problem_statement,
        "--output",
        str(result_path),
    ]
    if args.max_iterations is not None:
        runner_argv += ["--max-turns", str(args.max_iterations)]

    runner = importlib.import_module(f"{_PKG}.runner")
    sys.argv = runner_argv
    rc = runner.main()

    result = {}
    try:
        if result_path.exists():
            result = json.loads(result_path.read_text())
            _write_vibe_metrics(output_dir, result)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"run_infer: failed to write vibe_metrics.json: {exc}", file=sys.stderr)  # noqa: T201

    # ATIF trajectory.json (alongside the raw conversation.json). Best-effort.
    try:
        write_atif_trajectory(
            output_dir,
            result,
            agent_version=RELEASE_ID,
            model_name=args.model_name,
        )
    except Exception as exc:  # noqa: BLE001 — never fail the run over a trajectory artifact
        print(f"run_infer: failed to write trajectory.json: {exc}", file=sys.stderr)  # noqa: T201

    return rc


if __name__ == "__main__":
    sys.exit(main())
