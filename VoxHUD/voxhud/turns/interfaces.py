"""Explicit contracts for turn management.

These interfaces intentionally mirror the three concerns FireRedChat separates:

* ``EndOfTurnDetector`` decides *when* a user utterance is complete.
* ``BargeInPolicy`` decides *whether* user audio should interrupt the assistant.
* ``TurnManager`` owns the dialogue state machine and cancellation epochs.

Keeping them separate makes it possible to swap an energy VAD for a semantic
turn detector, or a simple energy rule for a learned barge-in model, without
touching the WebSocket providers or the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol


class TurnState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR = "error"


@dataclass(frozen=True)
class VadFrame:
    timestamp_ms: int
    rms: float
    speech_probability: float
    is_speech: bool
    raw_rms: float = 0.0
    echo_suppression: float = 0.0


@dataclass(frozen=True)
class EndOfTurnDecision:
    commit: bool
    reason: str
    confidence: float = 0.0
    text: str = ""
    hold_ms: int = 0

    @staticmethod
    def wait() -> "EndOfTurnDecision":
        return EndOfTurnDecision(commit=False, reason="", confidence=0.0)


@dataclass(frozen=True)
class BargeInContext:
    now_ms: int
    state: TurnState
    state_since_ms: int
    speaking_since_ms: int | None
    assistant_active: bool
    last_barge_in_ms: int | None = None


@dataclass(frozen=True)
class BargeInDecision:
    trigger: bool
    reason: str = ""
    confidence: float = 0.0
    speech_ms: int = 0

    @staticmethod
    def no() -> "BargeInDecision":
        return BargeInDecision(trigger=False)


@dataclass(frozen=True)
class UserTurn:
    turn_id: int
    text: str
    reason: str
    confidence: float
    started_ms: int
    committed_ms: int


class EndOfTurnDetector(Protocol):
    """Pure turn-boundary policy, fed by VAD/ASR and polled by TurnManager."""

    def reset(self) -> None: ...

    def on_vad(self, frame: VadFrame) -> None: ...

    def on_asr_partial(self, text: str, now_ms: int) -> None: ...

    def on_asr_final(self, text: str, now_ms: int) -> None: ...

    def on_server_speech_started(self, now_ms: int) -> None: ...

    def on_server_speech_stopped(self, now_ms: int) -> None: ...

    def poll(self, now_ms: int) -> EndOfTurnDecision: ...

    def snapshot(self) -> dict[str, Any]: ...


class BargeInPolicy(Protocol):
    """Decides if current microphone activity should cancel the assistant."""

    def reset(self) -> None: ...

    def on_frame(self, frame: VadFrame, context: BargeInContext) -> BargeInDecision: ...


@dataclass
class TurnCallbacks:
    on_state: Callable[[TurnState, str], None]
    on_user_partial: Callable[[str], None]
    on_user_final: Callable[[str], None]
    on_commit: Callable[[UserTurn], None]
    on_barge_in: Callable[[str, BargeInDecision], None]
    on_cancel: Callable[[str], None]
    on_debug: Callable[[str, dict[str, Any]], None] = lambda _name, _data: None
