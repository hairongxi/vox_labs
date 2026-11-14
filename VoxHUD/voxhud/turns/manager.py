"""Dialogue turn state machine.

The manager is the single authority for *who owns the turn*. Providers and the
audio thread only feed it observations; it decides when to commit a user turn,
when to interrupt the assistant, and which assistant callbacks are stale.

Cancellation is handled with a monotonically increasing ``epoch``. Every
assistant audio/text callback carries its turn id; the controller additionally
compares the epoch before pushing audio to the speaker. This is the same
"generation" trick used by FireRedChat-style pipelines to make barge-in safe.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .interfaces import (
    BargeInContext,
    BargeInDecision,
    BargeInPolicy,
    EndOfTurnDecision,
    EndOfTurnDetector,
    TurnCallbacks,
    TurnState,
    UserTurn,
    VadFrame,
)

log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.monotonic() * 1000)


class TurnManager:
    def __init__(
        self,
        detector: EndOfTurnDetector,
        barge_policy: BargeInPolicy,
        callbacks: TurnCallbacks,
        min_utterance_chars: int = 1,
        barge_in_on_asr: bool = True,
        asr_barge_in_min_ms: int = 500,
    ) -> None:
        self.detector = detector
        self.barge_policy = barge_policy
        self.callbacks = callbacks
        self.min_utterance_chars = min_utterance_chars
        self.barge_in_on_asr = barge_in_on_asr
        self.asr_barge_in_min_ms = asr_barge_in_min_ms
        self._state = TurnState.IDLE
        self._state_detail = ""
        self._state_since = now_ms()
        self._turn_counter = 0
        self._current_turn_id: int | None = None
        self._committed = False
        self._epoch = 0
        self._speaking_since: int | None = None
        self._last_barge_in_ms: int | None = None
        self._interrupted_until = 0
        self._ignore_asr_until = 0
        self._last_emitted_partial = ""
        self._last_emitted_final = ""
        self._last_activity_ms = now_ms()
        self._last_speech_vad_ms: int | None = None

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> TurnState:
        return self._state

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def current_turn_id(self) -> int | None:
        return self._current_turn_id

    def start(self) -> None:
        self._epoch = 0
        self._committed = False
        self._current_turn_id = None
        self.detector.reset()
        self.barge_policy.reset()
        self._set_state(TurnState.LISTENING, "ready")

    def set_error(self, message: str) -> None:
        self._set_state(TurnState.ERROR, message)

    def recover(self) -> None:
        if self._state == TurnState.ERROR:
            self._set_state(TurnState.LISTENING, "recovered")

    def set_muted(self, muted: bool) -> None:
        # Muting is a UI/audio concern; keep the turn state semantics intact.
        if muted and self._state == TurnState.LISTENING:
            self._set_state(TurnState.IDLE, "muted")
        elif not muted and self._state == TurnState.IDLE:
            self._set_state(TurnState.LISTENING, "unmuted")

    # ------------------------------------------------------------------ audio
    def on_vad_frame(self, frame: VadFrame) -> None:
        self._last_activity_ms = frame.timestamp_ms
        if frame.is_speech:
            self._last_speech_vad_ms = frame.timestamp_ms
        if self._state in (TurnState.LISTENING, TurnState.INTERRUPTED):
            self.detector.on_vad(frame)

        if self._state in (TurnState.THINKING, TurnState.SPEAKING):
            context = BargeInContext(
                now_ms=frame.timestamp_ms,
                state=self._state,
                state_since_ms=self._state_since,
                speaking_since_ms=self._speaking_since,
                assistant_active=True,
                last_barge_in_ms=self._last_barge_in_ms,
            )
            decision = self.barge_policy.on_frame(frame, context)
            if decision.trigger:
                self._do_barge_in(decision.reason or "user_speech", decision, frame.timestamp_ms)
                return

            if frame.speech_probability >= 0.9 and frame.rms >= 0.02:
                # Keep the EOT detector aware of speech during the assistant
                # turn so a late final can be committed after a barge-in.
                self.detector.on_vad(frame)

    # -------------------------------------------------------------------- ASR
    def on_asr_partial(self, text: str, at_ms: int | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        at = at_ms if at_ms is not None else now_ms()
        self._last_activity_ms = at

        if self._state in (TurnState.THINKING, TurnState.SPEAKING):
            if at < self._ignore_asr_until:
                return
            # Partial text alone is not trusted as barge-in evidence: ASR can
            # hallucinate on residual echo. VAD/energy handles interruption.
            return

        if self._state not in (TurnState.LISTENING, TurnState.INTERRUPTED):
            return

        self.detector.on_asr_partial(text, at)
        if text != self._last_emitted_partial:
            self._last_emitted_partial = text
            self.callbacks.on_user_partial(text)

    def on_asr_final(self, text: str, at_ms: int | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        at = at_ms if at_ms is not None else now_ms()
        self._last_activity_ms = at

        if self._state in (TurnState.THINKING, TurnState.SPEAKING):
            if at < self._ignore_asr_until:
                return
            if not self.barge_in_on_asr:
                return
            if at - self._state_since < self.asr_barge_in_min_ms:
                return
            # ASR-only barge-in must be corroborated by recent VAD speech;
            # otherwise residual TTS echo can interrupt the assistant.
            if (
                self._last_speech_vad_ms is None
                or at - self._last_speech_vad_ms > 650
            ):
                self.callbacks.on_debug("asr_barge_in_ignored", {
                    "reason": "no_recent_vad",
                    "at_ms": at,
                    "last_vad_ms": self._last_speech_vad_ms,
                })
                return
            decision = BargeInDecision(
                trigger=True,
                reason="asr_final",
                confidence=0.85,
                speech_ms=max(0, at - self._state_since),
            )
            self._do_barge_in("asr_final", decision, at)
            # The final belongs to the interrupting utterance; feed it into the
            # fresh EOT detector so it can be committed normally.
            self.detector.on_asr_final(text, at)
            self._emit_final(text)
            return

        if self._state not in (TurnState.LISTENING, TurnState.INTERRUPTED):
            return

        self.detector.on_asr_final(text, at)
        self._emit_final(text)

    def on_server_speech_started(self, at_ms: int | None = None) -> None:
        at = at_ms if at_ms is not None else now_ms()
        self._last_speech_vad_ms = at
        if self._state in (TurnState.LISTENING, TurnState.INTERRUPTED):
            self.detector.on_server_speech_started(at)

    def on_server_speech_stopped(self, at_ms: int | None = None) -> None:
        at = at_ms if at_ms is not None else now_ms()
        if self._state in (TurnState.LISTENING, TurnState.INTERRUPTED):
            self.detector.on_server_speech_stopped(at)

    # -------------------------------------------------------------- assistant
    def on_assistant_started(self, turn_id: int, at_ms: int | None = None) -> None:
        if self._current_turn_id is None or turn_id != self._current_turn_id:
            return
        at = at_ms if at_ms is not None else now_ms()
        self._speaking_since = at
        self._set_state(TurnState.SPEAKING, f"turn:{turn_id}")

    def on_assistant_finished(self, turn_id: int, at_ms: int | None = None) -> None:
        if self._current_turn_id is None or turn_id != self._current_turn_id:
            return
        self._current_turn_id = None
        self._committed = False
        self._speaking_since = None
        self._ignore_asr_until = 0
        self.detector.reset()
        self.barge_policy.reset()
        self._set_state(TurnState.LISTENING, "assistant_done")

    def on_assistant_failed(self, turn_id: int, message: str) -> None:
        if self._current_turn_id is not None and turn_id != self._current_turn_id:
            return
        self._current_turn_id = None
        self._committed = False
        self._speaking_since = None
        self._set_state(TurnState.ERROR, message)

    def interrupt(self, reason: str = "manual", at_ms: int | None = None) -> None:
        at = at_ms if at_ms is not None else now_ms()
        if self._state in (TurnState.THINKING, TurnState.SPEAKING):
            decision = BargeInDecision(trigger=True, reason=reason, confidence=1.0)
            self._do_barge_in(reason, decision, at)
        else:
            self.detector.reset()
            self._last_emitted_partial = ""
            self._committed = False
            self._set_state(TurnState.LISTENING, "cleared")

    # --------------------------------------------------------------- lifecycle
    def poll(self, at_ms: int | None = None) -> None:
        at = at_ms if at_ms is not None else now_ms()
        if self._state == TurnState.INTERRUPTED and at >= self._interrupted_until:
            self._set_state(TurnState.LISTENING, "after_barge_in")
            return
        if self._state != TurnState.LISTENING:
            return
        decision = self.detector.poll(at)
        if decision.commit:
            self._commit(decision, at)

    def reset_session(self) -> None:
        self._epoch += 1
        self._current_turn_id = None
        self._committed = False
        self._speaking_since = None
        self._last_emitted_partial = ""
        self._last_emitted_final = ""
        self.detector.reset()
        self.barge_policy.reset()
        self._set_state(TurnState.LISTENING, "new_session")

    def force_commit(self, text: str, reason: str = "text_input") -> None:
        """Commit a turn without waiting for ASR; used by the demo text box."""
        if self._state not in (TurnState.IDLE, TurnState.LISTENING, TurnState.INTERRUPTED):
            return
        text = (text or "").strip()
        if not text:
            return
        if self._state == TurnState.INTERRUPTED:
            self._set_state(TurnState.LISTENING, "typed")
        self._committed = False
        self._commit(
            EndOfTurnDecision(
                commit=True,
                reason=reason,
                confidence=1.0,
                text=text,
                hold_ms=0,
            ),
            now_ms(),
        )

    # ---------------------------------------------------------------- internal
    def _commit(self, decision: Any, at: int) -> None:
        if self._committed:
            return
        text = (decision.text or "").strip()
        if len(text) < max(1, self.min_utterance_chars):
            self.detector.reset()
            return
        self._committed = True
        self._turn_counter += 1
        turn_id = self._turn_counter
        self._current_turn_id = turn_id
        self._speaking_since = None
        # Ignore trailing ASR finals from the utterance we just committed.
        self._ignore_asr_until = at + 900
        if text != self._last_emitted_final:
            self._emit_final(text)
        user_turn = UserTurn(
            turn_id=turn_id,
            text=text,
            reason=decision.reason,
            confidence=decision.confidence,
            started_ms=self.detector.snapshot().get("utterance_started_ms") or at,
            committed_ms=at,
        )
        self.detector.reset()
        self.barge_policy.reset()
        self._set_state(TurnState.THINKING, f"commit:{decision.reason}")
        self.callbacks.on_commit(user_turn)
        self.callbacks.on_debug("commit", {
            "turn_id": turn_id,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "text": text,
        })

    def _do_barge_in(self, reason: str, decision: BargeInDecision, at: int) -> None:
        if self._state not in (TurnState.THINKING, TurnState.SPEAKING):
            return
        cancelled_turn = self._current_turn_id
        self._epoch += 1
        self._current_turn_id = None
        self._committed = False
        self._speaking_since = None
        self._last_barge_in_ms = at
        self._interrupted_until = at + 750
        self.detector.reset()
        self.barge_policy.reset()
        self._set_state(TurnState.INTERRUPTED, f"barge_in:{reason}")
        self.callbacks.on_barge_in(reason, decision)
        self.callbacks.on_cancel(reason)
        self.callbacks.on_debug("barge_in", {
            "reason": reason,
            "cancelled_turn": cancelled_turn,
            "speech_ms": decision.speech_ms,
            "epoch": self._epoch,
        })

    def _emit_final(self, text: str) -> None:
        self._last_emitted_final = text
        self.callbacks.on_user_final(text)

    def _set_state(self, state: TurnState, detail: str = "") -> None:
        if state == self._state and detail == self._state_detail:
            return
        self._state = state
        self._state_detail = detail
        self._state_since = now_ms()
        log.debug("turn state -> %s (%s)", state.value, detail)
        self.callbacks.on_state(state, detail)
