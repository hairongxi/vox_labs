"""Small thread-safe event bus between audio/network threads and Tk."""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class UiEventKind(str, Enum):
    STATE = "state"
    PARTIAL = "partial"
    FINAL = "final"
    ASSISTANT_DELTA = "assistant_delta"
    ASSISTANT_DONE = "assistant_done"
    ASSISTANT_CLEAR = "assistant_clear"
    LEVEL = "level"
    LATENCY = "latency"
    ERROR = "error"
    TOAST = "toast"
    DEBUG = "debug"
    CONNECTION = "connection"
    CONFIG = "config"
    CLEAR = "clear"
    MUTE = "mute"


@dataclass(frozen=True)
class UiEvent:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class UiBus:
    """A bounded queue. Dropping old UI events is preferable to blocking audio."""

    def __init__(self, maxsize: int = 512) -> None:
        self._q: queue.Queue[UiEvent] = queue.Queue(maxsize=maxsize)

    def emit(self, kind: UiEventKind | str, **data: Any) -> None:
        event = UiEvent(str(kind.value if isinstance(kind, UiEventKind) else kind), data)
        try:
            self._q.put_nowait(event)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(event)
            except queue.Empty:
                pass

    def drain(self, limit: int = 100) -> list[UiEvent]:
        out: list[UiEvent] = []
        for _ in range(limit):
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out
