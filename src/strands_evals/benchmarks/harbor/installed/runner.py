"""In-container entrypoint that runs a user's Strands agent.

Uploaded into the Harbor container and executed as::

    python3 /installed-agent/runner.py \\
        --agent my_module:agent \\
        --instruction "..." \\
        --output /logs/agent/result.json

Imports the user's Agent instance, invokes it with the instruction, and writes
token metrics + trajectory to files that Harbor downloads post-run.

This file depends ONLY on strands (installed in the container). No harbor or
strands_evals imports.
"""

import argparse
import json
import os
import sys
import traceback
from importlib import import_module
from pathlib import Path


def _import_agent(agent_spec: str):
    """Import 'module.path:attribute' and return the Agent instance."""
    if ":" not in agent_spec:
        raise ValueError(f"agent_spec must be 'module:attribute', got: {agent_spec!r}")
    module_path, attr_name = agent_spec.rsplit(":", 1)
    mod = import_module(module_path)
    return getattr(mod, attr_name)


def _write_result(output_path: Path, data: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        json.dump(data, f)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Strands agent inside a Harbor container.")
    parser.add_argument("--agent", required=True, help="Import path to the Agent instance (module:attribute)")
    parser.add_argument("--instruction", required=True, help="The task instruction")
    parser.add_argument("--output", required=True, help="Path to write the result JSON")
    parser.add_argument("--max-turns", type=int, default=None, help="Max event loop cycles")
    args = parser.parse_args()

    output_path = Path(args.output)

    # Add the user agent directory to sys.path so imports resolve
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        agent = _import_agent(args.agent)
    except Exception:
        traceback.print_exc()
        _write_result(output_path, {"error": traceback.format_exc(), "stop_reason": "import_error"})
        return 1

    # Apply max_turns if provided and the agent supports limits
    invoke_kwargs: dict = {}
    if args.max_turns is not None:
        try:
            from strands.types.agent import Limits

            invoke_kwargs["limits"] = Limits(turns=args.max_turns)
        except (ImportError, TypeError):
            pass

    try:
        result = agent(args.instruction, **invoke_kwargs)
    except Exception:
        traceback.print_exc()
        # Still try to capture partial metrics
        metrics = getattr(agent, "event_loop_metrics", None)
        usage = getattr(metrics, "accumulated_usage", {}) if metrics else {}
        _write_result(
            output_path,
            {
                "error": traceback.format_exc(),
                "stop_reason": "error",
                "input_tokens": usage.get("inputTokens"),
                "output_tokens": usage.get("outputTokens"),
                "cache_tokens": usage.get("cacheReadInputTokens"),
                "cycle_count": getattr(metrics, "cycle_count", None),
                "accumulated_usage": dict(usage) if usage else None,
            },
        )
        return 1

    # Success — write full metrics
    metrics = agent.event_loop_metrics
    usage = metrics.accumulated_usage if metrics else {}

    _write_result(
        output_path,
        {
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
            "cache_tokens": usage.get("cacheReadInputTokens"),
            "stop_reason": getattr(result, "stop_reason", None),
            "cycle_count": getattr(metrics, "cycle_count", None),
            "accumulated_usage": dict(usage) if usage else None,
        },
    )

    # Write the full conversation for ATIF trajectory conversion
    conversation_path = output_path.parent / "conversation.json"
    try:
        json.dump(agent.messages, conversation_path.open("w"), default=str)
    except Exception:
        pass  # best-effort; metrics are the critical output

    return 0


if __name__ == "__main__":
    sys.exit(main())
