"""Session helpers for sequence and deduplication."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


class SeenMessageCache:
    """Tracks seen message IDs using a capped FIFO set."""

    def __init__(self, max_size: int = 2048) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        self._set: set[str] = set()
        self._queue: deque[str] = deque()

    def mark(self, msg_id: str) -> bool:
        """Returns True if msg_id is newly observed, False if duplicate."""
        if msg_id in self._set:
            return False

        self._set.add(msg_id)
        self._queue.append(msg_id)

        while len(self._queue) > self._max_size:
            oldest = self._queue.popleft()
            self._set.discard(oldest)

        return True


@dataclass
class SequenceCounter:
    current: int = 0

    def next(self) -> int:
        self.current += 1
        return self.current


@dataclass
class EndpointState:
    session_id: str
    outgoing_seq: SequenceCounter = field(default_factory=SequenceCounter)
    incoming_seen: SeenMessageCache = field(default_factory=SeenMessageCache)
