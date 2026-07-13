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
    """Import 'module:symbol' and resolve to an Agent + invoke callable.

    Supports:
    - A class with a create_agent() method
    - Plain function (legacy): calls it to get the Agent

    Returns (agent_instance, invoke_fn_or_None, benchmark_instance_or_None).
    """
    if ":" not in agent_spec:
        raise ValueError(f"agent_spec must be 'module:symbol', got: {agent_spec!r}")
    module_path, symbol_name = agent_spec.rsplit(":", 1)
    mod = import_module(module_path)
    symbol = getattr(mod, symbol_name)

    # Class with create_agent() method
    if isinstance(symbol, type):
        instance = symbol()
        agent = instance.create_agent()
        invoke_fn = getattr(instance, "invoke_agent", None)
        return agent, invoke_fn, instance

    # Legacy: plain create_agent() function
    if not callable(symbol):
        raise TypeError(f"{agent_spec} is not callable and has no create_agent() method")
    agent = symbol()
    return agent, None, None


def _token_fields(usage: dict) -> dict:
    """Map Strands' accumulated_usage to Harbor's token fields.

    Strands reports four mutually-exclusive counts that sum to totalTokens:
    inputTokens (uncached), cacheReadInputTokens, cacheWriteInputTokens,
    outputTokens. Harbor's AgentContext only has n_input_tokens (defined as
    "including cache"), n_cache_tokens, and n_output_tokens.

    So input_tokens is the FULL input side (uncached + cache read + cache write)
    to match Harbor's definition, and cache_tokens is all cache activity. The
    raw accumulated_usage is still dumped alongside for the exact read/write split.
    """
    usage = usage or {}
    uncached = usage.get("inputTokens") or 0
    cache_read = usage.get("cacheReadInputTokens") or 0
    cache_write = usage.get("cacheWriteInputTokens") or 0
    return {
        "input_tokens": uncached + cache_read + cache_write,
        "output_tokens": usage.get("outputTokens"),
        "cache_tokens": cache_read + cache_write,
    }


def _write_result(output_path: Path, data: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        json.dump(data, f)


def _dump_conversation(output_dir: Path, all_messages: list, agent) -> None:
    """Write conversation.json (full JSON array) from recorded messages.

    The JSONL file is written incrementally during the run (survives kills).
    This writes the final clean JSON array that the ATIF converter reads.
    """
    conversation_path = output_dir / "conversation.json"
    try:
        messages = all_messages if all_messages else getattr(agent, "messages", [])
        json.dump(messages, conversation_path.open("w"), default=str)
    except Exception:
        pass


def _capture_patch(output_dir: Path) -> None:
    """Write a unified diff of all changes in the working tree to patch.diff."""
    import subprocess

    try:
        # Find the git repo root (workdir varies per task)
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if root.returncode != 0:
            return
        repo_root = root.stdout.strip()
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True, text=True, timeout=30,
            cwd=repo_root,
        )
        if result.returncode == 0 and result.stdout.strip():
            (output_dir / "patch.diff").write_text(result.stdout)
    except Exception:
        pass


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
        agent, invoke_fn, benchmark_instance = _import_agent(args.agent)
    except Exception:
        traceback.print_exc()
        _write_result(output_path, {"error": traceback.format_exc(), "stop_reason": "import_error"})
        return 1

    # Stream messages to a JSONL file as they happen. This survives hard kills
    # (same principle as `tee` for strands.log) — even if the runner is SIGKILLed,
    # messages up to the last completed write are preserved.
    all_messages: list = []
    conversation_path = output_path.parent / "conversation.jsonl"
    conversation_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from strands.hooks import MessageAddedEvent
        from strands.plugins import Plugin, hook

        class _MessageRecorder(Plugin):
            name = "harbor-message-recorder"

            @hook  # type: ignore[call-overload]
            def on_message(self, event: MessageAddedEvent) -> None:
                all_messages.append(event.message)
                with conversation_path.open("a") as f:
                    f.write(json.dumps(event.message, default=str) + "\n")

        agent._plugin_registry.add_and_init(_MessageRecorder())
    except Exception:
        pass  # fall back to agent.messages if plugin fails

    # SIGTERM handler: dump metrics on timeout. Conversation is already on disk
    # (written incrementally above), but metrics need a final flush.
    import signal

    def _on_sigterm(signum, frame):
        metrics = getattr(agent, "event_loop_metrics", None)
        usage = getattr(metrics, "accumulated_usage", {}) if metrics else {}
        _write_result(
            output_path,
            {
                "error": "Agent timed out (SIGTERM)",
                "stop_reason": "timeout",
                **_token_fields(usage),
                "cycle_count": getattr(metrics, "cycle_count", None),
                "accumulated_usage": dict(usage) if usage else None,
            },
        )
        sys.exit(1)

    signal.signal(signal.SIGTERM, _on_sigterm)

    # Apply max_turns if provided and the agent supports limits
    invoke_kwargs: dict = {}
    if args.max_turns is not None:
        try:
            from strands.types.agent import Limits

            invoke_kwargs["limits"] = Limits(turns=args.max_turns)
        except (ImportError, TypeError):
            pass

    try:
        if invoke_fn is not None:
            result = invoke_fn(agent, args.instruction, **invoke_kwargs)
        else:
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
                **_token_fields(usage),
                "cycle_count": getattr(metrics, "cycle_count", None),
                "accumulated_usage": dict(usage) if usage else None,
            },
        )
        # Dump partial conversation even on error
        _dump_conversation(output_path.parent, all_messages, agent)
        return 1

    # Success — write full metrics
    metrics = agent.event_loop_metrics
    usage = metrics.accumulated_usage if metrics else {}

    # Strands version + model id for ATIF agent metadata
    try:
        from importlib.metadata import version as pkg_version

        strands_version = pkg_version("strands-agents")
    except Exception:
        strands_version = None

    try:
        model_id = agent.model.get_config().get("model_id")
    except Exception:
        model_id = None

    # Per-cycle timestamps from traces
    cycle_timestamps = []
    for trace in getattr(metrics, "traces", []):
        cycle_timestamps.append({"start_time": trace.start_time, "end_time": trace.end_time})

    # Per-tool usage stats
    tool_stats = {}
    for name, tm in getattr(metrics, "tool_metrics", {}).items():
        tool_stats[name] = {
            "call_count": tm.call_count,
            "success_count": tm.success_count,
            "error_count": tm.error_count,
            "total_time": round(tm.total_time, 3),
        }

    _write_result(
        output_path,
        {
            **_token_fields(usage),
            "stop_reason": getattr(result, "stop_reason", None),
            "cycle_count": getattr(metrics, "cycle_count", None),
            "accumulated_usage": dict(usage) if usage else None,
            "strands_version": strands_version,
            "model_id": model_id,
            "cycle_timestamps": cycle_timestamps,
            "tool_stats": tool_stats,
        },
    )

    # Write the full conversation for ATIF trajectory conversion.
    _dump_conversation(output_path.parent, all_messages, agent)

    # Capture git patch of all changes the agent made.
    _capture_patch(output_path.parent)

    return 0


if __name__ == "__main__":
    sys.exit(main())
