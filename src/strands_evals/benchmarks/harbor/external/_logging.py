"""Plugin that records the conversation to a JSONL trajectory file."""

import json
import logging
from pathlib import Path

from strands.hooks import MessageAddedEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)


class TrajectoryLogger(Plugin):
    """Append each conversation message to ``<logs_dir>/trajectory.jsonl``."""

    name = "trajectory-logger"

    def __init__(self, logs_dir: Path) -> None:
        self._path = Path(logs_dir) / "trajectory.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__()

    @hook  # type: ignore[call-overload]
    def on_message_added(self, event: MessageAddedEvent) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.message, default=str) + "\n")
        except Exception:
            logger.warning("path=<%s> | failed to append trajectory message", self._path, exc_info=True)
