"""Convert a Strands conversation (messages list) to ATIF trajectory format."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)


def _extract_text(content_blocks: list[dict[str, Any]]) -> str:
    """Extract concatenated text from Strands content blocks."""
    parts = []
    for block in content_blocks:
        if "text" in block:
            parts.append(block["text"])
    return "\n".join(parts) if parts else ""


def _extract_reasoning(content_blocks: list[dict[str, Any]]) -> str | None:
    """Extract reasoning/thinking content from Strands content blocks."""
    parts = []
    for block in content_blocks:
        reasoning_content = block.get("reasoningContent")
        if reasoning_content and isinstance(reasoning_content, dict):
            text = reasoning_content.get("reasoningText", {}).get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts) if parts else None


def _extract_tool_calls(content_blocks: list[dict[str, Any]]) -> list[ToolCall]:
    """Extract tool calls from Strands content blocks."""
    calls = []
    for block in content_blocks:
        tool_use = block.get("toolUse")
        if tool_use:
            calls.append(
                ToolCall(
                    tool_call_id=tool_use.get("toolUseId", str(uuid.uuid4())),
                    function_name=tool_use.get("name", "unknown"),
                    arguments=tool_use.get("input", {}),
                )
            )
    return calls


def _extract_tool_results(content_blocks: list[dict[str, Any]]) -> list[ObservationResult]:
    """Extract tool results from Strands content blocks."""
    results = []
    for block in content_blocks:
        tool_result = block.get("toolResult")
        if tool_result:
            content_parts = tool_result.get("content", [])
            text_parts = []
            for part in content_parts:
                if isinstance(part, dict) and "text" in part:
                    text_parts.append(part["text"])
                elif isinstance(part, dict) and "json" in part:
                    text_parts.append(json.dumps(part["json"]))
                elif isinstance(part, str):
                    text_parts.append(part)
            results.append(
                ObservationResult(
                    source_call_id=tool_result.get("toolUseId"),
                    content="\n".join(text_parts) if text_parts else None,
                )
            )
    return results


def convert_strands_to_atif(
    messages: list[dict[str, Any]],
    *,
    agent_name: str = "strands-installed",
    agent_version: str = "0.1.0",
    model_name: str | None = None,
    session_id: str | None = None,
    result_data: dict[str, Any] | None = None,
) -> Trajectory:
    """Convert a Strands messages list to an ATIF Trajectory.

    Args:
        messages: The agent's conversation (list of Message dicts with role + content).
        agent_name: Name for the ATIF agent field.
        agent_version: Version for the ATIF agent field.
        model_name: Model used (for the agent config).
        session_id: Optional session id.
        result_data: The runner's result.json data (for final metrics).
    """
    steps: list[Step] = []
    step_id = 0
    cycle_timestamps = (result_data or {}).get("cycle_timestamps", [])
    cycle_idx = 0

    for msg in messages:
        role = msg.get("role", "")
        content_blocks = msg.get("content", [])
        if not content_blocks:
            continue

        if role == "user":
            # User messages that are pure tool results get merged as observations
            # on the previous agent step (if any).
            tool_results = _extract_tool_results(content_blocks)
            if tool_results and steps and steps[-1].source == "agent":
                steps[-1].observation = Observation(results=tool_results)
                continue

            # Otherwise it's a user text message
            step_id += 1
            text = _extract_text(content_blocks)
            if not text:
                text = "(tool results)"
            steps.append(Step(step_id=step_id, source="user", message=text))

        elif role == "assistant":
            step_id += 1
            text = _extract_text(content_blocks)
            tool_calls = _extract_tool_calls(content_blocks)
            reasoning = _extract_reasoning(content_blocks)

            # Per-step metrics from Strands message metadata
            step_metrics = None
            metadata = msg.get("metadata")
            if metadata:
                usage = metadata.get("usage", {})
                step_metrics = Metrics(
                    prompt_tokens=usage.get("inputTokens"),
                    completion_tokens=usage.get("outputTokens"),
                    cached_tokens=usage.get("cacheReadInputTokens"),
                )

            # Timestamp from cycle traces (one cycle per assistant message)
            timestamp = None
            if cycle_idx < len(cycle_timestamps):
                cycle_timestamp = cycle_timestamps[cycle_idx]
                start = cycle_timestamp.get("start_time")
                if start:
                    timestamp = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()
                cycle_idx += 1

            steps.append(
                Step(
                    step_id=step_id,
                    source="agent",
                    message=text or "(tool calls)",
                    timestamp=timestamp,
                    reasoning_content=reasoning,
                    tool_calls=tool_calls if tool_calls else None,
                    metrics=step_metrics,
                )
            )

    if not steps:
        steps = [Step(step_id=1, source="user", message="(empty conversation)")]

    # Final metrics from result data
    final_metrics = None
    if result_data:
        final_metrics = FinalMetrics(
            total_prompt_tokens=result_data.get("input_tokens"),
            total_completion_tokens=result_data.get("output_tokens"),
            total_cached_tokens=result_data.get("cache_tokens"),
            total_steps=len(steps),
        )

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id or str(uuid.uuid4()),
        agent=Agent(
            name=agent_name,
            version=agent_version,
            model_name=model_name,
        ),
        steps=steps,
        final_metrics=final_metrics,
        notes="Converted from Strands Agent conversation",
    )


def write_atif_trajectory(
    output_dir: Path,
    result_data: dict[str, Any],
    *,
    agent_name: str = "strands-agent",
    model_name: str | None = None,
) -> None:
    """Read a Strands conversation from *output_dir* and write ATIF trajectory.json.

    Standalone form of the installed adapter's ``_write_atif_trajectory``: reads
    ``conversation.json`` (falling back to the incremental ``conversation.jsonl``
    that survives timeout kills), converts it via :func:`convert_strands_to_atif`,
    and writes ``trajectory.json`` next to the other artifacts. Best-effort: logs
    and returns on any failure rather than raising.
    """
    from harbor.utils.trajectory_utils import format_trajectory_json

    conversation_path = output_dir / "conversation.json"
    conversation_jsonl_path = output_dir / "conversation.jsonl"

    messages = None
    if conversation_path.exists():
        try:
            messages = json.loads(conversation_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("path=<%s> | failed to read conversation.json: %s", conversation_path, exc)

    if messages is None and conversation_jsonl_path.exists():
        try:
            messages = [
                json.loads(line)
                for line in conversation_jsonl_path.read_text().splitlines()
                if line.strip()
            ]
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug(
                "path=<%s> | failed to read conversation.jsonl: %s", conversation_jsonl_path, exc
            )

    if not messages:
        logger.debug("path=<%s> | no conversation data to convert", output_dir)
        return

    try:
        trajectory = convert_strands_to_atif(
            messages,
            agent_name=agent_name,
            agent_version=result_data.get("strands_version") or "unknown",
            model_name=result_data.get("model_id") or model_name,
            result_data=result_data,
        )
        trajectory_path = output_dir / "trajectory.json"
        trajectory_path.write_text(format_trajectory_json(trajectory.to_json_dict()))
        logger.debug("path=<%s> | wrote ATIF trajectory", trajectory_path)
    except Exception as exc:
        logger.debug("trajectory_error=<%s> | failed to convert to ATIF", exc)
